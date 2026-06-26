# series_catalog.md — Phase E

Every distinct time series in the store (593 series), keyed by the stable `series_id` (= `series_key`: controlled-vocabulary IDs only, stable across re-runs). This seeds the future MCP semantic layer: each row is one sliceable series with its definition, dimensions, unit, calorific basis, period_type, source families, and template-version provenance.

**series_id composition:** `indicator|flow|product|sector|region|field|level|producer|basis|unit|period_type|redevance|scope|technology|regime|geography_scope`.

## Series by indicator

| indicator | #series | unit(s) | basis | period_types | years | families |
|---|---|---|---|---|---|---|
| Achats de gaz | 4 | ktep-pcs,ktep-pci | PCS,PCI | annual,ytd_cumulative | 2010-2026 | conjoncture |
| Bilan d'énergie primaire | 16 | ktep-pci | PCI | ytd_cumulative,annual | 2010-2026 | conjoncture |
| Bilan énergétique (matrice) | 368 | ktep | NA,PCS | annual | 2010-2024 | bilan |
| Consommation de produits pétroliers | 31 | ktep | NA | ytd_cumulative,annual | 2010-2026 | conjoncture/memento |
| Demande de gaz naturel | 20 | ktep-pci,ktep-pcs | PCS,PCI | annual,ytd_cumulative | 2010-2026 | memento/conjoncture |
| Exportation de produits pétroliers | 9 | ktep | NA | annual | 2023-2024 | memento |
| Production d'électricité | 28 | GWh | NA | ytd_cumulative,annual | 2010-2026 | conjoncture/memento |
| Production de gaz naturel | 44 | ktep-pci,ktep-pcs | PCI,PCS | ytd_cumulative,annual | 2010-2026 | conjoncture/memento |
| Production de pétrole brut | 41 | kt | NA | ytd_cumulative,annual | 2018-2026 | conjoncture/memento |
| Redevance / Forfait fiscal | 4 | ktep-pci,ktep-pcs | PCS,PCI | ytd_cumulative,annual | 2010-2026 | conjoncture |
| Ressources en gaz naturel | 10 | ktep-pci,ktep-pcs | PCS,PCI | annual,ytd_cumulative | 2010-2026 | memento/conjoncture |
| Solde énergétique | 4 | ktep-pci | PCI | ytd_cumulative,annual | 2010-2026 | conjoncture |
| Ventes d'électricité | 14 | GWh | NA | annual,ytd_cumulative | 2010-2026 | memento/conjoncture |

Full per-series detail (stable IDs, dimensions) in **series_catalog.csv**.
