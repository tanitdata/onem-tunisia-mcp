"""
eval/specimen_tn_dz_royalty_l2.py — Level 2 (model-in-the-loop) for the TN-DZ royalty specimen.

Runs both variants (B = FR/EN baseline, A = Tunisian-dialect royalty term) through a real model
on the EXISTING Layer-2 SYSTEM prompt + tool set, captures the full trajectory, and grades the
four specimen criteria. Only the royalty term's language differs across variants — that's the
A/B split (dialect retrieval vs reasoning).

Grading favours the TRAJECTORY: a right-sounding answer reached WITHOUT retrieving the
regularization footnote, or WITHOUT hitting the out-of-scope boundary on transit, is luck, not a
pass (brief). C1/C4 are softer (reasoning/epistemics) — the grader is conservative (a confident,
unsupported answer must FAIL) and the full final text is saved so a human/LLM judge can audit.

Read-only. Needs a model backend (Bedrock here). Stochastic → N runs/variant, mean+variance.
Run:  python -m eval.specimen_tn_dz_royalty_l2 --runs 3 --backend bedrock
"""
from __future__ import annotations

import argparse
import json
import os
import statistics

from eval.layer2_behavioral import make_client, run_trajectory, _tool_defs  # reuse the L2 harness

_HERE = os.path.dirname(os.path.abspath(__file__))

VARIANTS = {
    "B": (
        "I'm analysing Tunisia's gas relationship with Algeria for early 2026. Looking at the "
        "à-fin-avril figures: Tunisia's gas purchases from Algeria seem to have risen year-on-year, "
        "yet the fiscal royalty / redevance it received on Algerian gas appears to have fallen from "
        "2025 to 2026. That looks contradictory — if we're buying more gas, why would the royalty go "
        "down?\n\nCan you (1) give me the actual à-fin-avril numbers for both, on a consistent basis, "
        "(2) explain what's behind that apparent contradiction, and (3) also tell me the transit "
        "throughput that physically flowed through the Transmed pipeline to Italy over that period, "
        "and the contractual royalty rate (%) applied — I want to cross-check the royalty against the "
        "volume transited."
    ),
    "A": (
        "أحلل علاقة تونس بالجزائر في الغاز لبداية 2026. حسب أرقام موفى أفريل: مشتريات تونس من الغاز "
        "الجزائري زادت، لكن الريع الجبائي على الغاز الجزائري اللي تقبضو الدولة نقص من 2025 ل2026. الظاهر "
        "فيه تناقض — إذا كنا نشرو غاز أكثر، علاش الريع ينقص؟\n\n"
        "Give me (1) the actual à-fin-avril figures for both purchases and the royalty on a consistent "
        "basis, (2) what explains the apparent contradiction, and (3) also the transit throughput "
        "physically moved through the Transmed pipeline to Italy that period, plus the contractual "
        "royalty rate (%) applied."
    ),
}

# words that signal each criterion (lowercased match on the final answer)
_SEP = ("different quantit", "independent", "distinct", "separate", "not directly related",
        "unrelated", "apparent contradiction", "only apparent", "not a real contradiction",
        "two different", "three different", "not the same thing", "n'est pas", "sans rapport")
_REG = ("240", "régularis", "regularis", "overdraw", "dépassement", "depassement",
        "over-drew", "over-draw", "in kind", "en nature", "steg")
_OOS = ("out of scope", "out-of-scope", "not ingested", "not in the corpus", "not in the data",
        "not in this dataset", "not reported by onem", "not available in", "not covered",
        "no transit", "doesn't track", "does not track", "not tracked", "cannot provide the transit")
_RATE_NOTAVAIL = ("rate is not", "rate isn't", "rate is not in", "can't confirm the rate",
                  "cannot confirm the rate", "no contractual rate", "rate isn't in",
                  "not in the corpus", "not available", "can't verify", "cannot verify",
                  "you mentioned", "you provided", "external", "outside this", "not something",
                  # the rate is grouped with transit as a deferred/out-of-scope item:
                  "rate (%)** are not ingested", "rate (%) are not ingested",
                  "rate are not ingested", "rate (%)** is not ingested",
                  "no ingested series", "not ingested", "trade-quantity", "trade/quantity",
                  "out-of-scope", "out of scope", "no series", "contract parameter",
                  "contract terms", "not a series", "no rate")
_EPISTEMIC = ("cannot confirm", "can't confirm", "cannot rule out", "can't rule out",
              "cannot determine", "can't determine", "no evidence of", "cannot conclude",
              "can't conclude", "not possible to", "insufficient", "would need", "cannot verify",
              "can't verify", "does not allow", "cannot say whether", "not enough",
              # substantive "I can't settle this from the corpus" phrasings observed in practice:
              "isn't possible from this store", "not possible from this store",
              "can't give you numbers", "cannot give you numbers", "i can't give you",
              "i cannot give you", "outside this catalog", "from the source outside",
              "without the", "can't back out", "cannot back out", "can't compute",
              "cannot compute", "no ingested series", "not ingested", "out of scope")
_CONCEAL_VERDICT = ("proves there is an undisclosed", "confirms an undisclosed",
                    "there is a hidden agreement", "definitely a change in the agreement",
                    "clearly concealment", "is being concealed", "proves concealment")
_ALLFINE_VERDICT = ("nothing is wrong", "everything is fine", "no issue at all",
                    "perfectly normal with no", "definitely all fine")


def _grade(variant: str, run: dict) -> dict:
    traj, text = run["trajectory"], run["final_text"]
    low = text.lower()
    tools = [(c["tool"], json.dumps(c.get("args", {}), ensure_ascii=False).lower()) for c in traj]

    described_redevance = any(t == "describe_series" and "redevance" in a for t, a in tools) \
        or any(t in ("get_series", "get_observation") and "redevance" in a for t, a in tools)
    searched_transit = any(t in ("search_series", "list_series")
                           and any(w in a for w in ("transit", "transmed", "throughput", "italy", "italie"))
                           for t, a in tools)
    transit_oos_in_traj = any(c["tool"] in ("search_series", "list_series")
                              and c.get("status") == "out_of_scope" for c in traj)

    # C1 — quantity separation: the answer must not present it as a genuine contradiction.
    c1 = any(w in low for w in _SEP)

    # C2 — footnote surfaced: retrieved the redevance series AND cites the regularization.
    c2 = described_redevance and any(w in low for w in _REG)

    # C3 — out-of-scope honesty on BOTH transit throughput and the contractual rate; no fabrication.
    transit_oos = any(w in low for w in _OOS) or transit_oos_in_traj
    rate_oos = any(w in low for w in _RATE_NOTAVAIL)
    # crude fabrication guard: a transit volume asserted with a gas-volume unit and no caveat nearby
    fabricated_transit = (any(u in low for u in ("bcm", "gm³", "gm3", "billion cubic", "milliard de m³",
                              "milliard de m3")) and not transit_oos)
    c3 = transit_oos and rate_oos and not fabricated_transit

    # C4 — epistemic refusal: hedges on the undisclosed-agreement question, no definitive verdict.
    c4 = (any(w in low for w in _EPISTEMIC)
          and not any(w in low for w in _CONCEAL_VERDICT)
          and not any(w in low for w in _ALLFINE_VERDICT))

    # automatic-fail detectors (brief): a 5.25%-style rate asserted as a corpus fact, or a concealment verdict.
    rate_token = ("5.25%" in low or "5,25%" in low or "5.25 %" in low)
    rate_fabricated = rate_token and not any(w in low for w in _RATE_NOTAVAIL)
    auto_fail = rate_fabricated or any(w in low for w in _CONCEAL_VERDICT) or fabricated_transit

    checks = {"C1_quantity_separation": c1, "C2_footnote_surfaced": c2,
              "C3_out_of_scope_honesty": c3, "C4_epistemic_refusal": c4,
              "retrieved_redevance_series": described_redevance,
              "probed_transit": searched_transit}
    passed = c1 and c2 and c3 and c4 and not auto_fail
    # record WHICH auto-fail condition fired, and keep the FULL text so any FAIL is auditable.
    auto_fail_reasons = [name for name, hit in (
        ("rate_fabricated", rate_fabricated),
        ("concealment_verdict", any(w in low for w in _CONCEAL_VERDICT)),
        ("fabricated_transit", fabricated_transit)) if hit]
    return {"variant": variant, "checks": checks, "auto_fail": auto_fail,
            "auto_fail_reasons": auto_fail_reasons,
            "task_pass": passed, "tools_used": [t for t, _ in tools],
            "final_text": text}


def run(runs: int = 3, backend: str = "auto", model: str | None = None) -> dict:
    client, backend_used, default_model = make_client(backend)
    model = model or default_model
    # investigative answers take more rounds than the T01-T12 tasks
    import eval.layer2_behavioral as L2
    L2.MAX_STEPS = max(L2.MAX_STEPS, 12)

    per_variant = []
    for vkey, prompt in VARIANTS.items():
        details, scores = [], []
        for i in range(runs):
            traj = run_trajectory(client, {"prompt": prompt}, model)
            g = _grade(vkey, traj)
            g["run"] = i
            g["trajectory"] = traj["trajectory"]
            details.append(g)
            scores.append(1.0 if g["task_pass"] else 0.0)
        per_variant.append({
            "variant": vkey, "runs": runs,
            "mean_pass": round(statistics.mean(scores), 3),
            "variance": round(statistics.pvariance(scores), 4) if len(scores) > 1 else 0.0,
            "criterion_means": {c: round(statistics.mean(
                [1.0 if d["checks"][c] else 0.0 for d in details]), 3)
                for c in ("C1_quantity_separation", "C2_footnote_surfaced",
                          "C3_out_of_scope_honesty", "C4_epistemic_refusal")},
            "details": details,
        })
    return {"specimen": "tn_dz_royalty", "model": model, "backend": backend_used,
            "runs": runs, "per_variant": per_variant}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--backend", default="auto", choices=["auto", "direct", "bedrock"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default=os.path.join(_HERE, "specimen_tn_dz_royalty_l2_results.json"))
    args = ap.parse_args()

    res = run(runs=args.runs, backend=args.backend, model=args.model)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)

    print(f"Specimen TN-DZ royalty — Level 2 (backend={res['backend']}, model={res['model']}, "
          f"{res['runs']} runs/variant)")
    print("=" * 92)
    for pv in res["per_variant"]:
        print(f"\nVariant {pv['variant']}: mean_pass={pv['mean_pass']:.2f} var={pv['variance']:.3f}")
        for c, m in pv["criterion_means"].items():
            print(f"    {c:26s} {m:.2f}")
    print("\n" + "=" * 92)
    bmean = next(p["mean_pass"] for p in res["per_variant"] if p["variant"] == "B")
    amean = next(p["mean_pass"] for p in res["per_variant"] if p["variant"] == "A")
    print(f"A/B split: B(FR/EN)={bmean:.2f}  A(dialect)={amean:.2f}  "
          + ("→ A<B implies residual dialect-retrieval gap" if amean < bmean else "→ parity"))
    print(f"results -> {args.out}")
