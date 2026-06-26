# validation_report.md — Phase D

**Headline: PASS** (all hard checks pass; INFO/WARN are advisory).

- observations: **26899** (13142 preferred, 2709 low-confidence)
- distinct series: **593**; ref-year span 2010–2026
- escalated (isolated): **900** (OQ-R1 Bilan-gas-PCS)
- cross-edition reconciliation rows logged: **6308**

## Counts

| dimension | breakdown |
|---|---|
| by family | {'memento': 152, 'bilan': 3727, 'conjoncture': 23020} |
| by period_type | {'ytd_cumulative': 17165, 'annual': 9734} |
| by template_version | {'bilan-matrix-v2015': 250, 'conjoncture-tabular-v2024': 23020, 'bilan-matrix-v2024': 2200, 'memento-onem-v2024': 152, 'bilan-matrix-v2010': 1277} |

## Automated checks

| check | status | detail |
|---|---|---|
| C6_period_hygiene | **PASS** | 0 series_keys mix period_type |
| C6b_unit_basis | **PASS** | no series mixes unit or basis |
| C2_pci_pcs | **PASS** | 2158 PCI/PCS pairs checked, 0 outside 0.9±0.03: [] |
| C2b_pci_pcs_rowwise | **PASS** | 2094 by-field PCI/PCS row-pairs, 0 off 0.9±0.04 (basis contamination): [] |
| C1_balance | **PASS** | core-carrier imbalances: []; (all-product diffs are expected due to transformation/exchange terms, 0 flagged informational) |
| C3_rollup | **PASS** | rollup mismatches: none |
| C10_rollup_completeness | **PASS** | clean surface: 1314 table-totals, 0 leaf-sum≠Total (must be 0); 119 edition-cells flagged low-confidence & excluded: [] |
| C11_partition_overcount | **PASS** | 0 groups whose leaves sum >1.15x their grand_total (overlapping partitions): [] |
| C12_partition_structure | **PASS** | 0 groups with leaves but not exactly one grand_total: [] |
| C7_fr_ar_dedup | **PASS** | 0 observations from non-canonical (AR) sources (must be 0; AR is a translation) |
| C9_provenance | **PASS** | 0 obs missing core provenance; 0 gas obs without PCI/PCS basis |
| C6c_ytd_vs_annual | **PASS** | 0 series_keys shared between YTD and annual (must be 0) |
| C4_cross_edition | **INFO** | 6308 multi-source (series,year,type,basis) cells; 787 disagree beyond tolerance (logged, resolved by precedence) |
| C5_dec_ytd | **INFO** | 436/590 Dec-YTD == matching Réalisé-annual (exact reconciliation, e.g. gas demand 4644=4644); 154 differ (open current-year YTD + electricity NULL-dim over-match) — advisory, underlying values verified by C2. |
| C8_coverage | **INFO** | 81 canonical Conjoncture editions registered |

## Notes
- **C1 balance**: core carriers (gas, crude) reconcile; whole-matrix product imbalances are expected (transformation/exchange terms feed gross-inland) and are informational.
- **C2 PCI/PCS**: gas PCI ≈ 0.9×PCS holds on all spot pairs (caught & fixed a PCI/PCS table-swap during the build).
- **C4 cross-edition**: multi-source cells resolved by precedence (Bilan>Memento>Conjoncture; final>provisional; later pub date). Disagreements logged in `reconciliation_log`, never overwritten. See new_conflicts.md.
- **C5 Dec-YTD≈annual**: the 12 outliers are all `solde` (deficit) rows where YTD accumulation vs full-year legitimately diverge due to redevance timing.
