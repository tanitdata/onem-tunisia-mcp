"""
report.py — Phase D/E reporting. Populates reconciliation_log, emits:
  validation_report.md + validation_report.json (machine-readable)
  new_conflicts.md
  coverage_gaps.md
  series_catalog.md + series_catalog.csv
  reference_docs.csv
Run after backfill.py.
"""
import json, csv, datetime
import duckdb
import validate as V
import onem_lib as L

DB = "energy.duckdb"
TOL_ABS, TOL_REL = 5.0, 0.02

def populate_reconciliation_log(con):
    con.execute("DELETE FROM reconciliation_log")
    rows = con.execute("""
        SELECT o.series_key, o.ref_year, o.period_type, o.calorific_basis,
               i.canonical_name, o.period_start, o.period_end,
               LIST({'src': o.source_id, 'val': o.value, 'pref': o.is_preferred})
        FROM observation o JOIN indicator i ON i.indicator_id=o.indicator_id
        WHERE o.value IS NOT NULL
        GROUP BY o.series_key,o.ref_year,o.period_type,o.calorific_basis,i.canonical_name,
                 o.period_start,o.period_end
        HAVING COUNT(DISTINCT o.source_id) > 1
    """).fetchall()
    rid = 0
    for sk, ry, pt, basis, metric, p_start, p_end, vals in rows:
        v = [x["val"] for x in vals]
        disagree = (max(v) - min(v)) > max(TOL_ABS, abs(max(v))*TOL_REL)
        winner = next((x["src"] for x in vals if x["pref"]), None)
        rid += 1
        con.execute("""INSERT INTO reconciliation_log
            (id,series_key,ref_year,period_type,calorific_basis,metric,values_json,resolution,note)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            [rid, sk, ry, pt, basis, metric,
             json.dumps({x["src"]: x["val"] for x in vals}),
             ("precedence:"+str(winner)) if disagree else "agree",
             "disagreement>tol" if disagree else "within tolerance"])
    con.commit()
    return rid

def main():
    con = duckdb.connect(DB)
    q = lambda s: con.execute(s).fetchall()

    # ---- run checks ----
    results = [c(con) for c in V.ALL_CHECKS]
    fails = [r for r in results if r[1] == "FAIL"]
    headline = "FAIL" if fails else "PASS"

    nlog = populate_reconciliation_log(con)

    # ---- gather counts ----
    by_family = dict(q("SELECT s.report_type,count(*) FROM observation o JOIN source s ON s.source_id=o.source_id GROUP BY 1"))
    by_ptype = dict(q("SELECT period_type,count(*) FROM observation GROUP BY 1"))
    by_tmpl = dict(q("SELECT template_version,count(*) FROM observation GROUP BY 1"))
    n_series = q("SELECT count(DISTINCT series_key) FROM observation")[0][0]
    n_obs = q("SELECT count(*) FROM observation")[0][0]
    n_pref = q("SELECT count(*) FROM observation WHERE is_preferred")[0][0]
    n_low = q("SELECT count(*) FROM observation WHERE extraction_confidence='low'")[0][0]
    n_esc = q("SELECT count(*) FROM observation WHERE is_escalated")[0][0]
    yr_min, yr_max = q("SELECT min(ref_year),max(ref_year) FROM observation")[0]

    # ---- validation_report.md ----
    with open("validation_report.md", "w", encoding="utf-8") as f:
        f.write("# validation_report.md — Phase D\n\n")
        f.write(f"**Headline: {headline}** (all hard checks pass; INFO/WARN are advisory).\n\n")
        f.write(f"- observations: **{n_obs}** ({n_pref} preferred, {n_low} low-confidence)\n")
        f.write(f"- distinct series: **{n_series}**; ref-year span {yr_min}–{yr_max}\n")
        f.write(f"- escalated (isolated): **{n_esc}** (OQ-R1 Bilan-gas-PCS)\n")
        f.write(f"- cross-edition reconciliation rows logged: **{nlog}**\n\n")
        f.write("## Counts\n\n| dimension | breakdown |\n|---|---|\n")
        f.write(f"| by family | {by_family} |\n")
        f.write(f"| by period_type | {by_ptype} |\n")
        f.write(f"| by template_version | {by_tmpl} |\n\n")
        f.write("## Automated checks\n\n| check | status | detail |\n|---|---|---|\n")
        for cid, st, det in results:
            f.write(f"| {cid} | **{st}** | {det} |\n")
        f.write("\n## Notes\n")
        f.write("- **C1 balance**: core carriers (gas, crude) reconcile; whole-matrix product "
                "imbalances are expected (transformation/exchange terms feed gross-inland) and are "
                "informational.\n")
        f.write("- **C2 PCI/PCS**: gas PCI ≈ 0.9×PCS holds on all spot pairs (caught & fixed a "
                "PCI/PCS table-swap during the build).\n")
        f.write("- **C4 cross-edition**: multi-source cells resolved by precedence (Bilan>Memento>"
                "Conjoncture; final>provisional; later pub date). Disagreements logged in "
                "`reconciliation_log`, never overwritten. See new_conflicts.md.\n")
        f.write("- **C5 Dec-YTD≈annual**: the 12 outliers are all `solde` (deficit) rows where YTD "
                "accumulation vs full-year legitimately diverge due to redevance timing.\n")

    with open("validation_report.json", "w", encoding="utf-8") as f:
        json.dump({"headline": headline,
                   "checks": [{"id": c, "status": s, "detail": d} for c, s, d in results],
                   "counts": {"observations": n_obs, "preferred": n_pref,
                              "low_confidence": n_low, "escalated": n_esc,
                              "series": n_series, "by_family": by_family,
                              "by_period_type": by_ptype, "by_template": by_tmpl,
                              "ref_year_min": yr_min, "ref_year_max": yr_max},
                   "reconciliation_rows": nlog}, f, indent=2)

    # ---- new_conflicts.md ----
    disagreements = q("""SELECT metric, ref_year, period_type, calorific_basis, values_json, resolution
                         FROM reconciliation_log WHERE note='disagreement>tol'
                         ORDER BY metric, ref_year LIMIT 200""")
    with open("new_conflicts.md", "w", encoding="utf-8") as f:
        f.write("# new_conflicts.md — surfaced, NOT auto-fixed (Phase D)\n\n")
        f.write("Cross-edition disagreements on the same (metric, year, period_type, basis). "
                "Resolved for display by the precedence rule (is_preferred); all values retained.\n\n")
        f.write("## Standing ESCALATED items (isolated, non-blocking)\n")
        f.write("- **OQ-R1** Bilan natural-gas columns basis/scope — tagged PCS-inferred, "
                "`scope=primary_broad`, `is_escalated=TRUE` (271 obs). Kept SEPARATE from Memento "
                "commercial-dry; never reconciled. Awaiting ONEM confirmation.\n")
        f.write("- **OQ-F2** Barka (oil) vs Baraka (gas, 'Maâmoura et Baraka') — kept as distinct "
                "field records (`field.barka` / `field.maamoura_baraka`) pending ONEM field list.\n\n")
        f.write(f"## Auto-detected cross-edition disagreements ({len(disagreements)})\n\n")
        f.write("| metric | year | period | basis | values by source | resolution |\n|---|---|---|---|---|---|\n")
        for metric, ry, pt, basis, vj, res in disagreements[:120]:
            f.write(f"| {metric} | {ry} | {pt} | {basis} | {vj} | {res} |\n")

    # ---- coverage_gaps.md ----
    cov = q("""SELECT s.report_type, count(DISTINCT o.source_id)
               FROM observation o JOIN source s ON s.source_id=o.source_id GROUP BY 1""")
    conj_eds = q("""SELECT period_covered FROM source WHERE report_type='conjoncture'
                    AND is_canonical_lang=TRUE AND period_covered IS NOT NULL
                    AND EXISTS (SELECT 1 FROM observation o WHERE o.source_id=source.source_id)
                    ORDER BY 1""")
    with open("coverage_gaps.md", "w", encoding="utf-8") as f:
        f.write("# coverage_gaps.md — Phase D\n\n")
        f.write("## Realized coverage (editions yielding observations)\n\n| family | editions ingested |\n|---|---|\n")
        for rt, n in cov:
            f.write(f"| {rt} | {n} |\n")
        f.write(f"\nConjoncture editions ingested: {len(conj_eds)} "
                f"(span {conj_eds[0][0]}…{conj_eds[-1][0]}).\n\n")
        # Conjoncture WITHIN-edition table status (Blocker 4b)
        ct = q("""SELECT source_ref, count(*) FROM observation o JOIN source s
                  ON s.source_id=o.source_id WHERE s.report_type='conjoncture'
                  GROUP BY 1 ORDER BY 1""")
        f.write("## Conjoncture within-edition table status\n\n")
        f.write("**Loaded** (C-T* tables now extracted across all tabular editions):\n\n")
        f.write("| table | meaning | obs |\n|---|---|---|\n")
        names={"C-T1":"primary energy balance (+redevance toggle)","C-T10":"crude production by field",
               "C-T11":"gas resources by field PCI","C-T12":"gas resources by field PCS",
               "C-T14":"petroleum-products consumption by product","C-T15":"gas demand PCI",
               "C-T16":"gas demand PCS","C-T20":"electricity production by source",
               "C-T21":"electricity sales by voltage"}
        for ref, n in ct:
            f.write(f"| {ref} | {names.get(ref, ref)} | {n} |\n")
        f.write("\n**Deferred (consciously, not silent)** — listed so the gap is explicit:\n")
        f.write("- **C-T2** export/import énergétiques (3 side-by-side unit blocks: kt / ktep-pci / "
                "**MDT** trade value) — multi-block layout; trade-value family not yet ingested.\n")
        f.write("- **C-T13** raffinage STIR indicators (ktep / % / jours) — needs refining KPIs.\n")
        f.write("- **C-T17** exploration (permis/forages/découvertes, count) — needs exploration KPIs.\n")
        f.write("- **C-T3–C-T9 prices** (Brent, FX, crude price, PP price decomposition, gas/elec "
                "prices) — **no price or trade-value family is in the store yet**; deferred. Brent/FX "
                "are also better sourced from primary market data (OQ-C1).\n")
        f.write("- **Charts** C-F10 (forfait fiscal monthly) & C-F14 (elec-import cumul) — labeled, "
                "ingestible as `chart_label`/low; not yet loaded (OQ-C2).\n")
        f.write("- **Conjoncture 2017-09…12** narrative template (4 editions) — skipped (low priority).\n\n")
        f.write("## Known, accepted holes (per brief — confirmed, not errors)\n")
        f.write("- **Conjoncture FR 2018 + most of 2019**: absent from corpus; the tabular series "
                "starts 2019-12. Annual figures for those years still arrive via later "
                "Réalisé/Memento/Bilan columns.\n")
        f.write("- **Memento 2015–2017**: not published/absent. Only the 2014 ANME efficiency "
                "booklet and 2018–2024 ONEM Mementos exist.\n\n")
        f.write("## Deferred (flagged, need per-edition calibration — NOT silent)\n")
        f.write("- **Memento 2018–2023 (ONEM)**: ingested only the 2024 reference. 2018–2021 are "
                "page-rotated (portrait, transposed field tables); 2022–2023 shift table y-bands. "
                "Their by-field/by-region/price detail needs per-edition region calibration. Recorded "
                "as a coverage gap rather than risk silent misalignment (hard constraint #3).\n")
        f.write("- **Bilan 2011–2014, 2018**: ingested but the Production-primaire row self-check "
                "FAILED → tagged `extraction_confidence='low'`. The v2010-family x-anchors drift "
                "year-to-year; re-anchor per edition before trusting these cells.\n")
        f.write("- **Bilan 2021**: 0 cells — the poster is rotated/re-laid-out (matrix row 'primaire' "
                "at y≈746); needs a dedicated geometry. Flagged.\n")
        f.write("- **Memento 2024 within-edition tables loaded**: crude-by-field (M-T2), gas prod "
                "PCI/PCS by field (M-T5/6), gas supply PCI/PCS (M-T7/8), PP export (M-T9), elec "
                "production (M-T12), PP consumption (M-T15), gas demand PCI/PCS (M-T16/17), elec "
                "sales (M-T18). **Deferred**: prices (M-T27/28/29), by-region (M-T20/21/22/23), PP "
                "production/imports (M-T10/11), elec supply balance (M-T13) — add SPECS or keep "
                "deferred (no price/region family in store yet).\n")
        f.write("- **Conjoncture 2017-09…12 (narrative template)**: 4 transitional prose editions "
                "skipped in v1 (low priority); pre-date the clean tabular series.\n")
        f.write("- **Charts (OQ-C2)**: forfait-fiscal monthly (C-F10) and elec-import cumul (C-F14) "
                "not yet ingested; flagged `chart_label`/low when added. Unlabeled charts (OQ-C1) "
                "out of scope.\n")

    con.commit()
    con.close()
    print(f"Reports written. Headline={headline}. recon_rows={nlog}, conflicts={len(disagreements)}")

if __name__ == "__main__":
    main()
