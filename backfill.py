"""
backfill.py — Phase D full corpus ingest (idempotent, re-runnable).

Drives all in-scope editions through the generalized loaders:
  - Bilan posters (2010-2024) via load_bilan + per-template/version
  - Memento ONEM (2018-2024) via load_memento, excluding the 2014 ANME file
  - Conjoncture FR (2019-12 .. 2026-04) via load_conjoncture, cutoff read per edition
  - Rapport: matrix superset -> precedence only (the poster Bilan is canonical); we
    register the source but do NOT re-ingest the matrix (avoids double count).
  - COVID + ANME-2014 + Other(17): catalog only, no series.

FR/multi canonical; AR sources carry is_canonical_lang=FALSE and are NOT ingested.
Each edition is tagged with its template_version and an extraction self-check result.
After ingest, recompute is_preferred per (series_key, period) by precedence (OQ-R5).
"""
import re
import duckdb
import onem_lib as L
import seed as SEED
import load_bilan, load_memento, load_conjoncture

def bilan_template(year):
    if year <= 2014: return "bilan-matrix-v2010"
    if year == 2015: return "bilan-matrix-v2015"
    return "bilan-matrix-v2024"

def run(reseed=True):
    if reseed:
        SEED.main()
    con = duckdb.connect(SEED.DB_PATH)
    db = L.DB(con)
    v = L.Vocab(".")
    man = L.load_manifest()
    report = {"bilan":[], "memento":[], "conjoncture":[], "skipped":[]}

    # ---- Bilan posters (multi-language single file = canonical) ----
    for r in man:
        if r["report_family"] != "Bilan" or r["language"] != "multi":
            continue
        if not r["period"].isdigit():
            report["skipped"].append((L.derive_source_id(r)[0], "non-year Bilan (Evolution/Note)"))
            continue
        year = int(r["period"])
        sid, _ = L.derive_source_id(r)
        tmpl = bilan_template(year)
        ver = "v2" if year == 2024 else None
        try:
            n, chk = load_bilan.load(db, r["local_path"], source_id=sid,
                                     template_version=tmpl, ref_year=year,
                                     data_status=("final" if year < 2024 else "final"),
                                     version=ver)
            report["bilan"].append((sid, year, tmpl, n, chk))
        except Exception as e:
            report["bilan"].append((sid, year, tmpl, 0, f"ERROR:{e}"))

    # ---- Memento ONEM. The 2024 reference layout is validated and ingested. Older
    # ONEM editions (2018-2023) have per-edition geometry drift (2018-2021 are page-
    # rotated; 2022-2023 shift table y-bands) that the reference SPECS do not yet fit;
    # the loader self-rejects rotated layouts and we ingest only editions whose gas-
    # TOTAL identity reconciles, to avoid silent misalignment (hard constraint #3).
    # Editions not ingested are recorded as coverage gaps (per-edition calibration TODO).
    # 2014 = ANME efficiency publication -> reference_docs (not this series).
    for r in man:
        if r["report_family"] != "Memento" or r["language"] != "fr":
            continue
        if r["period"] == "2014":
            report["skipped"].append(("memento_2014", "ANME energy-efficiency, not ONEM supply -> reference_docs"))
            continue
        sid, _ = L.derive_source_id(r)
        if r["period"] != "2024":
            report["skipped"].append((sid, f"Memento {r['period']} geometry drift -> coverage gap (calibration TODO)"))
            continue
        try:
            n = load_memento.load(db, v, r["local_path"], source_id=sid,
                                  template_version="memento-onem-v2024", ref_year=2024)
            report["memento"].append((sid, r["period"], n))
        except Exception as e:
            report["memento"].append((sid, r["period"], f"ERROR:{e}"))

    # ---- Conjoncture FR tabular (2019-12 .. 2026-04) ----
    for r in man:
        if r["report_family"] != "Conjoncture" or r["language"] != "fr":
            continue
        if not re.match(r"^\d{4}-\d{2}$", r["period"] or ""):
            report["skipped"].append((L.derive_source_id(r)[0], "Conjoncture without YYYY-MM period"))
            continue
        # 2017 narrative editions: different template, low priority -> skip in v1 ingest
        if r["period"] < "2019-12":
            report["skipped"].append((L.derive_source_id(r)[0], "conjoncture-vNarrative-2017 (low priority)"))
            continue
        sid, _ = L.derive_source_id(r)
        try:
            n, hdr = load_conjoncture.load(db, v, r["local_path"], source_id=sid,
                                           template_version="conjoncture-tabular-v2024")
            report["conjoncture"].append((sid, r["period"], n, hdr.get("cutoff_month")))
        except Exception as e:
            report["conjoncture"].append((sid, r["period"], f"ERROR:{e}", None))

    con.commit()
    recompute_preferred(con)            # set is_preferred first
    flag_rollup_low_confidence(con)     # then downgrade non-reconciling/partial groups
    con.commit()
    return con, db, report

def flag_rollup_low_confidence(con):
    """Post-pass (best-practice over silent trust): for each captured grand-Total in a
    breakdown table, if its LEAF rows (is_total=FALSE) don't reconcile to the Total
    (>5%), the table cell was mis-extracted for that edition -> mark all its rows
    extraction_confidence='low' so v_series_clean excludes them. The reconciling
    editions (the large majority) stay 'normal'. This makes per-edition layout drift a
    flagged, conscious downgrade rather than a silent wrong value."""
    flagged = 0
    for ref, where_total in GRAND_TOTAL_SQL.items():
        totals = con.execute(f"""
            SELECT source_id, period_start, period_end, value FROM observation
            WHERE source_ref=? AND is_total=TRUE AND value IS NOT NULL AND ({where_total})""",
            [ref]).fetchall()
        for sid, ps, pe, tot in totals:
            if not tot:
                continue
            leaf = con.execute("""SELECT COALESCE(SUM(value),0) FROM observation
                WHERE source_id=? AND source_ref=? AND period_start=? AND period_end=?
                  AND is_total=FALSE AND value IS NOT NULL""", [sid, ref, ps, pe]).fetchone()[0]
            if abs(leaf - tot) > max(8.0, abs(tot) * 0.05):
                con.execute("""UPDATE observation SET extraction_confidence='low'
                    WHERE source_id=? AND source_ref=? AND period_start=? AND period_end=?""",
                    [sid, ref, ps, pe])
                flagged += 1
    # Second pass: a breakdown group that has LEAVES but NO canonical grand total is a
    # thin/partial extraction (e.g. a sparse 2010 baseline column that captured only a
    # row or two). It can't form a trustworthy partition -> flag low-confidence so the
    # clean surface (and the C11/C12 gates that read it) never sees a leaves-without-total
    # group. Keeps the data (queryable in v_series) but out of the default-sum surface.
    # NOTE: this runs AFTER recompute_preferred so it sees the preferred set the MCP uses
    # (a grand total may be non-preferred while its leaves are preferred, splitting the
    # partition across editions — those groups must also be flagged).
    # A group is "incomplete" (flag low) when, among its preferred rows, there is NO
    # canonical grand total AND it has either leaves (would under/over-sum) or only
    # orphan alternative-breakdown rows (a partial capture of a sparse baseline column).
    # Either way it cannot form a trustworthy default-sum partition.
    for ref in ("C-T20", "C-T21", "C-T14", "C-T15", "C-T16"):
        groups = con.execute("""
            SELECT period_start, period_end, source_id,
                   COUNT(*) FILTER (WHERE NOT is_total) n_leaf,
                   COUNT(*) FILTER (WHERE aggregation_role='grand_total') n_grand,
                   COUNT(*) AS n_any
            FROM observation WHERE source_ref=? AND is_preferred GROUP BY 1,2,3
            HAVING n_grand = 0 AND n_any > 0""", [ref]).fetchall()
        for ps, pe, sid, *_ in groups:
            con.execute("""UPDATE observation SET extraction_confidence='low'
                WHERE source_id=? AND source_ref=? AND period_start=? AND period_end=?""",
                [sid, ref, ps, pe])
            flagged += 1
    return flagged

# The canonical grand-total row per breakdown table (the one the leaves must sum to).
# Pinning the exact total row avoids treating supply-balance rows (echanges/achats),
# which are also is_total, as the table total.
GRAND_TOTAL_SQL = {
    "C-T14": "product_id='prod.petroleum_products_total'",
    "C-T15": "flow_id='flow.demand' AND COALESCE(scope,'')='' AND COALESCE(level_id,'')=''",
    "C-T16": "flow_id='flow.demand' AND COALESCE(scope,'')='' AND COALESCE(level_id,'')=''",
    "C-T20": "flow_id='flow.primary_production' AND COALESCE(producer_id,'')='' "
             "AND COALESCE(technology,'')='' AND COALESCE(scope,'')=''",
    "C-T21": "flow_id='flow.sales' AND COALESCE(level_id,'')='' AND COALESCE(geography_scope,'')=''",
}

# ----------------------------------------------------------------- precedence
PRECEDENCE_TYPE = {"bilan": 3, "memento": 2, "rapport": 2, "conjoncture": 1, "covid_bulletin": 0}
STATUS_RANK = {"final": 3, "revised": 2, "provisional": 1, "estimated": 0}

def recompute_preferred(con):
    """Set is_preferred per (series_key, period_start, period_end) by OQ-R5 precedence:
    report-type rank, then data_status rank, then later publication_date. Losers stay
    (history preserved) with is_preferred=FALSE and supersedes_id -> winner."""
    rows = con.execute("""
        SELECT o.observation_id, o.series_key, o.period_start, o.period_end,
               s.report_type, o.data_status, s.publication_date
        FROM observation o JOIN source s ON s.source_id=o.source_id""").fetchall()
    groups = {}
    for oid, sk, ps, pe, rtype, status, pub in rows:
        groups.setdefault((sk, ps, pe), []).append((oid, rtype, status, pub))
    # reset (clear self-FK first to avoid DuckDB self-reference UPDATE limitation)
    con.execute("UPDATE observation SET supersedes_id=NULL WHERE supersedes_id IS NOT NULL")
    con.execute("UPDATE observation SET is_preferred=TRUE")
    losers = []   # (loser_oid, winner_oid)
    for key, members in groups.items():
        if len(members) == 1:
            continue
        def rank(m):
            _, rtype, status, pub = m
            return (PRECEDENCE_TYPE.get(rtype, 0), STATUS_RANK.get(status, 0),
                    str(pub) if pub else "")
        members.sort(key=rank, reverse=True)
        winner = members[0][0]
        for oid, *_ in members[1:]:
            losers.append((oid, winner))
    for oid, winner in losers:
        con.execute("UPDATE observation SET is_preferred=FALSE, supersedes_id=? WHERE observation_id=?",
                    [winner, oid])

if __name__ == "__main__":
    con, db, report = run()
    print("\n=== BACKFILL SUMMARY ===")
    nb = sum(x[3] for x in report["bilan"] if isinstance(x[3], int))
    print(f"Bilan editions: {len(report['bilan'])}, obs={nb}")
    for sid, yr, tmpl, n, chk in sorted(report["bilan"], key=lambda z: z[1]):
        print(f"   {yr} {tmpl:20} {str(n):>4} cells  check={chk}")
    nm = sum(x[2] for x in report["memento"] if isinstance(x[2], int))
    print(f"Memento editions: {len(report['memento'])}, obs={nm}")
    nc = sum(x[2] for x in report["conjoncture"] if isinstance(x[2], int))
    print(f"Conjoncture editions: {len(report['conjoncture'])}, obs={nc}")
    print(f"skipped: {len(report['skipped'])}")
    tot = con.execute("SELECT COUNT(*) FROM observation").fetchone()[0]
    pref = con.execute("SELECT COUNT(*) FROM observation WHERE is_preferred").fetchone()[0]
    print(f"TOTAL observations={tot}, preferred={pref}, stats={db.stats}")
    con.close()
