# ONEM MCP — Evaluation Suite

A standing, versioned eval for the `onem-energy` MCP server. **Not** the QA pass (that found and
fixed defects); this keeps *measuring* — after every tool or description change — whether the server
lets a consuming LLM produce **trustworthy answers**.

> Read-only on the DB and the server; the eval modifies nothing.

## Governing principle: score by failure mode, never a single accuracy number

For a database, "correct value" is the metric. For an **MCP server** it isn't — the store already
holds correct values; the server's job is to make a consuming model *behave* correctly. The failures
that matter return individually-correct numbers and still produce a wrong answer: wrong series picked,
basis dropped (PCI vs PCS), a total summed with its leaves, out-of-scope read as "no data". A global
accuracy score is blind to all of these. So the **headline is the per-failure-mode breakdown**, and the
behavioral layer (a real model driving the tools) is the centerpiece.

**Scope boundary (keeps the eval diagnostic):** ground truth is the **DATABASE** (`v_series_clean`), not
the source PDFs. A DB-vs-source discrepancy is the audit's job and already gated; grounding on the DB
isolates failures to the **MCP layer**. A value that looks wrong → route to the audit carry-forward, not
here.

## Layout

| File | What it is |
|---|---|
| `golden_set.json` | **Versioned**, DB-derived golden set. Each task carries its failure-mode category + mechanical assertions. The single source of expected answers. |
| `harness.py` | Read-only plumbing: `call_tool()` (live FastMCP dispatch) + `db()` (read-only DuckDB ground truth). |
| `layer1_retrieval.py` | **Layer 1 — retrieval fidelity** (deterministic). Value + every qualifier survives the round-trip; `convert_units` round-trips. |
| `layer2_behavioral.py` | **Layer 2 — behavioral / task eval** (model-in-the-loop, centerpiece). A real model drives the tools; scores the *trajectory*. Stochastic → N runs, mean+variance. |
| `layer3_adversarial.py` | **Layer 3 — adversarial gates** (deterministic pass/fail). The category errors the design prevents. Any gating failure → FIX-FIRST. |
| `report.py` | Per-failure-mode scoring + `eval_report.md` / `eval_results.json` generator. |
| `eval_report.md`, `eval_results.json` | Latest run output (the **baseline** is committed). |
| `layer2_results.json` | Full Layer-2 trajectories from the last L2 run (when run). |

## Running

```bash
# From the repo root. Force UTF-8 on Windows consoles (the server emits accented FR labels):
export PYTHONUTF8=1

# Deterministic layers only (no API key needed) — the re-runnable gate:
python -m eval.layer1_retrieval
python -m eval.layer3_adversarial

# Full report (L1 + L3; adds L2 if a key is present and --runs > 0):
python -m eval.report                 # deterministic only
python -m eval.report --runs 3        # + Layer 2, 3 runs/task

# Layer 2 alone (needs credentials — see below):
python -m eval.layer2_behavioral --runs 3                      # auto backend
python -m eval.layer2_behavioral --backend bedrock --runs 3    # force Bedrock
python -m eval.layer2_behavioral --runs 1 --task T01           # quick single task

# Re-score the SAVED Layer-2 trajectories after a scoring/golden change (no model re-run):
python -m eval.rescore_layer2

# Full report folding in the saved Layer-2 run (no model re-run):
python -m eval.report --use-saved-layer2
```

**Layer 2 backends.** Layer 2 drives a real model via the Anthropic Python SDK. `make_client()`
auto-detects:
- **direct** — `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` present → `anthropic.Anthropic()`, model
  `claude-opus-4-8`.
- **bedrock** — AWS creds present (`AWS_ACCESS_KEY_ID` / `AWS_PROFILE` / `~/.aws/credentials`) →
  `anthropic.AnthropicBedrock(aws_region=...)`, model `us.anthropic.claude-opus-4-8` (cross-region
  inference profile; the IAM principal needs `bedrock:InvokeModel` on the profile in your chosen region).

Force one with `--backend {direct,bedrock}`. With neither credential set, `report.py` records Layer 2 as
*unavailable* and still produces the deterministic gate — **L1 and L3 are the hard, key-free regression
gate; L2 is the richer, stochastic, lower-trust layer on top.**

**Cost / runtime.** Layer 2 is the expensive layer: every trajectory is a *sequential* agentic loop
(model → tool → model …). A 5-run sweep is 60 trajectories on Opus and takes ~15–20 min wall-clock; L1+L3
finish in seconds. For fast iteration use `--runs 3`, a single `--task`, or `--backend` with a cheaper
model via `--model`. When only the *scoring* changed (not the trajectories), use `rescore_layer2` — it
replays `score()` over the persisted trajectories (tool calls + args + final text) instead of re-driving
the model.

## Interpreting the per-failure-mode scores

The report's headline table is **per failure mode** (`pci_pcs_conflation`, `period_type_mixing`,
`scope_confusion`, `double_count`, `no_data_vs_out_of_scope`, `series_misselection`, `qualifier_drop`,
`provisional_as_fact`, `contested_as_settled`, `unit_basis_conversion`):

- **Deterministic column (L1 + L3):** exact pass rate. **100% is the bar** — these are mechanical.
- **Layer-2 mean column:** mean trajectory pass rate over N runs for tasks in that category. A category
  "passes" L2 at **mean ≥ 0.67** (variance-tolerant; tune `summarize(threshold=...)`). Treat L2 as the
  soft, lower-trust component — an LLM judge or a stochastic model can share the system's blind spots
  (a judge that doesn't notice a dropped basis won't penalize it), which is why the rubric is pinned to
  mechanical checks (correct `series_id` in tool args, qualifier words in the answer, guard status in the
  trajectory, forbidden-phrase absence) wherever possible.

**Verdict logic:** `report.py` returns **FIX-FIRST** iff any Layer-3 **gate** fails. L1 drops and L2
weak categories are surfaced prominently but are read by a human — a Layer-1 value-drift failure almost
always means a genuine interface regression; a Layer-2 dip may be model variance, so re-run with more
`--runs` before concluding.

## Updating the golden set (versioned, reviewed)

The golden set is a **stable regression gate**, not a moving target. To change it:

1. Bump `version` in `golden_set.json` (semver: patch = add tasks / fix typo; minor = new failure mode;
   major = re-derived expectations).
2. **Derive expected answers from the DB**, never hand-type them. The current values were pulled with
   direct `v_series_clean` queries (e.g. `SELECT value,unit_id,calorific_basis,aggregation_role,scope,
   geography_scope FROM v_series_clean WHERE series_key=? AND ref_year=?`). Re-derive after any
   re-ingest/rebuild.
3. **Human-review** the diff before committing (the file header tracks review status). A golden value
   that changed because the DB was rebuilt is expected; one that changed because the *interface* changed
   is a finding.
4. Keep each task tagged with (a) the failure-mode category it targets and (b) checkable assertions.

## When this surfaces a defect

- A genuine **MCP interface** defect (guard bypass, qualifier drop, label/selection problem, out-of-scope
  mislabeled) → route to the **MCP builder**.
- A **DB-state** problem visible at the MCP layer (e.g. an empty `reconciliation_log`) → route to the
  **DB builder**.
- A suspected **wrong value** → audit carry-forward, **not** an MCP defect (we ground on the DB).
- A genuinely new **repo-wide invariant** → recorded for human review and promotion into the project
  conventions.

## Re-gate discipline

Re-run L1 + L3 after **every** tool or description change — guardrails regress easily, and the live
server must be **restarted/reconnected** to pick up code edits before re-gating (a freshly-imported test
process and the running server can diverge). L1/L3 are deterministic; L2 is stochastic (run ≥3×, read the
variance) and is the lower-trust layer.
