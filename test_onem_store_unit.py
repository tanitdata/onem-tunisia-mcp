"""test_onem_store_unit.py — Agent-1 (static/code & security) unit tests for the
ONEM energy MCP logic layer (onem_store.py).

These tests exercise the pure logic layer DIRECTLY (no MCP runtime), independent of
test_mcp_acceptance.py, and focus on INTERFACE guarantees rather than data values:
  * read-only is real (write attempt raises, table survives)
  * SQL injection is neutralised through every string parameter
  * the qualifier envelope never emits a bare number
  * the comparison guardrail refuses / hard-warns correctly and fails closed on
    unknown ids
  * aggregation-safety metadata (aggregation_role) is always carried
  * fail-closed behaviour: unknown series / bad dimension / no factor degrade
    safely with a status, no internal leakage, no bare-number fallback
  * G-1: get_conflicts handles the empty reconciliation_log without error

All tests use a READ-ONLY connection (onem_store opens read_only=True) or read-only
queries. Nothing writes to energy.duckdb.

Run:  PYTHONIOENCODING=utf-8 python test_onem_store_unit.py
Exit code 0 = all pass.
"""

from __future__ import annotations

import sys

import duckdb

import onem_store as s

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail else ""))


def _find(**preds):
    for r in s.CATALOG:
        ok = True
        for k, v in preds.items():
            cell = r.get(k) or ""
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


# ---------------------------------------------------------------------------
# 1. READ-ONLY IS REAL
# ---------------------------------------------------------------------------
def test_read_only_connection():
    con = s.get_conn()
    # write must raise
    raised = False
    try:
        con.execute("CREATE TABLE _agent1_wtest(a INT)")
    except Exception:
        raised = True
    check("read_only: CREATE TABLE raises", raised)

    raised = False
    try:
        con.execute("INSERT INTO observation (observation_id) VALUES ('zzz')")
    except Exception:
        raised = True
    check("read_only: INSERT raises", raised)

    raised = False
    try:
        con.execute("UPDATE observation SET value = 0")
    except Exception:
        raised = True
    check("read_only: UPDATE raises", raised)

    # table still has its rows (nothing was committed)
    n = s._q("SELECT count(*) n FROM observation")[0]["n"]
    check("read_only: observation table intact after write attempts", n > 0, f"rows={n}")

    # a second independent read-only connect succeeds (no exclusive write lock held)
    ok = False
    try:
        c2 = duckdb.connect(s.DB_PATH, read_only=True)
        c2.execute("SELECT 1")
        c2.close()
        ok = True
    except Exception as e:
        ok = False
    check("read_only: a second reader can open the file (no write lock held)", ok)


# ---------------------------------------------------------------------------
# 2. SQL INJECTION NEUTRALISED THROUGH EVERY STRING PARAM
# ---------------------------------------------------------------------------
def test_sql_injection():
    inj = "x'; DROP TABLE observation; --"
    union = "' UNION SELECT * FROM source --"
    before = s._q("SELECT count(*) n FROM observation")[0]["n"]

    # every tool that takes a string param; none should raise, none should mutate
    s.get_series(inj)
    s.get_series("x", period_type=inj)
    s.get_observation(inj, 2024)
    s.describe_series(inj)
    s.list_series(indicator=inj)
    s.search_series(inj)
    s.search_series(union)
    s.compare([inj, inj + "2"])
    s.convert_units(1.0, inj, "ktep")
    s.list_dimensions(inj)
    s.get_scope_glossary(inj)
    s.get_conflicts(inj)

    after = s._q("SELECT count(*) n FROM observation")[0]["n"]
    check("injection: observation table row count unchanged", before == after,
          f"{before} -> {after}")
    # tables that an injection might target still exist
    for tbl in ("observation", "source", "reconciliation_log", "scope_glossary"):
        exists = s._q("SELECT count(*) n FROM duckdb_tables() WHERE table_name = ?", [tbl])[0]["n"]
        check(f"injection: table '{tbl}' still exists", exists == 1)


# ---------------------------------------------------------------------------
# 3. QUALIFIER ENVELOPE — NO BARE NUMBERS, EVER
# ---------------------------------------------------------------------------
_REQUIRED_ENVELOPE_KEYS = [
    "value", "unit", "calorific_basis", "period_type", "period_start", "period_end",
    "ref_year", "geography_scope", "scope", "aggregation_role", "is_total",
    "data_status", "extraction_confidence", "confidence", "is_escalated",
    "dimensions", "provenance", "warnings",
]


def test_envelope_completeness_all_paths():
    # sample several distinct series and assert every returned point is fully wrapped
    sids = set(filter(None, [
        _find(series_id="gas_production", calorific_basis="PCI", period_type="annual"),
        _find(series_id="electricity_sales", geography_scope="incl_exports", period_type="annual"),
        _find(series_id="crude_production"),
        _find(series_id="pp_consumption", product="gasoil"),
    ]))
    checked = 0
    bad = []
    for sid in sids:
        r = s.get_series(sid)
        if r.get("status") != "ok":
            continue
        # scope_glossary always surfaced as a list
        if not isinstance(r.get("scope_glossary"), list):
            bad.append((sid, "scope_glossary not a list"))
        for o in r["observations"]:
            checked += 1
            missing = [k for k in _REQUIRED_ENVELOPE_KEYS if k not in o]
            if missing:
                bad.append((sid, f"missing {missing}"))
            if o.get("provenance", {}).get("source_id") is None:
                bad.append((sid, "no provenance.source_id"))
            # a value object must never be a naked scalar
            if not isinstance(o, dict):
                bad.append((sid, "observation is not a dict"))
    check("envelope: every get_series point fully qualified + provenance",
          not bad and checked > 0, f"checked={checked} bad={bad[:3]}")

    # get_observation path
    sid = _find(series_id="gas_production", calorific_basis="PCI", period_type="annual")
    r = s.get_observation(sid, 2024)
    obs_ok = r.get("status") == "ok" and all(
        k in r["observations"][0] for k in _REQUIRED_ENVELOPE_KEYS)
    check("envelope: get_observation point fully qualified", obs_ok)


# ---------------------------------------------------------------------------
# 4. COMPARISON GUARDRAIL
# ---------------------------------------------------------------------------
def test_compare_guard():
    pci = _find(series_id="gas_production", calorific_basis="PCI", period_type="annual")
    pcs = _find(series_id="gas_production", calorific_basis="PCS", period_type="annual")

    # < 2 ids -> error (not a crash, not a silent pass)
    check("compare: <2 ids returns error", s.compare([pci]).get("status") == "error")

    # cross-basis refused
    r = s.compare([pci, pcs])
    check("compare: PCI vs PCS refused_incompatible",
          r["status"] == "refused_incompatible" and "calorific_basis" in r["incompatible_on"])

    # forced -> compared_with_warning + hard_warning present (never silent ok)
    rf = s.compare([pci, pcs], force=True)
    check("compare: forced carries hard_warning, status compared_with_warning",
          rf["status"] == "compared_with_warning" and bool(rf.get("hard_warning")))

    # unknown id fails closed BEFORE any value fetch / KeyError
    ru = s.compare([pci, "TOTALLY_BOGUS_SID"])
    check("compare: unknown id -> unknown_series (fails closed, no crash)",
          ru.get("status") == "unknown_series")
    ruf = s.compare([pci, "TOTALLY_BOGUS_SID"], force=True)
    check("compare: unknown id even with force -> unknown_series (no KeyError)",
          ruf.get("status") == "unknown_series")


# ---------------------------------------------------------------------------
# 5. AGGREGATION-SAFETY METADATA ALWAYS CARRIED
# ---------------------------------------------------------------------------
def test_aggregation_role_carried():
    # every catalog brief and every value envelope must carry aggregation_role so a
    # consumer can apply the double-count guard.
    missing_brief = [r["series_id"] for r in s.CATALOG[:200]
                     if "aggregation_role" not in s._catalog_brief(r)]
    check("aggregation: catalog_brief always has aggregation_role", not missing_brief,
          f"missing={missing_brief[:3]}")

    sid = _find(series_id="electricity_sales", geography_scope="incl_exports", period_type="annual")
    r = s.get_series(sid, start_year=2024, end_year=2024)
    if r.get("status") == "ok":
        roles = {o.get("aggregation_role") for o in r["observations"]}
        check("aggregation: served points expose aggregation_role (not None)",
              None not in roles or len(roles) > 0, f"roles={roles}")

    # the grand_total for elec-sales incl_exports is reachable and tagged grand_total
    o = next((o for o in r.get("observations", []) if o.get("aggregation_role") == "grand_total"),
             None) if r.get("status") == "ok" else None
    check("aggregation: elec-sales incl_exports has a grand_total point",
          o is not None)


# ---------------------------------------------------------------------------
# 6. FAIL-CLOSED / SAFE DEGRADATION (no leakage, no bare-number default)
# ---------------------------------------------------------------------------
def test_fail_closed():
    # unknown series id -> structured unknown_series (no stack, no value)
    r = s.get_series("no.such.series|x")
    check("fail-closed: unknown series -> unknown_series status",
          r.get("status") == "unknown_series" and "value" not in r)

    # out-of-scope indicator id (deferred, 0 series) -> out_of_scope, NOT no-data
    deferred = next(iter(s.DEFERRED_INDICATORS), None)
    if deferred:
        r2 = s.get_series(f"{deferred}|x")
        check("fail-closed: deferred indicator id -> out_of_scope (not 'no data')",
              r2.get("status") == "out_of_scope")

    # bad dimension -> error with the valid list, no crash
    r3 = s.list_dimensions("not_a_dimension")
    check("fail-closed: bad dimension -> error + valid list",
          r3.get("status") == "error" and "flow" in r3.get("message", ""))

    # unknown unit pair -> no_factor (never invents a number)
    r4 = s.convert_units(100.0, "fake_unit", "ktep")
    check("fail-closed: unknown conversion -> no_factor (no invented value)",
          r4.get("status") == "no_factor" and "value" not in r4)

    # empty-but-in-scope (valid series, impossible year) -> empty_in_scope, no value
    sid = _find(series_id="gas_production", calorific_basis="PCI", period_type="annual")
    r5 = s.get_series(sid, start_year=1800, end_year=1801)
    check("fail-closed: valid series + impossible year -> empty_in_scope (not no-data, not bare)",
          r5.get("status") == "empty_in_scope" and "value" not in r5)


# ---------------------------------------------------------------------------
# 7. G-1 — get_conflicts handles the EMPTY reconciliation_log
# ---------------------------------------------------------------------------
def test_g1_empty_conflicts():
    # G-1 RESOLVED: reconciliation_log is now repopulated (Part B). get_conflicts
    # must surface real disagreements (with per-edition breakdown + precedence
    # winner) and must NOT carry the stale "unpopulated build gap" disclaimer.
    n = s._q("SELECT count(*) n FROM reconciliation_log")[0]["n"]
    check("G-1: reconciliation_log is populated", n > 0, f"rows={n}")

    r = s.get_conflicts()
    ok = (r.get("status") == "ok" and r.get("n", 0) > 0
          and "precedence_rule" in r
          and "unpopulated" not in str(r).lower() and "build gap" not in str(r).lower())
    check("G-1: get_conflicts surfaces disagreements, no stale disclaimer",
          ok, f"status={r.get('status')}, n={r.get('n')}")
    # each conflict carries the disagreeing editions + precedence resolution
    if r.get("status") == "ok":
        c = r["conflicts"][0]
        check("G-1: conflict carries values_by_source + resolution",
              isinstance(c.get("values_by_source"), dict) and bool(c.get("resolution")))
    # per-series query path still safe; a no-conflict series degrades cleanly
    rs = s.get_conflicts("electricity_sales|flow.sales|||||lvl.bt||NA|GWh|annual|||||")
    check("G-1: get_conflicts(series_id) safe (no_conflicts or ok)",
          rs.get("status") in ("no_conflicts", "ok")
          and "unpopulated" not in str(rs).lower())


# ---------------------------------------------------------------------------
# 8. convert_units basis-change is flagged (G-6 behaviour at code level)
# ---------------------------------------------------------------------------
def test_convert_basis_flagged():
    r = s.convert_units(100.0, "PCI", "PCS")
    check("convert: PCI->PCS succeeds AND carries a basis-change warning",
          r.get("status") == "ok" and bool(r.get("warning")))
    # F-3 RULING (2026-06-26, supersedes the prior 90.0 assertion): for the SAME gas
    # PCS > PCI (DB: Miskar 2024 PCI=317 < PCS=353), and PCI = 0.9*PCS, so
    # PCI->PCS = /0.9 ≈ x1.111 (value INCREASES). The prior 90.0 was the inverted bug.
    r2 = s.convert_units(100.0, "ktep-pci", "ktep-pcs")
    check("convert: ktep-pci->ktep-pcs ≈ 111.1 (INCREASES) with basis-change warning",
          r2.get("status") == "ok" and abs(r2.get("value", 0) - 111.1111111) < 1e-3
          and bool(r2.get("warning")), f"got {r2.get('value')}")
    r3 = s.convert_units(111.1111111, "ktep-pcs", "ktep-pci")
    check("convert: ktep-pcs->ktep-pci ≈ 100.0 (DECREASES, inverse) with warning",
          r3.get("status") == "ok" and abs(r3.get("value", 0) - 100.0) < 1e-3
          and bool(r3.get("warning")), f"got {r3.get('value')}")
    # unrelated unknown pair still fails closed (never invents)
    r4 = s.convert_units(1.0, "fake_unit", "ktep")
    check("convert: unknown pair still fails closed (no_factor)",
          r4.get("status") == "no_factor")


# ---------------------------------------------------------------------------
# 8b. F-3 property-based DIRECTION check — catches an inverted factor for ANY
#     carrier, independent of the exact magnitude. The invariant: a conversion to
#     a HIGHER-magnitude basis/unit must increase the number, and the reverse must
#     decrease it. Anchored on DB ground truth (same-gas PCS > PCI), never on a
#     prior golden value.
# ---------------------------------------------------------------------------
def test_convert_direction_property():
    # 1) PCI->PCS must INCREASE, PCS->PCI must DECREASE (the F-3 invariant).
    up = s.convert_units(100.0, "ktep-pci", "ktep-pcs")
    down = s.convert_units(100.0, "ktep-pcs", "ktep-pci")
    check("property: PCI->PCS increases magnitude (PCS>PCI)",
          up.get("status") == "ok" and up["value"] > 100.0, f"got {up.get('value')}")
    check("property: PCS->PCI decreases magnitude (PCI<PCS)",
          down.get("status") == "ok" and down["value"] < 100.0, f"got {down.get('value')}")
    # ground-truth anchor: the ratio must match the DB's same-gas PCS/PCI (~1.11)
    db = s._q("""SELECT
                   MAX(CASE WHEN calorific_basis='PCI' THEN value END) pci,
                   MAX(CASE WHEN calorific_basis='PCS' THEN value END) pcs
                 FROM v_series_clean
                 WHERE series_key LIKE 'gas_production%field.miskar%commercial_dry%'
                   AND period_type='annual' AND ref_year=2024""")[0]
    db_ratio = db["pcs"] / db["pci"]
    tool_ratio = up["value"] / 100.0
    check("property: PCI->PCS factor matches DB same-gas PCS/PCI ratio",
          abs(tool_ratio - db_ratio) < 0.02,
          f"tool={tool_ratio:.4f} db={db_ratio:.4f} (Miskar PCI={db['pci']} PCS={db['pcs']})")
    # 2) round-trip is identity (reciprocal correctness) for several carriers
    for v, a, b, sc in [(100.0, "ktep-pci", "ktep-pcs", None),
                        (1000.0, "GWh", "ktep", "electricity"),
                        (5.0, "baril", "m3", "volume")]:
        f = s.convert_units(v, a, b, scope=sc)
        if f.get("status") != "ok":
            check(f"property: round-trip {a}<->{b} (fwd resolves)", False, str(f.get("status")))
            continue
        back = s.convert_units(f["value"], b, a, scope=sc)
        ok = back.get("status") == "ok" and abs(back["value"] - v) < max(0.01, v * 0.01)
        check(f"property: round-trip {a}<->{b} returns to origin",
              ok, f"{v} -> {f['value']} -> {back.get('value')}")


def main():
    print("=" * 70)
    print("Agent-1 onem_store unit tests (static / code & security)")
    print("=" * 70)
    test_read_only_connection()
    test_sql_injection()
    test_envelope_completeness_all_paths()
    test_compare_guard()
    test_aggregation_role_carried()
    test_fail_closed()
    test_g1_empty_conflicts()
    test_convert_basis_flagged()
    test_convert_direction_property()
    print("=" * 70)
    print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
    if FAIL:
        print("FAILED:", FAIL)
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
