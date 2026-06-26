"""
eval/layer1_retrieval.py — Layer 1: retrieval fidelity (scripted, deterministic).

Question: does a value, with EVERY qualifier, survive the tool round-trip intact?
Ground truth = direct query on v_series_clean (NOT the source PDFs — CLAUDE.md scope
boundary; a DB-vs-source gap is the audit's job).

For each golden series: call get_series, locate the ref_year point, and assert
  • value matches the DB (no drift)
  • unit / calorific_basis / scope / geography_scope / aggregation_role match
  • the full qualifier envelope is present (no bare number)
Plus convert_units round-trips (ktep-pci <-> ktep-pcs and friends).

Deterministic — re-run on every server or description change. Emits per-check results
consumed by report.py; `python -m eval.layer1_retrieval` prints a summary.
"""
from __future__ import annotations

import json
import os

from eval.harness import (
    call_tool, db_one, iter_points, missing_qualifiers, approx, REQUIRED_QUALIFIERS,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(_HERE, "golden_set.json")


def _golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def _point_for_year(resp: dict, ref_year: int) -> dict | None:
    for p in iter_points(resp):
        if p.get("ref_year") == ref_year:
            return p
    return None


def _check(results, task_id, modes, name, passed, detail=""):
    results.append({
        "layer": 1, "task_id": task_id, "modes": modes,
        "check": name, "passed": bool(passed), "detail": detail,
    })


def run() -> list[dict]:
    g = _golden()
    results: list[dict] = []

    # ---- retrieval fidelity ---------------------------------------------------------
    for t in g["layer1_retrieval"]:
        sid, yr, modes = t["series_id"], t["ref_year"], t["modes"]

        # ground truth straight from the clean view
        gt = db_one(
            "SELECT value, unit_id, calorific_basis, period_type, aggregation_role, "
            "geography_scope, scope, data_status FROM v_series_clean "
            "WHERE series_key = ? AND ref_year = ? LIMIT 1", [sid, yr])
        if gt is None:
            _check(results, t["id"], modes, "db_has_groundtruth", False,
                   f"no clean-view row for {sid} @ {yr}")
            continue
        gv, gunit, gbasis, gperiod, grole, ggeo, gscope, gstatus = gt

        resp = call_tool("get_series", {"series_id": sid})
        if resp.get("status") != "ok":
            _check(results, t["id"], modes, "tool_returns_ok", False,
                   f"status={resp.get('status')}")
            continue
        pt = _point_for_year(resp, yr)
        if pt is None:
            _check(results, t["id"], modes, "point_present", False,
                   f"no point for ref_year={yr}")
            continue

        # value fidelity (vs DB and vs reviewed golden)
        _check(results, t["id"], modes, "value_matches_db",
               approx(pt.get("value"), gv),
               f"tool={pt.get('value')} db={gv}")
        _check(results, t["id"], modes, "value_matches_golden",
               approx(pt.get("value"), t["expect_value"]),
               f"tool={pt.get('value')} golden={t['expect_value']}")

        # qualifier fidelity
        _check(results, t["id"], modes, "unit_matches",
               pt.get("unit") == gunit == t.get("expect_unit", gunit),
               f"tool={pt.get('unit')} db={gunit}")
        _check(results, t["id"], modes, "basis_matches",
               (pt.get("calorific_basis") == gbasis), f"tool={pt.get('calorific_basis')} db={gbasis}")
        _check(results, t["id"], modes, "period_type_matches",
               (pt.get("period_type") == gperiod), f"tool={pt.get('period_type')} db={gperiod}")
        _check(results, t["id"], modes, "aggregation_role_matches",
               (pt.get("aggregation_role") == grole), f"tool={pt.get('aggregation_role')} db={grole}")
        if t.get("expect_scope") is not None:
            _check(results, t["id"], modes, "scope_matches",
                   (pt.get("scope") == gscope == t["expect_scope"]),
                   f"tool={pt.get('scope')} db={gscope} golden={t['expect_scope']}")
        if t.get("expect_geography_scope") is not None:
            _check(results, t["id"], modes, "geography_scope_matches",
                   (pt.get("geography_scope") == ggeo == t["expect_geography_scope"]),
                   f"tool={pt.get('geography_scope')} db={ggeo}")

        # no bare number: full qualifier envelope present
        miss = missing_qualifiers(pt)
        _check(results, t["id"], modes + ["qualifier_drop"], "no_qualifier_dropped",
               not miss, f"missing={miss}" if miss else "all present")
        _check(results, t["id"], modes, "provenance_present",
               bool(pt.get("provenance")), "provenance " + ("present" if pt.get("provenance") else "ABSENT"))

    # ---- convert_units round-trips --------------------------------------------------
    for c in g["layer1_convert"]:
        args = {"value": c["value"], "from_unit": c["from_unit"], "to_unit": c["to_unit"]}
        # F-1: exercise the scope/carrier synonym normalization when a scope is given
        if c.get("scope") is not None:
            args["scope"] = c["scope"]
        resp = call_tool("convert_units", args)
        if c.get("expect_status") == "no_factor":
            _check(results, c["id"], c["modes"], "fails_closed_no_factor",
                   resp.get("status") == "no_factor", f"status={resp.get('status')}")
            continue
        _check(results, c["id"], c["modes"], "convert_ok",
               resp.get("status") == "ok", f"status={resp.get('status')}")
        _check(results, c["id"], c["modes"], "convert_value",
               approx(resp.get("value"), c["expect_value"]),
               f"tool={resp.get('value')} golden={c['expect_value']}")
        blob = json.dumps(resp).lower()
        has_warn = ("warning" in resp and bool(resp.get("warning"))) or "basis" in blob
        if c["expect_basis_warning"]:
            _check(results, c["id"], c["modes"], "basis_change_flagged",
                   has_warn, "warning " + ("present" if has_warn else "ABSENT"))

    return results


def summarize(results: list[dict]) -> dict:
    passed = sum(r["passed"] for r in results)
    return {"layer": 1, "checks": len(results), "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": round(passed / len(results), 4) if results else 0.0}


if __name__ == "__main__":
    res = run()
    for r in res:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{mark}] {r['task_id']:34s} {r['check']:28s} {r['detail']}")
    s = summarize(res)
    print("-" * 90)
    print(f"Layer 1 retrieval fidelity: {s['passed']}/{s['checks']} checks pass "
          f"({s['pass_rate']*100:.1f}%)")
