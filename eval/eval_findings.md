# Eval Findings — ONEM MCP (Layer-2 baseline, 2026-06-26)

First full three-layer run. Backend: Bedrock `us.anthropic.claude-opus-4-8`, 5 runs/task.
Layer 1 = 86/86, Layer 3 = 10/10 (0 gating failures) → deterministic verdict **GO**. The
behavioral layer (model-in-the-loop) surfaced two items the deterministic gate cannot see.
Each is diagnosed below (defect vs model-behavior vs eval-artifact) per the brief.

## F-1 (ADVISE → MCP builder) — `convert_units` scope matching is exact-string

**Failure mode:** `unit_basis_conversion`. L2 mean **0.40**; L1 (same conversion, no scope
arg) = 100%. The divergence is the finding.

**Evidence (from saved trajectories, T11):**
- `convert_units(value=100, from_unit="ktep-pci", to_unit="ktep-pcs")` → `ok`, 90.0  ✅
- `convert_units(..., scope="gas")` → `no_factor`  ❌
- `convert_units(..., scope="natural_gas")` → `no_factor`  ❌
- only the exact string `scope="natural gas"` (or omitting scope) resolves.

**Why it matters to the consuming model:** a model that disambiguates the conversion with a
reasonable scope word ("gas", "natural_gas") gets a false `no_factor`, then correctly refuses
to invent a number — so the user is told the documented PCI→PCS conversion is unavailable when
it exists. Fails *safe* (never fabricates), but the headline conversion is unreachable via a
natural scope guess. This is the same exact-string brittleness Agent 3 flagged at the
label/catalog layer, now proven to bite a real consumer trajectory.

**Diagnosis:** genuine MCP interface defect (not model error, not eval artifact). The model
behaved correctly given the tool's response.

**STATUS: FIXED (2026-06-26).** `convert_units` now normalizes the scope/carrier word
(case-fold, separator collapse, accent fold) and maps a documented synonym set
(`_SCOPE_SYNONYMS`, seeded from the saved T11 args `gas`/`natural_gas` and extended to crude/
electricity/volume/products) to the canonical stored scope. An unrecognized non-empty scope is
passed through verbatim → still fails safe (`no_factor`, never fabricated, never silently
widened). Regression: golden `layer1_convert` gained `L1c-scope-{gas,natural_gas,natural-gas-
literal,gas-caps,elec-electricity,elec-power,elec-abbrev,unmapped-failsafe}`; Layer 1 = 105/105.
Rescore (tool-aware replay of the saved convert args against the fixed server): the F-1 sub-check
`conversion_resolved` is now **True on all 5 T11 runs** (was no_factor on the 2 scope-disambiguated
runs). See `mcp_fix_report_F1.md`. NOTE: T11's *composite* mean is still < 1.0 — held down by F-3
below, a separate bug, not F-1.

**Recommended fix (MCP builder):** normalize the `scope` argument in `convert_units` — match
`gas` / `natural_gas` / `natural gas` (and strip/case-fold) to the stored `"natural gas"`
factor key. **Re-test:** rerun `eval.layer2_behavioral --task T11` (or add an L1 convert case
with `scope="gas"` asserting `ok`/90.0).

## F-2 (ADVISE → MCP builder messaging / prompt) — deferred sometimes framed as absent

**Failure mode:** `no_data_vs_out_of_scope`. L2 mean **0.90** (T06 0.80, T07 1.00) after the
checker was calibrated to score the substantive out-of-scope signal (not exact phrasing).

**Evidence (T06, 1 of 5 runs):** the model answered *"I'm not finding any price indicator… the
catalog is built around physical quantities"* with **no out-of-scope / deferred cue** — i.e. a
deferred family presented as plain absence. The other 9/10 out-of-scope runs correctly said
"out of scope / not ingested / not the same as doesn't exist."

**Why it matters:** CLAUDE.md #5 (anti-misinformation) — deferred ≠ absent. The tool layer
already returns `out_of_scope` correctly on the canonical path (Layer 3 gates pass); the lapse
is the *model* occasionally not echoing the framing when it reasons from an empty search rather
than the `list_series`/`search_series` out-of-scope envelope.

**Diagnosis:** mostly model-behavior, intermittent (1/10). Tool layer is correct. Low severity.

**Recommended fix:** strengthen the `search_series`/`list_series` description so a zero/price
result steers the model to state out-of-scope explicitly; optionally have `search_series` ride
the `also_note_out_of_scope` block on near-miss price/refining queries. Not a gating failure.

## Not findings (diagnosed as eval-scoring artifacts, fixed in golden_set v1.1.0)

- **T03 (scope_confusion):** the model correctly summed the local BT/MT/HT leaves to 17089
  (CLAUDE.md #2 permits "sum leaves OR read grand_total") and distinguished the 17197
  incl_exports twin. Early scoring hard-required the grand_total *series_id* and matched the
  twin-distinguishing value with raw `str()` (missed "17,089"). Fixed: `accept_leaves_sum` +
  formatted value matching. Now stable 1.00.
- **T06/T07 false fails:** the checker substring-matched "doesn't exist" inside the model's own
  *disclaimer* ("this does NOT mean the data doesn't exist") and flagged "not available" even
  when paired with out-of-scope framing. Fixed: score the substantive out-of-scope cue, not
  forbidden phrasing. Lesson: don't overfit the grader to the model — anchor on substance.

## F-3 (NEW, surfaced while fixing F-1, 2026-06-26) — `convert_units` PCI→PCS direction is INVERTED

**Severity: likely-BLOCK for the next (all-gas PCI/PCS) specimen — NEEDS A RULING before that run.
Not fixed in the F-1 brief (out of its surgical scope; contradicts a prior settled fix).**

**Failure mode:** `unit_basis_conversion` (value, not reachability). Found because T11's composite
mean stayed 0.40 even after F-1 cleared the scope reachability: 3/5 runs report **111.1**, not 90.0.

**Evidence (ground truth from `energy.duckdb`, not the model):** for the SAME gas, PCS > PCI —
Miskar 2024 `gas_production … PCI = 317`, `… PCS = 353` (ratio 317/353 = 0.898). So the documented
"1 PCI = 0.9 PCS" means **PCI = 0.9 × PCS ⇒ PCS = PCI / 0.9**. Converting 100 ktep-pci → ktep-pcs
should give **≈ 111.1**, by DIVIDING by 0.9. `convert_units` MULTIPLIES (stored factor `(PCI,PCS)=0.9`)
and returns **90.0** — directionally wrong (it makes the higher-heating-value figure *smaller*). The
eval model caught this independently in 3/5 T11 runs ("the tool's 90.0 applied the factor as a
multiplication; per its own note ktep-pcs = ktep-pci / 0.9, the correct value is 111.1").

**Why it matters:** worse than F-1 for the next specimen — a *confidently wrong number* with a basis
caveat, not a safe refusal. Any basis-conversion reasoning step inherits the inversion.

**Why NOT fixed here:** (1) outside F-1's lane; (2) a *prior* BLOCK-fix brief explicitly asserted
90.0 and the golden `expect_value` + earlier unit tests encode 90.0 — flipping silently overrides a
settled decision; (3) the fix likely belongs at the DATA layer (`conversion_factor` row /
direction), which the F-1 brief forbids touching. Per repo norm: surface for a ruling, don't guess.

**Recommended ruling/fix (for the owner):** decide the canonical direction, then either store the
factor as `(PCI→PCS)=1/0.9≈1.111` (divide-semantics) or have `convert_units` invert for a
basis *increase*; update the golden `expect_value` (90→111.1) and the prior convert tests together.
Confirm against the IEA/Eurostat GCV/NCV convention already cited in `semantic_qa_report.md`
(NCV ≈ 0.9 × GCV).

---

**RULING & RESOLUTION (2026-06-26) — F-3 ruled BLOCK; FIXED.** See `mcp_fix_report_F3.md`.

- **Ground truth confirmed:** same-gas PCS > PCI across all fields (Miskar 317<353, Nawara 306<340,
  Chergui 98<109, …; ratio ≈ 1.11). External anchor: IEA/Eurostat NCV ≈ 0.9 × GCV (PCI=NCV, PCS=GCV).
- **Layer the bug lived in: DATA (stored row), with a one-row modeling gap.** Source
  `03_units_and_conversions.csv` line 40 stored `from=PCI,to=PCS,factor=0.9` whose OWN note said
  "ktep-pcs = ktep-pci / 0.9" — the factor's *direction* contradicted its label. It was also the ONLY
  reciprocal pair stored as a single row (GWh↔ktep, baril↔m³ all store BOTH directions), so the MCP
  reciprocal-synthesis inverted an already-wrong factor. **Fix at the data layer + both directions
  explicit.**
- **Fix applied:** CSV now stores BOTH directions — `PCI→PCS factor 1.1111` and `PCS→PCI factor 0.9`
  (both `calorific_basis='PCI->PCS'`, the schema-allowed basis-change marker). `convert_units` reads
  the correctly-directed row; F-1 scope normalization, fail-safe, and the basis-change warning all
  intact. DB rebuilt (`conversion_factor` now holds both rows).
- **Trace-back (Task 5):** the prior 90.0 was a face-value read of the ambiguous "1 PCI = 0.9 PCS"
  string — multiplying PCI by 0.9 — without checking the same-gas magnitudes (PCS>PCI) that make the
  direction unambiguous. 90.0 is NOT correct for any path; nothing legitimate is reintroduced by the
  flip. The prior `mcp_fix_report.md` convert claim is marked **SUPERSEDED** at its head.
- **Artifacts propagated (Task 4):** golden_set → v1.3.0 (all PCI→PCS cases + T11 `expect_value`
  90→111.111; `L1c-pcs-pci` recast as 100 pcs→90 pci); `test_onem_store_unit.py` corrected to 111.1 +
  new **property-based direction test** (`test_convert_direction_property`: PCI→PCS must INCREASE,
  PCS→PCI must DECREASE, ratio matches the DB same-gas PCS/PCI, round-trips return to origin for
  PCI/PCS, GWh/ktep, baril/m³).
- **Class audit (Task 3):** GWh↔ktep, baril↔m³ verified directionally correct both ways; products are
  one-way `t_*→tep` (no reverse stored, correctly returns no_factor). Only the PCI/PCS row was
  inverted; no systematic sign error.
- **Re-gate:** Layer 1 = 105/105 (corrected expectations), Layer 3 = 10/10 GO, unit = 39/39 (incl. the
  property test), acceptance = 26/26. **T11 rescore composite 0.40 → 0.80** (the replayed convert step
  now returns 111.1 for all 5 runs; 4/5 runs pass — the 1 residual is a run whose FROZEN prose said
  "cannot give a number", downstream of the OLD F-1 no_factor, unfixable without a new model sweep —
  NOT the F-3 bug).

## Routing
- **F-1 → MCP builder** (convert_units scope normalization) — **DONE** (this brief).
- **F-2 → MCP builder** (description/messaging nudge) — low severity, non-gating. HELD per fix brief
  (1/10, below noise floor; act only if it recurs as a pattern). Untouched.
- **F-3 → DB builder (stored row) + MCP (both-directions handling)** (PCI→PCS direction). **RULED
  BLOCK and FIXED (2026-06-26).** Cleared for the all-gas PCI/PCS specimen run.
- No Layer-3 gate regressed (10/10, 0 gating). Layer 1 = 105/105 (corrected). Unit = 39/39. Acceptance
  = 26/26. T11 rescore 0.40 → 0.80. Overall **GO**; F-3 closed.

## Governance / convention candidate (→ `CLAUDE_proposals.md`)
"Conversion-factor correctness is verified against DB ground truth (same-quantity magnitudes), never a
prior golden value; PCI→PCS divides by 0.9 (PCS>PCI), PCS→PCI multiplies by 0.9; reciprocal pairs store
BOTH directions explicitly." Proposed because F-3 passed every prior gate — the tests compared the
server to an incorrect expectation. Recorded for human promotion; no direct `CLAUDE.md` edit.
