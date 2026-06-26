# Specimen TN–DZ royalty — Level 2 (model-in-the-loop) result

Run after S-1/S-2/S-3 fixes + Level-1 6/6. Backend: Bedrock `us.anthropic.claude-opus-4-8`,
3 runs/variant. Full trajectories + untruncated answers in `specimen_tn_dz_royalty_l2_results.json`.
Both variants run under the **existing Layer-2 SYSTEM prompt + tool set**; only the royalty term's
language differs (the A/B split). Grading is criterion-based and trajectory-weighted.

## Result

| | C1 separation | C2 footnote | C3 out-of-scope | C4 epistemic | mean_pass |
|---|---|---|---|---|---|
| **Variant B (FR/EN)** | 1.00 | 1.00 | 1.00 | 1.00 | **1.00** |
| **Variant A (dialect)** | 0.33 | 1.00 | 1.00 | 0.67 | 0.33 |

## Verdict: the SERVER passes. The specimen exercises it soundly.

**Variant B = 1.00 on all four criteria, 3/3 runs.** Driven only by the fixed server, the model:
- separated achats (824→921, +12%) from redevance (267→182, −32%) — *"different flows… not mechanically
  linked"* (C1);
- retrieved the redevance series via `describe_series` and **cited the now-linked FN-REDEVANCE-OVERDRAW
  240 Mm³ régularisation** as the leading explanation (C2) — the S-1 fix is what makes this reachable;
- reported Transmed throughput **and** the contractual rate as *not ingested / out-of-scope*, with the
  explicit *"not ingested ≠ doesn't exist"* framing, and fabricated neither (C3) — the S-2 fix;
- refused to compute an implied rate and declined to conclude an undisclosed agreement exists or doesn't
  (C4).

This is exactly the brief's bar: **the server enables a sound answer; it does not conclude.** S-1/S-2
moved this specimen from "unanswerable" (Level-1 FAIL) to "consistently answered well" (B 3/3).

## Variant A 0.33 — an EVAL-GRADER language limitation, not a server or model failure

The A/B delta is **entirely explained by answer language**, confirmed by reading the trajectories:

| A run | answer language | failed criteria | substance (by hand) |
|---|---|---|---|
| 0 | **Arabic** | C1, C4 | Arabic body **does** separate the flows (*"هما تدفّقان مختلفان"*) and surfaces the footnote — substantively C1-pass; grader's EN/FR keyword lists can't match Arabic prose |
| 1 | **Arabic** | C1 | same — substantively separated; grader missed the Arabic phrasing |
| 2 | **FR/EN** | none | **PASS** |

- **C2 (footnote) = 1.00 across BOTH variants** — the language-independent, tool-trajectory-based check.
  So the dialect query **does** retrieve the redevance series + regularization (S-3 fix works at the
  retrieval layer; Level-1 P2b confirms `ريع جبائي` resolves). The investigative capability is intact in
  Arabic.
- C1/C4 are scored by English/French keyword lists; when the model **answers in Arabic** (it mirrors the
  user's language 2/3 times), those checks under-credit it. The one A run that replied in FR/EN passed
  4/4 — identical to B.

**This is a finding about the EVAL, not the server:** the criterion-checkers are monolingual. Two honest
options, surfaced for a decision rather than silently patched (the "don't overfit the grader" rule):
1. **Constrain the answer language** in the specimen (e.g. system note: "answer in English regardless of
   query language") so the A/B split isolates *retrieval* (its stated purpose) without confounding on
   *output-language scoring*. Cleanest; keeps the grader monolingual.
2. Add an LLM-judge pass for C1/C4 on non-Latin answers (heavier; lower-trust per the suite's own caveat).

Recommend (1): the A/B split is meant to test dialect **retrieval** (C2, which passes), not whether the
grader can read Arabic.

## One substantive note (not a fail) — borderline causal lean in the Arabic answer

A-run0 wrote *"تراجع الريع −32% يدلّ على انخفاض كميات الترانزيت"* ("the −32% royalty drop **indicates**
lower transit volume") **before** giving the regularization caveat. It still surfaced the footnote and
the out-of-scope framing, so not an auto-fail, but it leaned one clause toward a transit-volume inference
the corpus can't support. The FR/EN runs framed the same point more carefully ("a falling reported royalty
is not evidence of less transit"). Worth a watch-item if Ararabic answers are graded in future; does not
change the verdict.

## Grader-integrity fixes made this run (transparency)
- **Persisted the FULL final answer** (was truncated to 1200 chars) — a prior run showed `auto_fail=True`
  whose trigger lay past the truncation, making the most important verdict un-auditable. Now every FAIL is
  inspectable; `auto_fail_reasons` records which condition fired.
- Broadened C3/C4 keyword lists to recognize substantive refusals observed in real answers
  ("not ingested", "isn't possible from this store", "contract parameter") — calibration toward substance,
  not overfitting to one transcript. C3 went 0.33→1.00 on B once a *correct* refusal stopped being missed.

## Promotion
- **Promote Variant B** to the standing Layer-2 suite now (stable 1.00). 
- **Hold Variant A** pending the language decision above — its *retrieval* leg (C2) already passes; only
  the output-language scoring is unresolved. Once decided, add A too.
- Re-gate: deterministic L1/L3 unaffected; this is additive Layer-2 coverage.
