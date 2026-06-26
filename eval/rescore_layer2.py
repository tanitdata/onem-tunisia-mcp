"""
eval/rescore_layer2.py — re-score saved Layer-2 trajectories with the CURRENT scoring logic.

Layer 2 model runs are expensive (sequential Opus-on-Bedrock agentic loops). When only the
*scoring* changes (a check is calibrated, a golden rubric note is added), there is no need to
re-drive the model — the raw trajectories (tool calls + args + final text) are persisted in
layer2_results.json. This replays score() over them and rewrites the aggregates in place,
so the report reflects the corrected scoring without a fresh sweep.

Usage:  python -m eval.rescore_layer2            # rescore eval/layer2_results.json in place
        python -m eval.rescore_layer2 --in X.json --out Y.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics

from eval import layer2_behavioral as L2
from eval.harness import call_tool

_HERE = os.path.dirname(os.path.abspath(__file__))


def _replay_convert_steps(traj: list) -> None:
    """F-1: re-invoke the SAVED convert_units calls against the (now-fixed) live
    server, refreshing each step's status + returned value/warning in place.

    This is what "replay on the already-saved args" means: the model trajectory is
    frozen, but the conversion path the model exercised is re-driven through the
    current server, so the rescore reflects the tool fix without a new model sweep.
    ONLY convert_units is re-invoked — the single tool F-1 touched — so the GO
    categories (whose tools are unchanged) are left byte-for-byte alone."""
    for step in traj:
        if step.get("tool") != "convert_units":
            continue
        try:
            live = call_tool("convert_units", step.get("args", {}))
        except Exception:  # pragma: no cover - defensive; never let replay crash rescore
            continue
        step["status"] = live.get("status")
        step["live_value"] = live.get("value")
        step["live_warning"] = live.get("warning")


def rescore(path_in: str, path_out: str) -> dict:
    data = json.load(open(path_in, encoding="utf-8"))
    golden = {t["id"]: t for t in
              json.load(open(os.path.join(_HERE, "golden_set.json"), encoding="utf-8"))["layer2_tasks"]}

    for pt in data["per_task"]:
        task = golden[pt["task_id"]]
        run_scores = []
        for r in pt["details"]:
            traj = r.get("trajectory")
            if traj is None:
                # older results without persisted args: cannot faithfully re-score arg-based
                # checks; keep the stored verdict and flag it.
                run_scores.append(1.0 if r["task_pass"] else 0.0)
                r["rescored"] = False
                continue
            _replay_convert_steps(traj)
            sc = L2.score(task, {"trajectory": traj, "final_text": r["final_text"]})
            r["checks"] = sc["checks"]
            r["task_pass"] = sc["task_pass"]
            r["rescored"] = True
            run_scores.append(1.0 if sc["task_pass"] else 0.0)
        pt["mean_pass"] = round(statistics.mean(run_scores), 3) if run_scores else 0.0
        pt["variance"] = round(statistics.pvariance(run_scores), 4) if len(run_scores) > 1 else 0.0

    by_cat: dict[str, list[float]] = {}
    for pt in data["per_task"]:
        by_cat.setdefault(pt["category"], []).append(pt["mean_pass"])
    data["by_category"] = {c: {"mean": round(statistics.mean(v), 3),
                               "min": round(min(v), 3), "n_tasks": len(v)}
                           for c, v in by_cat.items()}
    data["rescored"] = True

    json.dump(data, open(path_out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=os.path.join(_HERE, "layer2_results.json"))
    ap.add_argument("--out", dest="outp", default=None)
    args = ap.parse_args()
    out = args.outp or args.inp
    data = rescore(args.inp, out)
    print(f"Re-scored {args.inp} -> {out}  (model={data.get('model')}, runs={data.get('runs')})")
    print("-" * 80)
    for pt in data["per_task"]:
        print(f"  {pt['task_id']:5s} {pt['category']:22s} mean={pt['mean_pass']:.2f} var={pt['variance']:.3f}")
    print("-" * 80)
    for c, v in sorted(data["by_category"].items()):
        flag = "  <-- WEAK" if v["mean"] < 0.67 else ""
        print(f"  {c:24s} mean={v['mean']:.2f} min={v['min']:.2f} (n={v['n_tasks']}){flag}")
