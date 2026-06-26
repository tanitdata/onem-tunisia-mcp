# coverage_gaps.md — Phase D

## Realized coverage (editions yielding observations)

| family | editions ingested |
|---|---|
| bilan | 14 |
| conjoncture | 77 |
| memento | 1 |

Conjoncture editions ingested: 77 (span 2019-12…2026-04).

## Conjoncture within-edition table status

**Loaded** (C-T* tables now extracted across all tabular editions):

| table | meaning | obs |
|---|---|---|
| C-T1 | primary energy balance (+redevance toggle) | 3692 |
| C-T10 | crude production by field | 1203 |
| C-T11 | gas resources by field PCI | 2836 |
| C-T12 | gas resources by field PCS | 3451 |
| C-T14 | petroleum-products consumption by product | 4552 |
| C-T15 | gas demand PCI | 1386 |
| C-T16 | gas demand PCS | 1380 |
| C-T20 | electricity production by source | 3304 |
| C-T21 | electricity sales by voltage | 1216 |

**Deferred (consciously, not silent)** — listed so the gap is explicit:
- **C-T2** export/import énergétiques (3 side-by-side unit blocks: kt / ktep-pci / **MDT** trade value) — multi-block layout; trade-value family not yet ingested.
- **C-T13** raffinage STIR indicators (ktep / % / jours) — needs refining KPIs.
- **C-T17** exploration (permis/forages/découvertes, count) — needs exploration KPIs.
- **C-T3–C-T9 prices** (Brent, FX, crude price, PP price decomposition, gas/elec prices) — **no price or trade-value family is in the store yet**; deferred. Brent/FX are also better sourced from primary market data (OQ-C1).
- **Charts** C-F10 (forfait fiscal monthly) & C-F14 (elec-import cumul) — labeled, ingestible as `chart_label`/low; not yet loaded (OQ-C2).
- **Conjoncture 2017-09…12** narrative template (4 editions) — skipped (low priority).

## Known, accepted holes (per brief — confirmed, not errors)
- **Conjoncture FR 2018 + most of 2019**: absent from corpus; the tabular series starts 2019-12. Annual figures for those years still arrive via later Réalisé/Memento/Bilan columns.
- **Memento 2015–2017**: not published/absent. Only the 2014 ANME efficiency booklet and 2018–2024 ONEM Mementos exist.

## Deferred (flagged, need per-edition calibration — NOT silent)
- **Memento 2018–2023 (ONEM)**: ingested only the 2024 reference. 2018–2021 are page-rotated (portrait, transposed field tables); 2022–2023 shift table y-bands. Their by-field/by-region/price detail needs per-edition region calibration. Recorded as a coverage gap rather than risk silent misalignment (hard constraint #3).
- **Bilan 2011–2014, 2018**: ingested but the Production-primaire row self-check FAILED → tagged `extraction_confidence='low'`. The v2010-family x-anchors drift year-to-year; re-anchor per edition before trusting these cells.
- **Bilan 2021**: 0 cells — the poster is rotated/re-laid-out (matrix row 'primaire' at y≈746); needs a dedicated geometry. Flagged.
- **Memento 2024 within-edition tables loaded**: crude-by-field (M-T2), gas prod PCI/PCS by field (M-T5/6), gas supply PCI/PCS (M-T7/8), PP export (M-T9), elec production (M-T12), PP consumption (M-T15), gas demand PCI/PCS (M-T16/17), elec sales (M-T18). **Deferred**: prices (M-T27/28/29), by-region (M-T20/21/22/23), PP production/imports (M-T10/11), elec supply balance (M-T13) — add SPECS or keep deferred (no price/region family in store yet).
- **Conjoncture 2017-09…12 (narrative template)**: 4 transitional prose editions skipped in v1 (low priority); pre-date the clean tabular series.
- **Charts (OQ-C2)**: forfait-fiscal monthly (C-F10) and elec-import cumul (C-F14) not yet ingested; flagged `chart_label`/low when added. Unlabeled charts (OQ-C1) out of scope.
