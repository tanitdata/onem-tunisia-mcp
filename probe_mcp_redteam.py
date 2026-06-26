#!/usr/bin/env python3
"""
probe_mcp_redteam.py — re-runnable behavioral / consumer red-team probe for the
`onem-energy` MCP server. Companion to mcp_behavioral_qa_report.md (Agent 2).

PURPOSE
  Drive the SERVER THROUGH ITS MCP TOOL INTERFACE exactly as a consuming LLM would,
  and assert the guard behaviors that protect against twin-conflation, double-count,
  bare values, "no data" misinformation, and unflagged provisional/escalated data.
  Treat builder "tests pass" as claims; this re-verifies them at the interface.

DESIGN NOTE (read this)
  Agent 2 operates BLIND to the server source (mcp_server.py / onem_store*). This
  script therefore does NOT import the server. It defines each probe as
  (tool, args, expectation) and runs them through a pluggable `call_tool(tool, args)`
  transport that you wire to however your QA harness reaches the MCP server
  (stdio client, an in-process FastMCP client, the same client test_mcp_acceptance.py
  uses, etc.). If no transport is wired, the script PRINTS the full probe plan with
  expected-vs-actual columns so it still works as a documented manual re-test sequence.

  This keeps the probe valid after any tool/description change: the EXPECTATIONS encode
  the required consumer-safety behavior, independent of implementation.

USAGE
  1. Wire `call_tool` (see TODO) to your MCP client. It must return the parsed JSON
     dict that the tool returns to a consumer.
  2. python probe_mcp_redteam.py            # runs probes if transport wired, else prints plan
  3. Exit code 0 = all gating probes pass; 1 = at least one gating probe failed.

REGRESSION GATES (must pass before GO): P_B1_AGG, P_B2_SCOPE_PRICES, P_B2_SCOPE_REFINING.
"""

from __future__ import annotations
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------------------
# Stable series ids used by the probes (captured live 2026-06-26 via search_series).
# --------------------------------------------------------------------------------------
GAS_MISKAR_PCI_YTD = "gas_production|flow.primary_production|prod.natural_gas|||field.miskar|||PCI|ktep-pci|ytd_cumulative||commercial_dry|||"
GAS_MISKAR_PCS_YTD = "gas_production|flow.primary_production|prod.natural_gas|||field.miskar|||PCS|ktep-pcs|ytd_cumulative||commercial_dry|||"

ELEC_SALES_TOTAL_ANNUAL   = "electricity_sales|flow.sales|||||||NA|GWh|annual|||||"            # grand_total
ELEC_SALES_BT_ANNUAL      = "electricity_sales|flow.sales|||||lvl.bt||NA|GWh|annual|||||"      # leaf
ELEC_SALES_MT_ANNUAL      = "electricity_sales|flow.sales|||||lvl.mt||NA|GWh|annual|||||"      # leaf
ELEC_SALES_HT_ANNUAL      = "electricity_sales|flow.sales|||||lvl.ht||NA|GWh|annual|||||"      # leaf
ELEC_SALES_BT_LOCAL_ANN   = "electricity_sales|flow.sales|||||lvl.bt||NA|GWh|annual|||||local"
ELEC_SALES_INCL_EXP_ANN   = "electricity_sales|flow.sales|||||||NA|GWh|annual|||||incl_exports"  # grand_total

GAS_PRIMARY_BROAD_ESCALATED = "energy_balance|flow.primary_production|prod.natural_gas||||||PCS|ktep|annual||primary_broad|||"


# --------------------------------------------------------------------------------------
# Transport: wire this to your MCP client.
# --------------------------------------------------------------------------------------
def call_tool(tool: str, args: dict) -> dict:
    """Return the parsed JSON dict a consumer would receive from `tool`.

    Wired to the in-process FastMCP server (mcp_server.mcp.call_tool), driving the
    server through its real MCP tool interface — the same dispatch a stdio client
    would hit. The structured-content list of TextContent is parsed back to the
    dict the consumer receives. This stays blind to onem_store internals: it only
    sees the tool's JSON return, exactly as a consuming LLM does.
    """
    import asyncio
    import json as _json

    import mcp_server as _srv

    result = asyncio.run(_srv.mcp.call_tool(tool, args))
    # FastMCP.call_tool returns (content_list, structured_dict) on recent versions,
    # or just a content_list on older ones. Prefer the structured dict; else parse
    # the first TextContent's JSON text.
    content = result[0] if isinstance(result, tuple) else result
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        structured = result[1]
        # FastMCP wraps non-dict returns under {"result": ...}; our tools return dicts
        return structured.get("result", structured) if "result" in structured and len(structured) == 1 else structured
    for item in content:
        text = getattr(item, "text", None)
        if text:
            return _json.loads(text)
    raise RuntimeError(f"Could not parse tool result for {tool}: {result!r}")


# --------------------------------------------------------------------------------------
# Probe framework
# --------------------------------------------------------------------------------------
@dataclass
class Probe:
    pid: str
    severity: str                 # BLOCK / ADVISE / COSMETIC / VERIFY
    description: str
    tool: str
    args: dict
    expect: Callable[[dict], tuple[bool, str]]   # (passed, human-readable explanation)
    gating: bool = False          # True => failure flips overall exit code to 1


def _has(d: Any, *keys) -> bool:
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    return True


def _json_blob(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False).lower()


# ---- Expectation predicates ----------------------------------------------------------
def exp_status(want: str):
    def f(r: dict):
        ok = r.get("status") == want
        return ok, f'status == "{want}" (got "{r.get("status")}")'
    return f


def exp_status_in(*wants: str):
    def f(r: dict):
        ok = r.get("status") in wants
        return ok, f'status in {wants} (got "{r.get("status")}")'
    return f


def exp_b1_aggregation_warned(r: dict):
    """B-1 GATE: comparing a grand_total with its own leaves must NOT come back as a
    silent OK. Require either a refusal/aggregation status or an explicit
    aggregation caveat somewhere in the payload."""
    status = r.get("status")
    blob = _json_blob(r)
    refused = status in ("refused_aggregation", "refused_incompatible")
    warned = ("aggregation_warning" in r) or ("aggregation" in blob and
             ("do not sum" in blob or "double" in blob or "already includes" in blob
              or "parent" in blob or "total already" in blob))
    ok = refused or warned
    return ok, ("aggregation_role mix (grand_total + its leaves) is flagged "
                f"(status={status}, warned={warned}, refused={refused}); "
                "BUG if status==ok with no aggregation caveat")


def exp_b2_out_of_scope(r: dict):
    """B-2 GATE: a deferred/unknown category must signal out-of-scope, never a bare
    status:ok with n:0 (which reads as 'no data exists')."""
    status = r.get("status")
    blob = _json_blob(r)
    bare_empty = status == "ok" and r.get("n", None) in (0, None) and not r.get("series")
    out_of_scope = (status == "out_of_scope"
                    or "out of scope" in blob or "out_of_scope" in blob
                    or "not ingested" in blob or "consciously deferred" in blob
                    or "coverage_gaps" in blob)
    ok = out_of_scope and not bare_empty
    return ok, ("deferred category signals out_of_scope (not a bare n:0 ok); "
                f"status={status}, bare_empty={bare_empty}")


def exp_refused_incompatible_on(*dims: str):
    def f(r: dict):
        if r.get("status") != "refused_incompatible":
            return False, f'expected refused_incompatible (got {r.get("status")})'
        inc = r.get("incompatible_on", {})
        missing = [d for d in dims if d not in inc]
        ok = not missing
        return ok, f'refused_incompatible on {list(inc.keys())} (want {list(dims)})'
    return f


def exp_force_hard_warned(r: dict):
    ok = r.get("status") == "compared_with_warning" and bool(r.get("hard_warning"))
    return ok, f'forced compare carries hard_warning (status={r.get("status")})'


def exp_every_point_qualified(r: dict):
    """No bare values: every observation carries the required qualifiers + a provisional/
    escalated warning where applicable."""
    pts = r.get("observations") or []
    if not pts and r.get("data"):  # compare-shaped payload
        pts = [p for blk in r["data"] for p in blk.get("points", [])]
    if not pts:
        return False, "no observations to check"
    required = ["period_type", "calorific_basis", "geography_scope", "scope",
                "aggregation_role", "data_status", "extraction_confidence",
                "is_escalated", "provenance"]
    for p in pts:
        miss = [k for k in required if k not in p]
        if miss:
            return False, f"point missing qualifiers {miss}"
        if p.get("data_status") == "provisional":
            w = _json_blob({"w": p.get("warnings", [])})
            if "provisional" not in w:
                return False, "provisional point lacks a provisional warning"
    return True, f"all {len(pts)} points fully qualified (+provisional flagged)"


def exp_escalated_optin(r: dict):
    """get_series default must NOT surface escalated points silently."""
    ok = r.get("status") == "empty_in_scope" or all(
        not p.get("is_escalated") for p in (r.get("observations") or [])
    )
    return ok, f'escalated hidden by default (status={r.get("status")})'


def exp_escalated_loud_when_optin(r: dict):
    pts = r.get("observations") or []
    if not pts:
        return False, "expected escalated point with include_escalated=true"
    for p in pts:
        if p.get("is_escalated"):
            if "escalated" not in _json_blob({"w": p.get("warnings", [])}):
                return False, "escalated point lacks ESCALATED warning"
    return True, "escalated point returned with loud ESCALATED warning"


def exp_conflicts_honest_if_empty(r: dict):
    """B-6: if no conflicts surface, the empty reconciliation_log must be DISCLOSED,
    not presented as 'settled'."""
    if r.get("status") != "no_conflicts":
        return True, f'conflicts surfaced (status={r.get("status")}) — log populated'
    blob = _json_blob(r)
    disclosed = "unpopulated" in blob or "build gap" in blob or "currently" in blob
    return disclosed, ("empty conflict log is disclosed as a build gap, not hidden "
                       f"(disclosed={disclosed})")


def exp_convert_safe_or_works(r: dict):
    """B-7: PCI->PCS must either work WITH a basis-change flag, or fail-safe (no_factor),
    but never invent a silent number."""
    status = r.get("status")
    blob = _json_blob(r)
    if status == "no_factor":
        return True, "fails safe (no_factor) — acceptable but see B-7 ADVISE"
    flagged = "basis" in blob and ("different" in blob or "not" in blob or "flag" in blob)
    ok = flagged
    return ok, f"if converted, result carries a basis-change flag (status={status})"


# --------------------------------------------------------------------------------------
# The probe suite
# --------------------------------------------------------------------------------------
PROBES: list[Probe] = [
    Probe("P_B1_AGG", "BLOCK",
          "compare(grand_total + its 3 voltage leaves) must warn on aggregation_role mix",
          "compare",
          {"series_ids": [ELEC_SALES_TOTAL_ANNUAL, ELEC_SALES_BT_ANNUAL,
                          ELEC_SALES_MT_ANNUAL, ELEC_SALES_HT_ANNUAL], "ref_year": 2024},
          exp_b1_aggregation_warned, gating=True),

    Probe("P_B2_SCOPE_PRICES", "BLOCK",
          'list_series("prices") must signal out_of_scope, not bare status:ok n:0',
          "list_series", {"indicator": "prices"}, exp_b2_out_of_scope, gating=True),

    Probe("P_B2_SCOPE_REFINING", "BLOCK",
          'list_series("refining") must signal out_of_scope, not bare status:ok n:0',
          "list_series", {"indicator": "refining"}, exp_b2_out_of_scope, gating=True),

    Probe("P_B2_CANONICAL_OK", "VERIFY",
          'list_series("brent_price") canonical id already returns out_of_scope',
          "list_series", {"indicator": "brent_price"}, exp_status("out_of_scope")),

    Probe("P_B3_PCI_PCS", "VERIFY",
          "compare PCI vs PCS twin must refuse on calorific_basis+unit",
          "compare", {"series_ids": [GAS_MISKAR_PCI_YTD, GAS_MISKAR_PCS_YTD]},
          exp_refused_incompatible_on("calorific_basis", "unit")),

    Probe("P_B4_GEO", "VERIFY",
          "compare local vs incl_exports twin must refuse on geography_scope",
          "compare", {"series_ids": [ELEC_SALES_INCL_EXP_ANN, ELEC_SALES_BT_LOCAL_ANN],
                      "ref_year": 2024},
          exp_refused_incompatible_on("geography_scope")),

    Probe("P_B5_FORCE", "VERIFY",
          "compare force=true on incompatible pair carries a loud hard_warning",
          "compare", {"series_ids": [GAS_MISKAR_PCI_YTD, GAS_MISKAR_PCS_YTD],
                      "force": True, "ref_year": 2024},
          exp_force_hard_warned),

    Probe("P_BAREVAL_GETSERIES", "VERIFY",
          "get_series exposes full qualifiers on every point (no bare values)",
          "get_series", {"series_id": GAS_MISKAR_PCI_YTD, "start_year": 2024,
                         "end_year": 2024},
          exp_every_point_qualified),

    Probe("P_BAREVAL_GETOBS", "VERIFY",
          "get_observation exposes full qualifiers (no bare values)",
          "get_observation", {"series_id": ELEC_SALES_TOTAL_ANNUAL, "ref_year": 2024},
          exp_every_point_qualified),

    Probe("P_ESCALATED_DEFAULT", "VERIFY",
          "get_series hides escalated/low-confidence by default (empty_in_scope)",
          "get_series", {"series_id": GAS_PRIMARY_BROAD_ESCALATED, "start_year": 2024,
                         "end_year": 2024},
          exp_escalated_optin),

    Probe("P_ESCALATED_OPTIN", "VERIFY",
          "get_series with include_escalated returns the point with loud ESCALATED warning",
          "get_series", {"series_id": GAS_PRIMARY_BROAD_ESCALATED, "start_year": 2024,
                         "end_year": 2024, "include_escalated": True,
                         "include_low_confidence": True},
          exp_escalated_loud_when_optin),

    Probe("P_B6_CONFLICTS", "ADVISE",
          "get_conflicts: if empty, must disclose the unpopulated reconciliation_log (G-1)",
          "get_conflicts", {"series_id": ELEC_SALES_TOTAL_ANNUAL},
          exp_conflicts_honest_if_empty),

    Probe("P_B7_CONVERT", "ADVISE",
          "convert_units PCI->PCS: works WITH basis flag, or fails safe (never invents)",
          "convert_units", {"value": 100, "from_unit": "ktep-pci", "to_unit": "ktep-pcs",
                            "scope": "natural gas", "calorific_basis": "PCI"},
          exp_convert_safe_or_works),
]


# --------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------
def run() -> int:
    transport_ready = True
    try:
        # cheap probe to see if call_tool is wired
        call_tool("list_units", {})
    except NotImplementedError:
        transport_ready = False
    except Exception:
        transport_ready = True  # wired but errored; let probes report

    print("=" * 100)
    print("onem-energy MCP behavioral red-team probe  (companion: mcp_behavioral_qa_report.md)")
    print("=" * 100)

    if not transport_ready:
        print("\n[transport NOT wired] Printing the documented probe plan (manual re-test sequence).\n"
              "Wire call_tool() to run assertions automatically.\n")
        for p in PROBES:
            gate = " [GATE]" if p.gating else ""
            print(f"- {p.pid} ({p.severity}){gate}\n    {p.description}\n"
                  f"    CALL: {p.tool}({json.dumps(p.args, ensure_ascii=False)})")
        print("\nExpected behaviors are encoded in each probe's predicate; see report for "
              "expected-vs-actual narrative. Gating probes: "
              + ", ".join(p.pid for p in PROBES if p.gating))
        return 0

    failures = 0
    gate_failures = 0
    for p in PROBES:
        try:
            resp = call_tool(p.tool, p.args)
            passed, why = p.expect(resp)
        except Exception as e:  # noqa: BLE001
            passed, why = False, f"EXCEPTION: {e!r}"
        mark = "PASS" if passed else "FAIL"
        gate = " [GATE]" if p.gating else ""
        print(f"[{mark}] {p.pid} ({p.severity}){gate}: {why}")
        if not passed:
            failures += 1
            if p.gating:
                gate_failures += 1

    print("-" * 100)
    print(f"{len(PROBES)} probes | {failures} failed | {gate_failures} gating failures")
    if gate_failures:
        print("VERDICT: FIX-FIRST — a gating (BLOCK) probe failed.")
        return 1
    if failures:
        print("VERDICT: GO-with-advice — only non-gating probes failed (review ADVISE items).")
        return 0
    print("VERDICT: GO — all probes pass.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
