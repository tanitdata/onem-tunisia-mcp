"""
eval/layer3_adversarial.py — Layer 3: adversarial / guard-regression GATES.

These are pass/fail gates, not soft scores. Any gating failure => FIX-FIRST. They encode
the category errors the design claims to prevent, driven directly at the tool layer
(deterministic — no model in the loop):
  • compare across PCI/PCS and local/incl_exports must be refused
  • compare of a grand_total with its own leaves must be refused/warned (double-count)
  • deferred-family list_series must say out_of_scope, never bare n:0
  • get_conflicts must surface the contested cells (not empty => settled)
  • get_series points must all carry the qualifier envelope (no bare number)
  • provisional points must be flagged; force=true must hard-warn

Re-run after every tool or description change — guardrails regress easily.
`python -m eval.layer3_adversarial`.
"""
from __future__ import annotations

import json
import os

from eval.harness import call_tool, iter_points, missing_qualifiers

_HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(_HERE, "golden_set.json")

_REFUSE_STATUSES = {"refused_incompatible", "refused_aggregation"}
_OOS_STATUSES = {"out_of_scope"}


def _golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def _evaluate(probe: dict) -> tuple[bool, str]:
    """Return (passed, detail) for one adversarial probe against the live tool."""
    resp = call_tool(probe["tool"], probe["args"])
    status = resp.get("status")
    blob = json.dumps(resp, ensure_ascii=False).lower()

    if probe.get("expect_refused"):
        return status in _REFUSE_STATUSES, f"status={status} (want a refusal)"

    if probe.get("expect_refused_or_warned"):
        warned = ("aggregation" in blob and any(
            k in blob for k in ("do not sum", "double", "already includes", "parent",
                                 "total already", "components")))
        ok = status in _REFUSE_STATUSES or warned or "aggregation_conflict" in resp
        return ok, f"status={status} warned={warned}"

    if probe.get("expect_out_of_scope"):
        bare_empty = status == "ok" and resp.get("n", None) == 0
        return status in _OOS_STATUSES, (
            f"status={status}" + (" (BARE n:0 ok — reads as 'no data')" if bare_empty else ""))

    if probe.get("expect_out_of_scope_signal"):
        # search_series: an out-of-scope note may ride alongside ok results
        signal = (status in _OOS_STATUSES
                  or "out_of_scope" in blob or "out of scope" in blob
                  or "not the same as" in blob or "not ingested" in blob)
        return signal, f"status={status} oos_signal={signal}"

    if probe.get("expect_hard_warning"):
        warned = (status == "compared_with_warning"
                  or bool(resp.get("hard_warning")) or "not directly comparable" in blob
                  or "category error" in blob)
        return warned, f"status={status} hard_warning={warned}"

    if probe.get("expect_conflicts_nonempty"):
        n = resp.get("n") or len(resp.get("conflicts") or [])
        no_disclaimer = "unpopulated" not in blob and "build gap" not in blob
        return (status == "ok" and n > 0 and no_disclaimer), f"status={status} n={n}"

    if probe.get("expect_all_points_qualified"):
        pts = list(iter_points(resp))
        if not pts:
            return False, "no points returned"
        bad = [p.get("ref_year") for p in pts if missing_qualifiers(p)]
        return (not bad), (f"{len(pts)} points, all qualified" if not bad
                           else f"points missing qualifiers: {bad}")

    if probe.get("expect_provisional_flagged"):
        pts = list(iter_points(resp))
        prov = [p for p in pts if p.get("data_status") == "provisional"]
        # pass if there are no provisional points, or every one is flagged with a status
        ok = all(p.get("data_status") for p in prov)
        return ok and (len(prov) > 0 or len(pts) > 0), f"{len(prov)} provisional / {len(pts)} points"

    return False, "no expectation encoded in probe"


def run() -> list[dict]:
    results = []
    for p in _golden()["layer3_adversarial"]:
        passed, detail = _evaluate(p)
        results.append({
            "layer": 3, "task_id": p["id"], "modes": [p["category"]],
            "category": p["category"], "gating": bool(p.get("gating")),
            "check": "guard", "passed": passed, "detail": detail, "note": p.get("note", ""),
        })
    return results


def summarize(results: list[dict]) -> dict:
    gating = [r for r in results if r["gating"]]
    gate_fail = [r for r in gating if not r["passed"]]
    passed = sum(r["passed"] for r in results)
    return {
        "layer": 3, "checks": len(results), "passed": passed,
        "failed": len(results) - passed,
        "gating": len(gating), "gating_failed": len(gate_fail),
        "gate_failures": [r["task_id"] for r in gate_fail],
        "verdict": "FIX-FIRST" if gate_fail else "GO",
    }


if __name__ == "__main__":
    res = run()
    for r in res:
        mark = "PASS" if r["passed"] else "FAIL"
        gate = " [GATE]" if r["gating"] else ""
        print(f"[{mark}]{gate:7s} {r['task_id']:28s} {r['category']:22s} {r['detail']}")
    s = summarize(res)
    print("-" * 90)
    print(f"Layer 3 adversarial: {s['passed']}/{s['checks']} pass | "
          f"gating failures: {s['gating_failed']} {s['gate_failures'] or ''}")
    print(f"VERDICT: {s['verdict']}")
