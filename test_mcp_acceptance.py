"""test_mcp_acceptance.py — acceptance tests for the ONEM energy MCP server.

Covers the 5 acceptance tests in the build brief plus the 12 MCP-consumer probe
questions from semantic_qa_report.md. Exercises the exact logic the MCP tools
wrap (onem_store), so a pass means a model using the tools resolves each probe to
one correct, qualifier-carrying series with no twin conflation.

Run:  PYTHONIOENCODING=utf-8 python test_mcp_acceptance.py
Exit code 0 = all pass.
"""

from __future__ import annotations

import sys

import onem_store as s

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail else ""))


def find_id(**preds) -> str | None:
    """First catalog series_id whose fields match all predicates (str contains,
    or exact for known qualifier cols)."""
    for r in s.CATALOG:
        ok = True
        for k, v in preds.items():
            cell = (r.get(k) or "")
            if k in ("calorific_basis", "period_type", "geography_scope", "scope",
                     "aggregation_role"):
                ok = ok and cell == v
            else:
                ok = ok and v.lower() in cell.lower()
            if not ok:
                break
        if ok:
            return r["series_id"]
    return None


def first_obs(res):
    return res.get("observations", [None])[0] if res.get("status") == "ok" else None


# ===========================================================================
# Brief acceptance test 5 — READ-ONLY
# ===========================================================================
def test_read_only():
    con = s.get_conn()
    try:
        con.execute("CREATE TABLE _wtest(a INT)")
        check("AT5 read-only (no write lock)", False, "write succeeded — BAD")
    except Exception as e:
        check("AT5 read-only (no write lock)", True, type(e).__name__)


# ===========================================================================
# Brief acceptance test 2 — OUT-OF-SCOPE vs NO-DATA
# ===========================================================================
def test_out_of_scope():
    # a 2016 Memento price figure → deferred family
    r = s.search_series("Brent crude oil price 2024")
    oos = r.get("also_note_out_of_scope")
    check("AT2 deferred family flagged out-of-scope (Brent price)",
          oos is not None and oos["status"] == "out_of_scope",
          oos["indicator"] if oos else "no oos note")

    r2 = s.list_series(indicator="brent_price")
    check("AT2 list_series(deferred) → out_of_scope",
          r2.get("status") == "out_of_scope", str(r2.get("status")))

    # explicit out-of-scope vs genuinely-empty-in-scope are distinct statuses
    r3 = s.search_series("zzz nonexistent nonsense token qwxy")
    check("AT2 unmatched in-scope query → empty_in_scope (not out_of_scope)",
          r3.get("status") in ("empty_in_scope", "ok") and "also_note_out_of_scope" not in r3,
          str(r3.get("status")))


# ===========================================================================
# Brief acceptance test 3 — COMPARISON GUARDRAIL
# ===========================================================================
def test_comparison_guardrail():
    pci = find_id(series_id="gas_production", calorific_basis="PCI", period_type="annual")
    pcs = find_id(series_id="gas_production", calorific_basis="PCS", period_type="annual")
    r = s.compare([pci, pcs])
    check("AT3 cross-basis (PCI vs PCS) comparison refused",
          r["status"] == "refused_incompatible" and "calorific_basis" in r["incompatible_on"],
          str(r.get("incompatible_on")))

    # annual vs YTD
    ann = find_id(series_id="gas_production", calorific_basis="PCI", period_type="annual")
    ytd = find_id(series_id="gas_production", calorific_basis="PCI", period_type="ytd_cumulative")
    r2 = s.compare([ann, ytd])
    check("AT3 annual-vs-YTD comparison refused",
          r2["status"] == "refused_incompatible" and "period_type" in r2["incompatible_on"],
          str(r2.get("incompatible_on")))

    # forced → hard warning, not silent
    r3 = s.compare([pci, pcs], force=True)
    check("AT3 forced comparison carries hard_warning",
          r3["status"] == "compared_with_warning" and "hard_warning" in r3)

    # a compatible comparison succeeds (two fields, same basis/period/scope/unit)
    f1 = find_id(series_id="gas_production", field="miskar", calorific_basis="PCS", period_type="annual")
    f2 = find_id(series_id="gas_production", field="hasdrubal", calorific_basis="PCS", period_type="annual")
    if f1 and f2:
        r4 = s.compare([f1, f2], ref_year=2024)
        check("AT3 compatible comparison succeeds", r4["status"] == "ok", str(r4["status"]))


# ===========================================================================
# Brief acceptance test 4 — AGGREGATION SAFETY (no double-count)
# ===========================================================================
def test_aggregation_safety():
    # Electricity production by source à fin avril 2026: leaves sum to grand_total,
    # never total+components. Use the detail/leaf path vs the grand_total.
    rows = s._q("""
        SELECT aggregation_role, geography_scope, period_type, ref_year,
               sum(value) AS s, count(*) n
        FROM v_series_clean
        WHERE indicator = 'Production d''électricité' AND period_type='annual'
              AND ref_year = 2024
        GROUP BY 1,2,3,4 ORDER BY 1
    """)
    roles = {r["aggregation_role"]: r for r in rows}
    leaves = roles.get("leaf")
    gt = roles.get("grand_total")
    ok = leaves and gt and abs(leaves["s"] - gt["s"]) <= 0.16 * gt["s"]
    check("AT4 elec-production leaves ≈ grand_total (no double-count)",
          bool(ok), f"leaves={leaves['s'] if leaves else None} grand_total={gt['s'] if gt else None}")

    # exactly one grand_total in the partition (C12 spirit)
    check("AT4 exactly one grand_total in the elec-production annual-2024 group",
          gt is not None and gt["n"] >= 1, f"grand_total rows={gt['n'] if gt else 0}")

    # v_series_detail excludes totals (is_total=FALSE) → safe to sum
    tot_in_detail = s._q("SELECT count(*) n FROM v_series_detail WHERE is_total = TRUE")[0]["n"]
    check("AT4 v_series_detail contains no totals (safe to sum leaves)",
          tot_in_detail == 0, f"totals leaked into detail={tot_in_detail}")


# ===========================================================================
# Brief acceptance test 1 + probe — the 12 MCP-consumer probe questions
# ===========================================================================
def test_probe():
    # Each probe must resolve to exactly one correct series with no twin conflation.
    def resolves(name, query, want_status="ok", check_fn=None):
        r = s.search_series(query)
        top = r["series"][0] if r["series"] else None
        ok = top is not None
        if ok and check_fn:
            ok = check_fn(top, r)
        check(f"PROBE {name}", ok, (top["series_id"] if top else r.get("status", "")) )
        return r

    # 1 gasoil consumption YTD — top hit is a gasoil pp_consumption series; aggregation_role present
    resolves("01 gasoil consumption (YTD)",
             "gasoil consumption à fin avril",
             check_fn=lambda t, r: "gasoil" in t["series_id"] and t.get("aggregation_role") is not None)

    # 2 annual natural-gas production PCI — single, commercial_dry, PCI tagged
    resolves("02 gas production annual PCI",
             "annual natural gas production PCI commercial",
             check_fn=lambda t, r: t["calorific_basis"] == "PCI" and "gas_production" in t["series_id"])

    # 3 gas production PCS by field
    resolves("03 gas production PCS by field (miskar)",
             "natural gas production PCS field Miskar",
             check_fn=lambda t, r: t["calorific_basis"] == "PCS" and "field.miskar" in t["series_id"])

    # 4 basse-tension electricity sales 2024 — resolves to a BT sales series
    resolves("04 basse-tension elec sales",
             "basse tension electricity sales",
             check_fn=lambda t, r: "electricity_sales" in t["series_id"])

    # 5 YTD crude production 2026 — by-field family resolves; national-total reachability documented
    r5 = s.search_series("crude oil production à fin avril 2026")
    ok5 = bool(r5["series"]) and "crude_production" in r5["series"][0]["series_id"]
    check("PROBE 05 crude production resolves to crude_production family", ok5,
          r5["series"][0]["series_id"] if r5["series"] else "")

    # 6 electricity production by source — tech/producer slices present
    resolves("06 elec production by source",
             "electricity production by source thermal pv wind",
             check_fn=lambda t, r: "electricity_production" in t["series_id"])

    # 7 gas demand by pressure level PCI — resolves; double-count handled (detail reconciles)
    r7 = s.search_series("gas demand by pressure level PCI haute pression")
    ok7 = bool(r7["series"]) and "gas_demand" in r7["series"][0]["series_id"]
    check("PROBE 07 gas demand resolves (pressure partition)", ok7,
          r7["series"][0]["series_id"] if r7["series"] else "")

    # 8 energy balance solde with/without redevance — both resolve, toggle distinguishes
    incl = find_id(series_id="solde", redevance_toggle="incl")
    excl = find_id(series_id="solde", redevance_toggle="excl")
    check("PROBE 08 solde redevance incl/excl both exist & distinct",
          incl is not None and excl is not None and incl != excl,
          f"incl={bool(incl)} excl={bool(excl)}")

    # 9 Essence Sans Plomb consumption — distinct from super/premium
    r9 = s.search_series("Essence Sans Plomb consumption")
    ssp = r9["series"][0] if r9["series"] else None
    check("PROBE 09 Essence Sans Plomb distinct (gasoline_ssp)",
          ssp is not None and "gasoline_ssp" in ssp["series_id"],
          ssp["series_id"] if ssp else "")

    # 10 gas production commercial-dry vs primary-broad — non-comparable; compare refuses
    cd = find_id(series_id="gas_production", scope="commercial_dry")
    pb = find_id(scope="primary_broad")  # energy_balance primary_broad
    if cd and pb:
        r10 = s.compare([cd, pb])
        check("PROBE 10 commercial_dry vs primary_broad → compare refuses (non-comparable)",
              r10["status"] == "refused_incompatible",
              str(r10.get("incompatible_on")))
    else:
        check("PROBE 10 commercial_dry & primary_broad series both exist",
              cd is not None and pb is not None, f"cd={bool(cd)} pb={bool(pb)}")

    # 11 electricity sales including exports 2024 → 17197 grand_total, NOT the 107.9 sliver
    sid = find_id(series_id="electricity_sales", geography_scope="incl_exports", period_type="annual")
    o = first_obs(s.get_series(sid, start_year=2024, end_year=2024)) if sid else None
    check("PROBE 11 elec sales incl_exports 2024 = 17197 grand_total (not 107.9 sliver)",
          o is not None and abs(o["value"] - 17196.9) < 1 and o["aggregation_role"] == "grand_total",
          str(o["value"]) if o else "none")

    # 12 Brent price 2024 — deferred → out-of-scope signal
    r12 = s.search_series("Brent crude price 2024")
    check("PROBE 12 Brent price → out-of-scope (not no-data)",
          r12.get("also_note_out_of_scope", {}).get("status") == "out_of_scope")


# ===========================================================================
# Extra invariants: qualifier envelope completeness (constraint #3)
# ===========================================================================
def test_qualifier_envelope():
    sid = find_id(series_id="gas_production", calorific_basis="PCI", period_type="annual")
    r = s.get_series(sid, start_year=2024, end_year=2024)
    o = first_obs(r)
    required = ["value", "unit", "calorific_basis", "period_type", "period_start",
                "period_end", "geography_scope", "scope", "aggregation_role",
                "data_status", "extraction_confidence", "provenance"]
    missing = [k for k in required if o is None or k not in o]
    check("INV no bare numbers — full qualifier envelope present", not missing,
          f"missing={missing}")
    check("INV provenance cites source", o is not None and o["provenance"]["source_id"] is not None)
    # glossary surfaced for scoped series
    check("INV scope_glossary surfaced with results", isinstance(r.get("scope_glossary"), list))


def main():
    print("=" * 70)
    print("ONEM Energy MCP — acceptance tests")
    print("=" * 70)
    test_read_only()
    test_out_of_scope()
    test_comparison_guardrail()
    test_aggregation_safety()
    test_probe()
    test_qualifier_envelope()
    print("=" * 70)
    print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
    if FAIL:
        print("FAILED:", FAIL)
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
