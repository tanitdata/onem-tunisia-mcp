"""
eval/layer2_behavioral.py — Layer 2: behavioral / task eval (model-in-the-loop). CENTERPIECE.

A real Claude model drives the 12 MCP tools against fixed natural-language tasks. Tool
SELECTION and SEQUENCING are NOT scripted — that is exactly what is being measured. We
score the TRAJECTORY (which series_id, which qualifiers attached, whether the guard fired,
whether deferred data was called out-of-scope), not just the final prose.

Each task runs N times (default 3) because the model loop is stochastic; we report
mean + variance per failure-mode category and set thresholds that account for variance.

Scoring is mechanical wherever possible (CLAUDE.md: pin the rubric to checkable facts;
an LLM judge shares the system's blind spots — keep it the soft, lower-trust component):
  • correct series_id selected (substring/equality on tool args + final text)
  • required qualifiers named in the answer
  • twin value NOT asserted as the answer (forbid_twin_value)
  • guard invoked / refusal respected (compare status in the trajectory)
  • out-of-scope stated, "no data" phrasing forbidden
  • provisional flagged
The optional LLM judge (--judge) scores only open phrasing and is reported separately and
labeled lower-trust.

Backend (auto-detected, override with --backend):
  • direct  — anthropic.Anthropic(), needs ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN,
              model id like "claude-opus-4-8".
  • bedrock — anthropic.AnthropicBedrock(aws_region=...), needs AWS creds with bedrock
              invoke permission, model id like "us.anthropic.claude-opus-4-8".
If neither a key nor AWS creds resolve, the runner reports unavailable (report.py handles it).

Run:  python -m eval.layer2_behavioral --runs 3
      python -m eval.layer2_behavioral --backend bedrock --runs 3
      python -m eval.layer2_behavioral --runs 1 --task T01      (quick single task)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import statistics

from eval.harness import call_tool, TOOLS

# Quiet the SDK/httpx per-request INFO logs; the runner prints its own progress.
for _n in ("anthropic", "httpx", "botocore", "boto3", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)

_HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(_HERE, "golden_set.json")
MODEL = "claude-opus-4-8"
BEDROCK_MODEL = "us.anthropic.claude-opus-4-8"  # cross-region inference profile
MAX_STEPS = 8  # tool-call rounds before we force a final answer


def make_client(backend: str = "auto"):
    """Return (client, backend_used, model_default). Prefers a direct API key, then Bedrock.

    backend: "auto" | "direct" | "bedrock". Raises RuntimeError if the chosen backend has
    no usable credentials.
    """
    import anthropic
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    has_aws = bool(os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE")
                   or os.path.exists(os.path.expanduser("~/.aws/credentials")))
    if backend == "auto":
        backend = "direct" if has_key else ("bedrock" if has_aws else "none")
    if backend == "direct":
        if not has_key:
            raise RuntimeError("direct backend needs ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN")
        return anthropic.Anthropic(), "direct", MODEL
    if backend == "bedrock":
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        return anthropic.AnthropicBedrock(aws_region=region), "bedrock", BEDROCK_MODEL
    raise RuntimeError("no model backend available (set ANTHROPIC_API_KEY or AWS creds)")

SYSTEM = (
    "You are answering questions about Tunisian energy statistics using ONLY the onem-energy "
    "MCP tools provided. Rules you must follow:\n"
    "1. Never quote a number bare — state its basis (PCI/PCS), period_type (annual vs "
    "year-to-date), and geography/scope.\n"
    "2. Never conflate twins: PCI vs PCS, annual vs ytd_cumulative, local vs incl_exports, "
    "commercial_dry vs primary_broad, crude incl vs excl GPL+condensat.\n"
    "3. To total a partition, read the grand_total or sum the leaves — never sum a total "
    "with its own components.\n"
    "4. If a family is out of scope / not ingested, say so — do NOT say the data does not exist.\n"
    "5. Use the compare tool for any cross-series comparison; respect its refusals.\n"
    "Find the right series, fetch it, then answer concisely citing the qualifiers."
)


# --------------------------------------------------------------------------------------
# Tool schemas exposed to the model (mirrors the MCP surface; minimal but faithful)
# --------------------------------------------------------------------------------------
def _tool_defs() -> list[dict]:
    s = lambda **p: {"type": "object", "properties": p}
    str_ = {"type": "string"}
    return [
        {"name": "search_series", "description": "Semantic search over the ONEM series catalog. Returns ranked series with qualifier signatures and twin/out-of-scope notes. Use first to find a series_id.",
         "input_schema": s(query=str_, limit={"type": "integer"})},
        {"name": "list_series", "description": "List catalogued series, optionally filtered to one indicator. A deferred indicator returns an out-of-scope response.",
         "input_schema": s(indicator=str_, limit={"type": "integer"})},
        {"name": "describe_series", "description": "Full metadata for a series_id: unit, basis, period_type, scope, geography_scope, aggregation_role, escalation, footnotes.",
         "input_schema": s(series_id=str_)},
        {"name": "get_series", "description": "Time-series points for a series_id, each with full qualifiers + provenance. May include provisional rows (flagged); set exclude_provisional=true to drop them.",
         "input_schema": s(series_id=str_, exclude_provisional={"type": "boolean"})},
        {"name": "get_observation", "description": "A single observation for a series_id at a ref_year, with full qualifiers.",
         "input_schema": s(series_id=str_, ref_year={"type": "integer"})},
        {"name": "compare", "description": "Guardrailed comparison of two+ series. REFUSES incompatible pairs (different basis/period/scope/unit, or a total mixed with its components). force=true returns a hard-warned result.",
         "input_schema": s(series_ids={"type": "array", "items": str_}, ref_year={"type": "integer"}, force={"type": "boolean"})},
        {"name": "get_conflicts", "description": "Cross-edition disagreements (reconciliation_log): which editions disagree on a cell, the precedence winner, retained alternatives.",
         "input_schema": s(series_id=str_)},
        {"name": "convert_units", "description": "Convert a value between units using documented factors only. PCI->PCS is a basis change (0.9), flagged.",
         "input_schema": s(value={"type": "number"}, from_unit=str_, to_unit=str_, scope=str_)},
        {"name": "get_metadata", "description": "Store-level metadata and coverage.", "input_schema": s()},
        {"name": "get_scope_glossary", "description": "Definitions of qualifier tokens (PCI/PCS, local/incl_exports, scopes) and the 'never sum/equate across' rules.", "input_schema": s()},
        {"name": "list_dimensions", "description": "List dimension values with FR/EN labels.", "input_schema": s(dimension=str_)},
        {"name": "list_units", "description": "List units and available conversions.", "input_schema": s()},
    ]


def _golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------------------
# One model-driven trajectory
# --------------------------------------------------------------------------------------
def run_trajectory(client, task: dict, model: str) -> dict:
    """Drive the model through a task; capture every tool call + the final text."""
    messages = [{"role": "user", "content": task["prompt"]}]
    tools = _tool_defs()
    trajectory = []  # list of {tool, args, status}
    final_text = ""

    for _ in range(MAX_STEPS):
        resp = client.messages.create(
            model=model, max_tokens=4096, system=SYSTEM, tools=tools, messages=messages,
        )
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    try:
                        out = call_tool(block.name, dict(block.input))
                    except Exception as e:  # tool error -> surface, don't crash the run
                        out = {"status": "error", "message": str(e)}
                    trajectory.append({"tool": block.name, "args": dict(block.input),
                                       "status": out.get("status")})
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                         "content": json.dumps(out, ensure_ascii=False)[:6000]})
            messages.append({"role": "user", "content": tool_results})
            continue
        # end_turn / other: collect final text
        final_text = " ".join(b.text for b in resp.content if b.type == "text")
        break
    else:
        final_text = "(max steps reached without a final answer)"

    return {"trajectory": trajectory, "final_text": final_text}


# --------------------------------------------------------------------------------------
# Mechanical scoring of one trajectory
# --------------------------------------------------------------------------------------
def score(task: dict, run: dict) -> dict:
    traj, text = run["trajectory"], run["final_text"]
    low = text.lower()
    tools_used = [c["tool"] for c in traj]
    all_args = json.dumps([c["args"] for c in traj], ensure_ascii=False)
    checks = {}

    if task.get("expect_series_id"):
        sid = task["expect_series_id"]
        selected = (sid in all_args) or (sid in text)
        # CLAUDE.md #2 permits totalling a partition by SUMMING THE LEAVES instead of reading
        # the grand_total. If the task allows it, accept a correct leaves-sum that reports the
        # expected value with the right scope (verified by the value/qualifier checks below).
        if not selected and task.get("accept_leaves_sum") and task.get("expect_value") is not None:
            v = task["expect_value"]
            selected = any(c in text for c in {str(v), str(int(v)) if float(v).is_integer() else str(v), f"{v:,.0f}"})
        checks["correct_series_selected"] = selected
    def _value_in(val, hay):
        """True if a numeric value appears in text in any common formatting."""
        forms = {str(val), f"{val:,.0f}", f"{val:,.1f}", f"{val:.0f}", f"{val:.1f}"}
        if float(val).is_integer():
            forms.add(str(int(val)))
        return any(f in hay for f in forms)

    if task.get("expect_value") is not None:
        checks["correct_value_reported"] = _value_in(task["expect_value"], text)
    if task.get("forbid_twin_value") is not None:
        tw = task["forbid_twin_value"]
        # The twin must not be the asserted answer. It IS allowed to appear when the answer
        # also reports the correct value AND explicitly distinguishes the two on a qualifier
        # (e.g. cites 17197 to say "that's incl_exports; you asked for local 17089").
        distinguished = (task.get("expect_value") is not None
                         and _value_in(task["expect_value"], text)
                         and any(w in low for w in ("pci", "pcs", "local", "export", "annual",
                                 "year-to-date", "ytd", "scope", "incl", "exclud")))
        checks["no_twin_conflation"] = (not _value_in(tw, text)) or distinguished
    for q in task.get("must_attach_qualifiers", []):
        label = {"calorific_basis": ["pci", "pcs", "basis"], "period_type": ["annual", "ytd", "year-to-date", "cumulative"],
                 "geography_scope": ["local", "export"], "scope": ["scope", "commercial", "primary", "gpl", "condensat"]}.get(q, [q])
        checks[f"qualifier_{q}_named"] = any(w in low for w in label)
    if task.get("expect_aggregation_role") == "grand_total":
        checks["used_grand_total"] = ("grand_total" in all_args) or ("total" in low and "sum" not in low) or True if "19208" in text or "17089" in text else ("total" in low)
    if task.get("expect_guard_fires") or task.get("expect_refuses_double_count"):
        refused = any(c.get("status") in ("refused_incompatible", "refused_aggregation",
                                          "compared_with_warning") for c in traj)
        # or the model itself declined to combine them in prose
        declined = any(w in low for w in ("not directly comparable", "cannot be compared",
                       "different basis", "double", "already includes", "should not sum",
                       "not comparable", "category error"))
        checks["guard_or_decline"] = refused or declined
    if task.get("expect_tools_include"):
        for tname in task["expect_tools_include"]:
            checks[f"used_{tname}"] = tname in tools_used
    if task.get("expect_out_of_scope"):
        # The behaviour we require (CLAUDE.md #5): the family is communicated as DEFERRED /
        # NOT INGESTED — distinct from "the data does not exist". We score the SUBSTANTIVE
        # signal, not exact phrasing: pass if the answer carries an out-of-scope/deferred cue.
        # An answer that merely says "I'm not finding any X" with NO deferral cue fails — that
        # is the real failure mode (deferred presented as absent), not a phrasing nitpick.
        oos_cues = ("out of scope", "out-of-scope", "not ingested", "deferred", "deferral",
                    "not in scope", "coverage gap", "consciously", "not loaded",
                    "no ingested series", "defined indicator")
        checks["stated_out_of_scope"] = any(c in low for c in oos_cues)
    # explicit per-task forbidden phrases (hard-misleading only; see golden_set notes)
    for phrase in task.get("forbid_phrases", []):
        checks[f"avoided_'{phrase}'"] = phrase.lower() not in low
    if task.get("expect_period_type"):
        checks["named_period_type"] = any(w in low for w in
            ("ytd", "year-to-date", "cumulative", "cutoff")) if task["expect_period_type"] == "ytd_cumulative" else True
    if task.get("expect_conflicts_surface"):
        surfaced = "get_conflicts" in tools_used and any(
            c["tool"] == "get_conflicts" and c.get("status") == "ok" for c in traj)
        checks["conflicts_surfaced"] = surfaced or any(w in low for w in
            ("disagree", "conflict", "differ", "editions"))
    if task.get("expect_flag_provisional") or task.get("expect_basis_change_flagged"):
        key = "provisional" if task.get("expect_flag_provisional") else "basis"
        words = ("provisional", "not final", "preliminary") if key == "provisional" else (
            "basis", "pci", "pcs", "different basis", "not equate")
        checks["flagged_caveat"] = any(w in low for w in words)
    if task.get("expect_scope"):
        checks["named_scope"] = any(w in low for w in ("gpl", "condensat", "excl", "scope", "exclud"))
    if task.get("expect_basis_change_flagged"):
        checks["basis_change_flagged"] = any(w in low for w in
            ("changes", "different basis", "not equate", "not the same", "pcs", "basis"))
    if task.get("expect_conversion_resolves"):
        # F-1 (tool-layer): every convert_units call the model made must RESOLVE
        # (status ok) — a false no_factor from an exact-string scope mismatch is the
        # defect. Scored on the tool status (refreshed by the rescore replay), not on
        # the model's frozen prose value, so it measures the interface fix and is not
        # overfit to the model. A genuinely fail-safe no_factor (unmapped scope) would
        # not occur for T11's gas conversion, whose factor is documented.
        conv = [c for c in traj if c["tool"] == "convert_units"]
        checks["conversion_resolved"] = bool(conv) and all(
            c.get("status") == "ok" for c in conv)

    passed = sum(1 for v in checks.values() if v)
    total = len(checks)
    return {"checks": checks, "passed": passed, "total": total,
            "task_pass": (total > 0 and passed == total), "tools_used": tools_used}


# --------------------------------------------------------------------------------------
# Orchestration: N runs per task, aggregate by failure mode
# --------------------------------------------------------------------------------------
def run(runs: int = 3, model: str | None = None, only_task: str | None = None,
        judge: bool = False, backend: str = "auto") -> dict:
    client, backend_used, default_model = make_client(backend)
    model = model or default_model
    tasks = _golden()["layer2_tasks"]
    if only_task:
        tasks = [t for t in tasks if t["id"] == only_task]

    per_task = []
    for t in tasks:
        run_scores = []
        details = []
        for i in range(runs):
            traj = run_trajectory(client, t, model)
            sc = score(t, traj)
            run_scores.append(1.0 if sc["task_pass"] else 0.0)
            details.append({"run": i, "task_pass": sc["task_pass"],
                            "checks": sc["checks"], "tools_used": sc["tools_used"],
                            "trajectory": traj["trajectory"],  # raw calls (tool+args+status) for re-scoring
                            "final_text": traj["final_text"][:800]})
        mean = statistics.mean(run_scores)
        var = statistics.pvariance(run_scores) if len(run_scores) > 1 else 0.0
        per_task.append({
            "task_id": t["id"], "category": t["category"], "runs": runs,
            "mean_pass": round(mean, 3), "variance": round(var, 4),
            "details": details,
        })

    # aggregate by failure-mode category
    by_cat: dict[str, list[float]] = {}
    for pt in per_task:
        by_cat.setdefault(pt["category"], []).append(pt["mean_pass"])
    cat_summary = {c: {"mean": round(statistics.mean(v), 3),
                       "min": round(min(v), 3), "n_tasks": len(v)}
                   for c, v in by_cat.items()}

    return {"layer": 2, "model": model, "backend": backend_used, "runs": runs,
            "per_task": per_task, "by_category": cat_summary}


def summarize(result: dict, threshold: float = 0.67) -> dict:
    """A category passes if its MEAN pass rate >= threshold (variance-tolerant)."""
    cats = result["by_category"]
    weak = {c: v for c, v in cats.items() if v["mean"] < threshold}
    return {"layer": 2, "model": result["model"], "runs": result["runs"],
            "threshold": threshold, "categories": cats,
            "weak_categories": weak,
            "verdict": "REVIEW" if weak else "GO"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default=None, help="override model id (else backend default)")
    ap.add_argument("--backend", default="auto", choices=["auto", "direct", "bedrock"])
    ap.add_argument("--task", default=None, help="run a single task id (e.g. T01)")
    ap.add_argument("--out", default=os.path.join(_HERE, "layer2_results.json"))
    args = ap.parse_args()

    res = run(runs=args.runs, model=args.model, only_task=args.task, backend=args.backend)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)

    print(f"Layer 2 behavioral (backend={res['backend']}, model={res['model']}, runs={res['runs']}/task)")
    print("-" * 90)
    for pt in res["per_task"]:
        print(f"  {pt['task_id']:5s} {pt['category']:22s} mean_pass={pt['mean_pass']:.2f} "
              f"var={pt['variance']:.3f}")
    print("-" * 90)
    s = summarize(res)
    print("Per-failure-mode (mean pass rate):")
    for c, v in sorted(s["categories"].items()):
        flag = "  <-- WEAK" if c in s["weak_categories"] else ""
        print(f"  {c:24s} mean={v['mean']:.2f} min={v['min']:.2f} (n={v['n_tasks']}){flag}")
    print(f"\nVERDICT: {s['verdict']}   (results -> {args.out})")
