"""
eval/report.py — per-failure-mode scoring & report generator.

The HEADLINE is the per-failure-mode breakdown, NOT a single global accuracy number
(a global average lets a dangerous mode hide behind trivial lookups — CLAUDE.md-aligned
scoring discipline). Layers contribute as:
  • Layer 1 — deterministic pass rates per check, bucketed by the failure mode each check targets
  • Layer 3 — pass/fail gates; ANY gating failure => FIX-FIRST
  • Layer 2 — per-category trajectory mean pass rates (stochastic; mean over N runs)

Run the whole suite and write eval_report.md + eval_results.json:
    python -m eval.report                 # L1 + L3 (deterministic); L2 if a key is present
    python -m eval.report --runs 3        # also run L2 with 3 runs/task
    python -m eval.report --no-layer2     # skip L2 explicitly
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict

from eval import layer1_retrieval, layer3_adversarial

_HERE = os.path.dirname(os.path.abspath(__file__))


def _bucket_by_mode(l1_results, l3_results):
    """Aggregate deterministic checks (L1 + L3) into per-failure-mode pass rates."""
    agg = defaultdict(lambda: {"passed": 0, "total": 0, "layers": set()})
    for r in l1_results:
        for m in r["modes"]:
            agg[m]["total"] += 1
            agg[m]["passed"] += int(r["passed"])
            agg[m]["layers"].add(1)
    for r in l3_results:
        for m in r["modes"]:
            agg[m]["total"] += 1
            agg[m]["passed"] += int(r["passed"])
            agg[m]["layers"].add(3)
    out = {}
    for m, v in agg.items():
        out[m] = {"passed": v["passed"], "total": v["total"],
                  "pass_rate": round(v["passed"] / v["total"], 3) if v["total"] else 0.0,
                  "layers": sorted(v["layers"])}
    return out


def _load_saved_layer2():
    p = os.path.join(_HERE, "layer2_results.json")
    if not os.path.exists(p):
        return None
    return json.load(open(p, encoding="utf-8"))


def build(runs: int = 0, model: str = "claude-opus-4-8", do_layer2: bool = True,
          use_saved_layer2: bool = False) -> dict:
    l1 = layer1_retrieval.run()
    l3 = layer3_adversarial.run()
    l1s = layer1_retrieval.summarize(l1)
    l3s = layer3_adversarial.summarize(l3)
    per_mode = _bucket_by_mode(l1, l3)

    l2 = None
    l2_status = "skipped"
    if use_saved_layer2:
        l2 = _load_saved_layer2()
        if l2:
            l2_status = (f"loaded saved run ({l2.get('backend', '?')}, {l2.get('model')}, "
                         f"{l2.get('runs')} runs/task" + (", re-scored" if l2.get('rescored') else "") + ")")
            for cat, v in l2["by_category"].items():
                per_mode.setdefault(cat, {"passed": 0, "total": 0, "pass_rate": None, "layers": []})
                per_mode[cat]["layer2_mean"] = v["mean"]
                if 2 not in per_mode[cat].get("layers", []):
                    per_mode[cat].setdefault("layers", []).append(2)
        else:
            l2_status = "no saved layer2_results.json found"
    elif do_layer2 and runs > 0:
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        has_aws = bool(os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE")
                       or os.path.exists(os.path.expanduser("~/.aws/credentials")))
        if not (has_key or has_aws):
            l2_status = "unavailable (no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN, no AWS creds)"
        else:
            try:
                from eval import layer2_behavioral
                # model=None → backend default (claude-opus-4-8 direct, or us.anthropic.claude-opus-4-8 on Bedrock)
                l2 = layer2_behavioral.run(runs=runs, model=(model if model != "claude-opus-4-8" else None))
                l2_status = f"ran ({l2['backend']}, {l2['model']})"
                # fold L2 category means into the per-mode headline (kept distinct via 'layer2_mean')
                for cat, v in l2["by_category"].items():
                    per_mode.setdefault(cat, {"passed": 0, "total": 0, "pass_rate": None, "layers": []})
                    per_mode[cat]["layer2_mean"] = v["mean"]
                    per_mode[cat].setdefault("layers", [])
                    if 2 not in per_mode[cat]["layers"]:
                        per_mode[cat]["layers"].append(2)
            except Exception as e:
                l2_status = f"error: {e}"

    verdict = "GO"
    if l3s["gating_failed"] > 0:
        verdict = "FIX-FIRST"

    return {
        "version": json.load(open(os.path.join(_HERE, "golden_set.json"), encoding="utf-8"))["version"],
        "layer1": l1s, "layer3": l3s, "layer3_detail": l3,
        "layer2_status": l2_status, "layer2": (l2 and {"model": l2["model"], "runs": l2["runs"], "by_category": l2["by_category"], "per_task": [{k: pt[k] for k in ("task_id", "category", "mean_pass", "variance")} for pt in l2["per_task"]]}),
        "per_failure_mode": per_mode,
        "verdict": verdict,
    }


def to_markdown(rep: dict) -> str:
    L = []
    L.append("# ONEM MCP — Evaluation Report")
    L.append("")
    L.append(f"- **Golden set version:** {rep['version']}")
    L.append(f"- **Overall verdict:** **{rep['verdict']}**  "
             "(FIX-FIRST iff any Layer-3 gate fails)")
    L.append(f"- **Layer 1 (retrieval fidelity):** {rep['layer1']['passed']}/{rep['layer1']['checks']} "
             f"checks ({rep['layer1']['pass_rate']*100:.1f}%)")
    L.append(f"- **Layer 3 (adversarial gates):** {rep['layer3']['passed']}/{rep['layer3']['checks']} pass, "
             f"{rep['layer3']['gating_failed']} gating failure(s) {rep['layer3']['gate_failures'] or ''}")
    L.append(f"- **Layer 2 (behavioral, model-in-loop):** {rep['layer2_status']}")
    L.append("")
    L.append("> Headline is the per-failure-mode table below — NOT a single global accuracy "
             "number (which would let a dangerous mode average out against trivial lookups).")
    L.append("")
    L.append("## Per-failure-mode breakdown")
    L.append("")
    L.append("| failure mode | deterministic (L1+L3) | layer-2 mean | layers |")
    L.append("|---|---|---|---|")
    for mode in sorted(rep["per_failure_mode"]):
        v = rep["per_failure_mode"][mode]
        det = (f"{v['passed']}/{v['total']} ({v['pass_rate']*100:.0f}%)"
               if v.get("total") else "—")
        l2m = f"{v['layer2_mean']*100:.0f}%" if v.get("layer2_mean") is not None else "—"
        L.append(f"| {mode} | {det} | {l2m} | {','.join(map(str, v.get('layers', [])))} |")
    L.append("")
    L.append("## Layer 3 — adversarial gates (detail)")
    L.append("")
    L.append("| probe | category | gate | result | note |")
    L.append("|---|---|---|---|---|")
    for r in rep["layer3_detail"]:
        L.append(f"| {r['task_id']} | {r['category']} | {'GATE' if r['gating'] else ''} | "
                 f"{'PASS' if r['passed'] else 'FAIL'} | {r['note']} |")
    L.append("")
    if rep.get("layer2"):
        L.append("## Layer 2 — per-task trajectory scores")
        L.append("")
        L.append(f"Model: `{rep['layer2']['model']}`, {rep['layer2']['runs']} run(s)/task. "
                 "Mean pass over runs (stochastic).")
        L.append("")
        L.append("| task | category | mean pass | variance |")
        L.append("|---|---|---|---|")
        for pt in rep["layer2"]["per_task"]:
            L.append(f"| {pt['task_id']} | {pt['category']} | {pt['mean_pass']:.2f} | {pt['variance']:.3f} |")
        L.append("")
    L.append("## How to read this")
    L.append("")
    L.append("- **Any Layer-3 gate FAIL → FIX-FIRST.** Those are the category errors the design "
             "claims to prevent; a regression there means a model can be led to a wrong/uncaught answer.")
    L.append("- **Layer 1** is value+qualifier fidelity vs the clean views. A drop here means the "
             "interface is corrupting or stripping what the store holds.")
    L.append("- **Layer 2** is the consuming-model's *behavior*; read per category, mind the variance. "
             "It is the lower-trust layer (a stochastic model + soft checks) — weight L1/L3 first.")
    L.append("- A *value* that looks wrong is an audit carry-forward, NOT an MCP defect "
             "(this eval grounds on the DB, not the source PDFs).")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=0, help="Layer-2 runs per task (0 = skip L2)")
    ap.add_argument("--model", default="claude-opus-4-8")
    ap.add_argument("--no-layer2", action="store_true")
    ap.add_argument("--use-saved-layer2", action="store_true",
                    help="fold in eval/layer2_results.json instead of re-driving the model")
    ap.add_argument("--md", default=os.path.join(_HERE, "eval_report.md"))
    ap.add_argument("--json", default=os.path.join(_HERE, "eval_results.json"))
    args = ap.parse_args()

    rep = build(runs=args.runs, model=args.model, do_layer2=not args.no_layer2,
                use_saved_layer2=args.use_saved_layer2)
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, ensure_ascii=False)
    md = to_markdown(rep)
    with open(args.md, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print(md)
    print(f"\n[written] {args.md}\n[written] {args.json}")
