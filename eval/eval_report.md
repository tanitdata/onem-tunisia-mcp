# ONEM MCP — Evaluation Report

- **Golden set version:** 1.1.0
- **Overall verdict:** **GO**  (FIX-FIRST iff any Layer-3 gate fails)
- **Layer 1 (retrieval fidelity):** 86/86 checks (100.0%)
- **Layer 3 (adversarial gates):** 10/10 pass, 0 gating failure(s) 
- **Layer 2 (behavioral, model-in-loop):** loaded saved run (bedrock, us.anthropic.claude-opus-4-8, 5 runs/task, re-scored)

> Headline is the per-failure-mode table below — NOT a single global accuracy number (which would let a dangerous mode average out against trivial lookups).

## Per-failure-mode breakdown

| failure mode | deterministic (L1+L3) | layer-2 mean | layers |
|---|---|---|---|
| contested_as_settled | 1/1 (100%) | 100% | 3,2 |
| double_count | 9/9 (100%) | 100% | 1,3,2 |
| no_data_vs_out_of_scope | 3/3 (100%) | 90% | 3,2 |
| pci_pcs_conflation | 20/20 (100%) | 100% | 1,3,2 |
| period_type_mixing | — | 100% | 2 |
| provisional_as_fact | 1/1 (100%) | 100% | 3,2 |
| qualifier_drop | 87/87 (100%) | — | 1,3 |
| scope_confusion | 28/28 (100%) | 100% | 1,3,2 |
| series_misselection | — | 100% | 2 |
| unit_basis_conversion | 9/9 (100%) | 40% | 1,2 |

## Layer 3 — adversarial gates (detail)

| probe | category | gate | result | note |
|---|---|---|---|---|
| L3-compare-pci-pcs | pci_pcs_conflation | GATE | PASS | PCI vs PCS must be refused as a category error |
| L3-compare-local-inclexp | scope_confusion | GATE | PASS | local (17089) vs incl_exports (17197) must be refused |
| L3-compare-aggrole-doublecount | double_count | GATE | PASS | grand_total beside its own BT/MT/HT leaves must be refused or warned (aggregation_role guard) |
| L3-list-prices-out-of-scope | no_data_vs_out_of_scope | GATE | PASS | deferred family term must say out_of_scope, never bare n:0 ok |
| L3-list-refining-out-of-scope | no_data_vs_out_of_scope | GATE | PASS | deferred family term must say out_of_scope |
| L3-search-brent-out-of-scope | no_data_vs_out_of_scope |  | PASS | price query should carry an out-of-scope note, not imply absence |
| L3-force-compare-hardwarn | pci_pcs_conflation |  | PASS | force=true must still carry a loud hard-warning |
| L3-conflicts-contested | contested_as_settled | GATE | PASS | the ~787 disagreements must surface; empty => contested reads as settled (G-1) |
| L3-getseries-qualifiers | qualifier_drop | GATE | PASS | no bare numbers: every point carries the full qualifier envelope |
| L3-provisional-flagged | provisional_as_fact |  | PASS | provisional points must be visibly flagged via data_status |

## Layer 2 — per-task trajectory scores

Model: `us.anthropic.claude-opus-4-8`, 5 run(s)/task. Mean pass over runs (stochastic).

| task | category | mean pass | variance |
|---|---|---|---|
| T01 | series_misselection | 1.00 | 0.000 |
| T02 | pci_pcs_conflation | 1.00 | 0.000 |
| T03 | scope_confusion | 1.00 | 0.000 |
| T04 | double_count | 1.00 | 0.000 |
| T05 | double_count | 1.00 | 0.000 |
| T06 | no_data_vs_out_of_scope | 0.80 | 0.160 |
| T07 | no_data_vs_out_of_scope | 1.00 | 0.000 |
| T08 | period_type_mixing | 1.00 | 0.000 |
| T09 | contested_as_settled | 1.00 | 0.000 |
| T10 | provisional_as_fact | 1.00 | 0.000 |
| T11 | unit_basis_conversion | 0.40 | 0.240 |
| T12 | scope_confusion | 1.00 | 0.000 |

## How to read this

- **Any Layer-3 gate FAIL → FIX-FIRST.** Those are the category errors the design claims to prevent; a regression there means a model can be led to a wrong/uncaught answer.
- **Layer 1** is value+qualifier fidelity vs the clean views. A drop here means the interface is corrupting or stripping what the store holds.
- **Layer 2** is the consuming-model's *behavior*; read per category, mind the variance. It is the lower-trust layer (a stochastic model + soft checks) — weight L1/L3 first.
- A *value* that looks wrong is an audit carry-forward, NOT an MCP defect (this eval grounds on the DB, not the source PDFs).
