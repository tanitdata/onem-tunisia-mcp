# Eval Specimen — TN–DZ gas royalty (investigative multi-series)

> **UPDATE 2026-06-26 — S-1/S-2/S-3 FIXED; Level-1 now 6/6 (target was 5/6). READY FOR LEVEL 2.**
> See `mcp_fix_report_S1_S2_S3.md`. S-1: `FN-REDEVANCE-OVERDRAW` linked to redevance 2025/26 obs
> (`describe_series` surfaces the legible 240 Mm³ régularisation text); audit found 36 stranded
> footnotes → 9 live ones linked, 26+1 correctly stranded on deferred tables (CLAUDE_proposals P-2).
> S-2: free-text transit/throughput queries now read out-of-scope (deferred-keyword match + a
> weak-AND-diffuse relevance floor). S-3: AR/dialect royalty term resolves to redevance. No regression
> (unit 39/39, acceptance 26/26, L1 105/105, L3 GO). **Level 2 tests the MODEL's reasoning over this
> now-fixed surface — the server enables a sound answer, it does not conclude.** Original FAIL analysis
> retained below.

Status after **Level 1 (by-hand tool calls, no model)**: **FAIL — unanswerable on this build.**
Rubric was fixed before any run (it ships in the brief). Level 2 (model-in-the-loop) is **not run yet**:
the decisive footnote is not retrievable, so no model could pass criterion 2 — running it would only
measure luck. Re-run Level 2 after S-1 is fixed.

Verdict per criterion (Level 1 evidence):
| # | criterion | result | why |
|---|---|---|---|
| 1 | quantity separation | (retrievable) | achats 921 / redevance 267→182 / transit are distinct series in the store |
| 2 | **footnote surfaced** | **FAIL → BLOCK** | the 240 Mm³ regularization footnote is in the DB but linked to **0** observations → invisible to `describe_series` |
| 3 | out-of-scope honesty | **FAIL** | transit-volume query returns ranked petroleum-products series with **no** out-of-scope note (looks like data exists) |
| 4 | epistemic refusal | (depends on 2+3) | can't be reached soundly while 2 and 3 mislead |

## Ground truth confirmed (DB + report)
- redevance PCI ytd cutoff=4: **2025 = 267 → 2026 = 182 ktep-pci (−31.8%)**; PCS 296 → 202. ✅ matches brief.
- achats PCI ytd 2026 = **921**; demande PCI ytd 2026 = **1478** (power_generation 951 + non_power 527). ✅
- footnote `FN-REDEVANCE-OVERDRAW` exists: *"dépassement des prélèvements STEG sur la redevance revenant
  à l'État à 240 millions de Cm3, en cours de régularisation (fin déc 2025/2025)"*. ✅ The decisive
  explanation is in the corpus — but stranded (see S-1).
- 5.25% contractual rate and Transmed throughput: **not in the corpus** (correct — must stay out-of-scope).

## Findings

### S-1 (BLOCK → DB builder) — the decisive regularization footnote is stranded (0 links)
**Evidence:** `FN-REDEVANCE-OVERDRAW` exists in `footnote` but
`SELECT count(*) FROM observation_footnote WHERE footnote_id='FN-REDEVANCE-OVERDRAW' AND <preferred>` = **0**.
Live `describe_series(redevance|…|PCI|ktep-pci|ytd_cumulative|…)` returns only FN-BALANCE-METHOD,
FN-PROVISIONAL, FN-PCI-PCS — **no mention of 240 / régularisation / dépassement** (`mentions=False`).
**Impact:** the footnote that turns "imported more, collected less royalty" from an apparent contradiction
into a documented STEG↔State accounting/timing artifact is **unreachable**. Criterion 2 cannot be met by
any model; criterion 4 (the sound refusal-with-likely-explanation) collapses without it. This is precisely
the BLOCK the brief anticipated: *"if the footnote isn't retrievable, no model can answer correctly."*
**Fix (DB builder):** link `FN-REDEVANCE-OVERDRAW` to the redevance ytd 2025/2026 observations (PCI+PCS)
via `observation_footnote` (mirror how FN-PROVISIONAL is attached). **Re-test:** `describe_series` on the
redevance series surfaces the 240 Mm³ regularization text.

### S-2 (BLOCK → MCP builder) — transit-volume query not flagged out-of-scope
**Evidence:** `search_series("Transmed throughput Algeria to Italy")` → `status=ok`, 476 matches, top hits
are `primary_balance`/`pp_consumption` **petroleum-products** series, **no** `also_note_out_of_scope`
block. Same for "gas transit volume to Italy". A consuming model sees plausible-looking series and may
treat transit as covered — exactly the no-data-vs-out-of-scope trap (CLAUDE.md #5). Note: the *canonical*
deferred indicator path is correct — `list_series("trade_quantity")` → `out_of_scope`,
`indicator="trade_value"` is defined-but-deferred — but **free-text transit queries don't reach it** and
silently return unrelated in-scope series. **Fix (MCP builder):** when a free-text query matches a deferred
family (trade/transit/throughput volume) but only returns weakly-related series, attach the
`also_note_out_of_scope` block (transit/échanges-volume is `trade_quantity`, deferred). **Re-test:** the
three transit queries carry an out-of-scope signal.

### S-3 (ADVISE → MCP builder, retrieval robustness) — Arabic-dialect term doesn't resolve
**Evidence (Variant A vs B):** `search_series("ريع جبائي على الغاز الجزائري")` → `empty_in_scope`
(redevance **not** found); the French "redevance / forfait fiscal" and English "royalty transit Algerian
gas" both resolve (`redevance_hit=True`). Per the brief's A-vs-B split, this isolates a **dialect/term
resolution gap in `search_series`**, distinct from the investigative-reasoning failure. AR is registered
non-canonical with 0 ingested observations (CLAUDE.md #8) — so this is a *search-robustness* improvement
(map common AR/dialect energy terms to canonical series), **not** a request to ingest AR. Lower severity:
a francophone/anglophone user reaches the series; the verbatim dialect user does not.

## Routing & gate
- **S-1 → DB builder** (link the stranded footnote). **S-2 → MCP builder** (out-of-scope on transit
  free-text). **S-3 → MCP builder** (AR/dialect term resolution, ADVISE).
- **Do not add this specimen to the standing Layer-2 suite yet** — the brief says add *once it passes*.
  After S-1 + S-2 are fixed and the live server is restarted, run Level 2 (both variants), capture
  trajectories, grade per criterion, then promote.
- Re-gate note: S-1 is a DB change → **restart/reconnect the MCP server** before re-testing (stale-handle
  rule), then re-run Level 1 here.
