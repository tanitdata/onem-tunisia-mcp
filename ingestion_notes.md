# ingestion_notes.md — Phase E

How the next monthly Conjoncture or annual Memento/Bilan is appended to the store
without breaking existing series or deleting history. The pipeline is **idempotent**
and **re-runnable** (verified: a second `backfill.py` run = 7586 noop, 0 insert).

## Pipeline at a glance

```
manifest.csv ──► seed.py     (schema.sql + dimensions + sources + reference_docs)
             └─► backfill.py (per-family loaders ─► observations ─► recompute is_preferred)
                              load_bilan.py / load_memento.py / load_conjoncture.py
                              └─► onem_lib.py (token norm, Vocab alias map, GridPage
                                  coordinate extractor, series_key/upsert_key, DB upsert)
             └─► validate.py + report.py + catalog.py
```

Re-run any time: `python backfill.py && python report.py && python catalog.py`.

## 1. Stable IDs are the backbone

- Every dimension member + indicator has a controlled-vocabulary string ID
  (`flow.*`, `prod.*`, `field.*`, `indicator.*`), defined in `vocab_*.csv` / `seed.py`,
  never derived from source text. Ingestion maps each source label → ID via the
  `aliases` column (`Vocab.match`).
- **`series_id` = `series_key`** (the stable series identity, seeds the MCP layer):
  `indicator|flow|product|sector|region|field|level|producer|basis|unit|period_type|`
  `redevance|scope|technology|regime|geography_scope`. Built from IDs only → identical
  across runs. Two observations share a series_id **iff** they are the same series
  differing only in period.
- **Unknown label → quarantine, never guess.** Unmapped source labels go to
  `staging_unmapped` for a human to add an alias or vocab member (hard constraint #2).
  New members get a new stable ID; existing IDs never change.

## 2. Adding the next report

1. Run the acquisition step (`acquire.py`) → it appends the new PDF to `manifest.csv`
   with `file_id`, `sha256`, `report_family`, `period` (parsed from the decoded
   filename), `version`, `language`, and a `supersedes` link if the hash changed at a
   known URL.
2. `seed.py` registers a `source` row with a **deterministic `source_id`**
   (`derive_source_id`: `<type>_<period>[_<version>][_lang]`, e.g. `conjoncture_2026_05`,
   `memento_2025`, `bilan_2025`). AR/`is_canonical_lang=FALSE` editions are registered
   but **never ingested** (translation, not a 2nd observation).
3. `backfill.py` routes the edition to its loader by family + template; the loader
   reads cutoff/years/anchors **per edition** and emits observations.

## 3. Upsert key & idempotency

- `upsert_key = series_key + period_start + period_end + source_id`.
- Re-ingesting the same report → `upsert_key` already present, identical payload →
  **no-op** (skip). Safe to re-run.
- A new month (`conjoncture_2026_05`, new `period_end`) → new `upsert_key` → pure
  **INSERT**, appended to the existing `series_key`'s series.

## 4. Revisions supersede WITHOUT deleting (the v1→v2 case)

Two values for the same `series_key + period` from different sources/versions both
**persist**. `recompute_preferred()` sets `is_preferred` per `(series_key, period)`
group by the ratified precedence (OQ-R5):

```
report_type rank: bilan(3) > memento/rapport(2) > conjoncture(1) > covid(0)
then data_status: final(3) > revised(2) > provisional(1) > estimated(0)
then later publication_date wins
```

Losers keep `is_preferred=FALSE` and `supersedes_id → winner` (history auditable; the
`v_series` view exposes only preferred). **Bilan v1→v2**: ingest `bilan_2024` (internally
v2) and, when a separate v1 surfaces, ingest it as `bilan_2024_v1`; v2 wins by later
publication_date, v1 row remains. The manifest `supersedes` column carries the link
(`source.supersedes_source`).

Precedence refinements applied (OQ-R5): it acts **per cell**, not per report (Memento
price tables are `provisional`/"avant audit" even when other Memento cells are firmer);
and an older annual never overrides the only source for a still-open period (the most
recent partial year legitimately stays Conjoncture-YTD).

## 5. Per-edition alignment + drift (mandatory, not one-time)

- **Conjoncture**: `load_conjoncture` locates the C-T1 page, reads the header years +
  **cutoff month from the report** (`parse_header_years`), and derives the 6-column value
  anchors from the RESSOURCES row (`detect_anchors_from_row`). Verified across cutoffs
  4/6/12/1 and baselines 2010/2015.
- **Bilan**: `edition_anchors` re-anchors the template column map to each edition via
  distinctive header words; a Production-primaire **row-identity self-check** sets
  `extraction_confidence='low'` if the row doesn't reconcile (never silently ingests a
  misalignment). `template_version` (v2010/v2015/v2024) is stored on every observation.
- **Memento**: detects the year-column anchors per edition; **rejects rotated layouts**
  (returns −1). Only the validated 2024 reference is ingested; 2018–2023 are deferred
  (see coverage_gaps.md) pending per-edition calibration.

## 6. The two ESCALATED items stay isolated

- **OQ-R1** (Bilan gas basis/scope): all Bilan natural-gas matrix cells carry
  `calorific_basis='PCS'`, `basis_confidence='inferred'`, `scope='primary_broad'`,
  `is_escalated=TRUE`, `escalation_ref='OQ-R1'`, footnote `FN-BILAN-GAS-PCS`. They form a
  **separate series** from Memento commercial-dry gas — the reconciliation never merges
  them. Querying excludes escalated series unless explicitly requested.
- **OQ-F2** (Barka vs Baraka): kept as distinct field records (`field.barka` oil vs
  `field.maamoura_baraka` gas), footnote `FN-OQ-F2-FIELD`. No merge until ONEM confirms.

Both are **non-blocking**: they are stored, flagged, and excluded from auto-reconciliation,
so the rest of the build proceeds.

## 7. Validation gates after each batch

`validate.py` runs the Phase D checks (period hygiene, PCI/PCS, balance, rollup safety,
FR/AR dedup, provenance completeness, YTD-vs-annual safety, cross-edition reconciliation,
Dec-YTD≈annual, coverage). A `FAIL` (e.g. an unmapped-label spike, a series mixing
period_type, or an AR-sourced observation) holds the batch for review. `report.py`
regenerates `validation_report.{md,json}`, `new_conflicts.md`, `coverage_gaps.md`;
`catalog.py` regenerates `series_catalog.{md,csv}` + `reference_docs.csv`.

## 7b. Rollup integrity (OQ-M2) — row-level is_total + completeness gate

- **Total-ness is a per-ROW property** (`observation.is_total`), set by each loader, NOT
  inferred from a dimension. The same `flow.demand` is a total in the "DEMANDE" row and a
  leaf in "Haute pression"; the same dimension can be either depending on the row.
- `v_series_detail` = `is_preferred AND NOT is_total` → leaves only, so `get_series`
  summation never double-counts totals + components.
- Multi-level hierarchies are flagged so exactly ONE partition sums to the total, e.g.:
  - C-T14 PP consumption: leaves = mid-level products (GPL, Essences, Gasoil, Fuel, …);
    the deeper sub-children (Gasoil ordinaire/SS/premium) are `is_total` (roll up into Gasoil).
  - C-T15/16 gas demand: DEMANDE = production_électrique + (HP + MBP); `hors prod élec`
    (= HP+MBP) is `is_total`, so leaves = production_électrique + HP + MBP.
  - C-T20 electricity: PRODUCTION NATIONALE = STEG-carriers + IPP + autoprod + tiers; the
    STEG subtotal and the supply rows (échanges/achats) are `is_total`.
- **Silent-drop gate**: `backfill.flag_rollup_low_confidence` checks, per edition, that the
  leaves sum to the canonical grand-Total row (pinned in `GRAND_TOTAL_SQL`, shared with
  `validate.check_rollup_completeness`). Editions that don't reconcile (per-edition layout
  drift) are tagged `extraction_confidence='low'` and excluded from `v_series_clean` — a
  conscious downgrade, never a silent wrong value. Validation check **C10** then requires
  the clean surface to reconcile **exactly** (0 mismatches).
- **Quarantine, never drop**: a data row whose label doesn't map but **carries values** is
  written to `staging_unmapped` *with its values* (so a dropped row is always visible),
  not silently skipped.

## 7c. Dual-partition tables, glossary, and consumer surface (QA round 5)

- **Two partitions of one total** (gas demand: usage power/non-power AND pressure HP/MBP;
  electricity: by-producer AND by-carrier): exactly ONE partition is the canonical leaf
  set; the other is flagged `is_total` (alternative breakdown). For gas demand the USAGE
  split is canonical (consistent across `load_memento` M-T16/17 and `load_conjoncture`
  C-T15/16). New gates **C11** (leaves ≤ 1.15× the canonical grand total) and **C12**
  (exactly one canonical grand total per group) catch the double-count that the
  parent↔child rollup check could not see.
- **Derived grand totals**: where the PDF prints no total row (M-T16/17 gas demand,
  M-T18 elec local/incl-exports, older C-T20/C-T21), the loader emits a derived
  `is_total=TRUE, is_derived=TRUE` total = sum of the canonical leaves, so every group
  has exactly one grand total. A printed total of 0 while leaves are non-zero is treated
  as a mis-read and replaced by the derived total.
- **`incl_exports` (OQ-R6)**: three distinct electricity-sales series — `local` (HT+MT+BT,
  ≈17090), `exports_only` (Ventes externes sliver, ≈107.9), and the `incl_exports` grand
  total (≈17197). Never equate local with incl_exports.
- **Footnotes**: `footnote.text` carries the real caveat (verbatim + effect) from
  `04_footnotes.md`; `v_observation_footnotes` resolves each observation's footnotes to
  full sentences.
- **Definitions + glossary**: every indicator has a real one-sentence `definition`; the
  `scope_glossary` table defines each qualifier token (commercial_dry, primary_broad,
  incl/excl_gpl_condensat, local/incl_exports, redevance incl/excl, PCI/PCS,
  power_generation/non_power) with its "never sum/equate across" rule.
- **Consumer surface**: `aggregation_role` is a first-class `observation` column
  (CHECK enum: grand_total / subtotal / alternative_breakdown / leaf), set by every
  loader through the single `onem_lib.aggregation_role()` classifier and exposed in
  `v_series` / `series_catalog`. `v_series` also exposes `is_total` + cell provenance
  (source_id/page/ref/cell). MCP defaults to `v_series_clean` / `v_series_detail`.
- **Grand-total guarantee**: every breakdown-table (source, period, geography) group on
  the clean surface has exactly ONE `grand_total` row (read from the PDF, or derived =
  sum of canonical leaves where the PDF prints none). Sparse/partial captures with no
  grand total are flagged `extraction_confidence='low'` and excluded from the clean
  surface. Gates C11 (no leaf-sum > 1.15× grand_total) and C12 (exactly one grand_total
  per group) — keyed on `aggregation_role` and grouped by period_type + geography_scope —
  enforce this. STEG / Gasoil / Essences are `subtotal`; pressure (HP/MBP),
  marché-local, échanges, exports-only are `alternative_breakdown`.

## 8. Next phase (NOT built here)

The **MCP server** is the next phase — it queries this DuckDB file (read-only),
exposing `series_id`-keyed slicing with the `v_series` / `v_series_detail` views (the
latter excludes `is_total` rows so aggregations never double-count totals + components,
per OQ-M2). It is intentionally **not** implemented in this build.
