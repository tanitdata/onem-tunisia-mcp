# 04 — Footnote & Caveat Catalog

Every methodological footnote, asterisk, and definitional caveat found, mapped to the **series/cells
it qualifies**. These are not decoration: each one changes what the number *means*. In the schema they
become rows in `footnote` linked many-to-many to observations (`observation_footnote`). Footnotes that
are *definitions of a series* should also populate the indicator's `definition` field.

`FN-id` = stable footnote key. `Type`: `definition` | `scope` | `provenance/status` | `toggle` |
`spec-change` | `aggregate-membership` | `units-basis`.

---

## Cross-cutting / highest-impact

| FN-id | Type | Text (verbatim/condensed) | Applies to | Effect on meaning |
|---|---|---|---|---|
| **FN-REDEVANCE-TOGGLE** | toggle | "(3) DEFICIT en considérant la redevance comme étant une ressource nationale" / "(4) … la redevance ne fait pas partie des ressources nationales" | M-T19 SOLDE rows; C-T1 SOLDE rows; taux d'indépendance (text p7 Conjoncture, p15 Memento) | **The solde and the independence rate each exist in TWO versions.** Algerian gas royalty counted as national resource (→ smaller deficit, higher independence: 41% / 35%) vs not (→ larger deficit, lower: e.g. 29%). Store as two separate observations distinguished by a `redevance_included` attribute. NEVER average. |
| **FN-PCI-PCS** | units-basis | "1 PCI = 0,9 PCS"; "Le gaz naturel est comptabilisé … en PCI, seule la quantité de gaz commercial sec est prise en compte (gaz sec)" | All gas/redevance/achats/demande tables (M-T5/6/7/8/16/17, C-T1/11/12/15/16, gas prices) | Gas reported in BOTH ktep-pci and ktep-pcs. Bilan uses **PCI dry-gas** convention. Keep `calorific_basis` first-class; never blend. |
| **FN-PROVISIONAL** | provenance/status | "(*) Données provisoires"; "(*) Données provisoires pour le mois d'avril 2026"; "Données provisoires avant audit" | Entire Conjoncture (esp. C-T1, C-T2, C-T10, C-T14); Memento price tables M-T28/T29 2024 col | Mark `data_status = provisional`. Conjoncture annual/ytd values are provisional and may be superseded by Memento/Bilan finals. |
| **FN-BILAN-VERSION** | provenance/status | "Version 1: juillet 2025 / Version 2: Novembre 2025" | All Bilan observations (B-T1/2/3) | Record source version. v2 supersedes v1; keep both if v1 file appears. |
| **FN-BALANCE-METHOD** | scope/definition | "Les ressources et la demande … calculés selon l'approche classique du bilan c.à.d sans tenir compte de la biomasse-énergie, ni de l'autoconsommation des champs, ni de la consommation des stations de compression du gazoduc trans-méditerranéen" | M-T19, C-T1 (primary energy balance) | Defines the boundary of "primary energy balance" — excludes biomass, field self-consumption, transmed compression. |
| **FN-DEMAND-NONENERGY** | scope | "Demande des produits pétroliers: hors consommation non énergétique (lubrifiants + bitumes + W Spirit)" | M-T19, C-T1 petroleum-products demand line | Primary-balance PP demand excludes non-energy products. Differs from M-T15/C-T14 total consumption. |

---

## Oil / crude production

| FN-id | Type | Text | Applies to | Effect |
|---|---|---|---|---|
| FN-OIL-GPLCOND-INCL | scope | "Y compris GPL primaire et condensât Gabès" | M-T2 (kt), M-T4 (Production/Export rows) | Production figure **includes** primary LPG + Gabès condensate → totals differ from "sans" version. |
| FN-OIL-GPLCOND-EXCL | scope | "* Sans GPL primaire et sans condensât Gabès" | M-T3 (barils/jour), M-T20 (par gouvernorat) | **Excludes** them → different total (28 547 b/j vs the kt total). Pair with FN-OIL-GPLCOND-INCL. |
| FN-CRUDE-MAP | units-basis | "MAP = Moyenne annuelle pondérée de la production nationale" tep/t = 1.023 | crude tep/t conversion (M-T32) | Conversion factor used to turn kt → ktep for crude. |
| FN-COND-EXPORT | scope | "(1) y compris condensats exportés par ETAP (Condensat Miskar et Hasdrubal mélange + condensat Gabès)" | C-T2 PETROLE BRUT export row | Crude export includes ETAP condensates. |
| FN-CRUDE-ESTIM | provenance/status | "La production du mois d'avril 2026 est estimée" | C-T10 (à fin avril 2026) | Last month estimated → status estimated within a provisional cumulative. |

---

## Natural gas

| FN-id | Type | Text | Applies to | Effect |
|---|---|---|---|---|
| FN-GAZSUD-MEMBERS-M | aggregate-membership | "Gaz commercial du sud: quantité de gaz traité de SITEP, Sonatrach El Borma, SITEP EB 407, Oued Zar, Adam, Djebel Grouz, Cherouk, Durra, Anaguid Est, Bochra, et Abir" | M-T5, M-T6 "Gaz Com. de Sud" row | Defines aggregate membership (Memento version). |
| FN-GAZSUD-MEMBERS-C | aggregate-membership | "Gaz commercial du sud: … d'El borma, Oued Zar, Djbel Grouz, Adam, ChouchEss., Cherouk, Durra, anaguid Est, Bochra et Abir" | C-T11/T12 "Gaz Com Sud" row | **Different member list** than Memento (adds ChouchEss., drops SITEP/EB407) → OQ-F1. |
| FN-GAS-COMMERCIAL | definition | "Seules les quantités de gaz commercial sont rapportées … gaz duquel les liquides … ont été extraits" | all gas production | Accounted gas = dry commercial gas only. |
| FN-NAWARA-START | provenance | "Début de commercialisation de gaz de Nawara le 29 mars 2020" | Nawara gas rows | Series starts 2020; pre-2020 = 0/absent legitimately. |
| FN-GHRIB-START | provenance | "Début de commercialisation du gaz de la concession Ghrib le 4/11/2017" | Franig/Ghrib aggregate | |
| FN-ANAGUID-DURRA-START | provenance | "Anaguid Est depuis 23/01/2017 et Durra depuis 9/01/2017" | Gaz Com Sud | |
| FN-BOCHRA-ABIR-START | provenance | "Bouchra et Abir en mars 2021" | Gaz Com Sud | |
| FN-GPLPRIM-DEF | definition | "(2) GPL champs hors Franig/Baguel/terfa et Ghrib + GPL usine Gabes" | C-T1 GPL primaire (2); M-T19 GPL primaire | Defines primary-LPG scope (excludes some fields). |
| FN-REDEVANCE-OVERDRAW | provenance | "dépassement des prélèvements STEG sur la redevance revenant à l'Etat … 240 millions de Cm3, en cours de régularisation" (fin déc 2025 / 2025) | redevance rows C-T1, C-T11/12, C-T2 | Data-quality caveat on redevance totals 2025-2026. |
| FN-REDEVANCE-TRADE | scope | "(2) la redevance totale (reçue en nature et cédée à la STEG + reçue en espèce et rétrocédée) prise en compte dans la balance commerciale comme importation à valeur nulle" | C-T2 REDEVANCE row | Royalty enters trade balance as zero-value import. |
| FN-ACHAT-STEG-2015 | provenance | "(5) Cession de gestion du contrat d'achat gaz de l'ETAP à la STEG à partir de juillet 2015" | achats rows C-T2/C-T11 | Operator change mid-2015 — affects series continuity. |

---

## Petroleum products / refining

| FN-id | Type | Text | Applies to | Effect |
|---|---|---|---|---|
| FN-PP-STIR-ONLY | scope | "(2) production STIR uniquement" | M-T10 Production de produits pétroliers | Production = STIR refinery only (not all sources). |
| FN-PP-INTERNALCONS | scope | "(1) y compris la consommation interne" | M-T10 | Includes internal consumption. |
| FN-PP-CONS-AUTOSTIR | scope | "*Y compris auto-consommation STIR" | M-T15 Consommation de produits pétroliers | Total consumption includes STIR self-consumption. Conjoncture C-T14 separates "Cons finale (Hors STEG&STIR)". |
| FN-GASOIL-50PPM | spec-change | "(*) Gasoil 50 ppm avant 2017"; "nouvelle spécification à partir du 1er janvier 2017: Gasoil sans soufre au lieu de Gasoil 50 ppm" | gasoil ordinaire/SS series (M-F6, C-T2 note 6) | Product spec changed 2017 → pre/post not strictly same product. |
| FN-STIR-IMPORT-2015 | provenance | "(3) Importation STIR à partir de 2015" | C-T2 PETROLE BRUT import | Crude import attributed to STIR from 2015. |
| FN-PETCOKE-ESTIM | provenance/status | "(4) Chiffres estimés" (coke de pétrole); "données partiellement estimées" | C-T2 / C-T14 coke de pétrole | Petcoke figures estimated. |
| FN-STIR-SHUTDOWN | provenance | "STIR à l'arrêt de janvier à avril 2025 … Depuis mai 2025 Topping a repris"; "Arrêt Platforming depuis janvier 2024" | C-T13 refining indicators, C-T14 | Explains zero/low 2025 production. |
| FN-STIR-COVERAGE | definition | "(1) taux couverture en tenant compte de la totalité de la production / (2) uniquement production destinée au marché local" | C-T13 taux couverture rows | Two different coverage ratios. |

---

## Electricity

| FN-id | Type | Text | Applies to | Effect |
|---|---|---|---|---|
| FN-ELEC-NOAUTOTHERM | scope | "* Hors autoproducteurs thermiques" | M-T12 Production d'électricité | Excludes thermal autoproducers. |
| FN-ELEC-NOAUTOSELF | scope | "* Hors autoproduction autoconsommée" | M-T13, M-T22, C-T21 | Excludes self-consumed autoproduction. |
| FN-ELEC-AUTOCONSPV | scope | "Y compris autoproduction PV" / "y compris autoproduction renouvelable" | M-T13 STEG row, C-T20 production national | Includes PV autoproduction (contrast with above). |
| FN-ELEC-SALES-FRAUD | scope | "(2) Y compris fraudes et proratas" | M-T22 ventes by district | Sales include fraud & prorated estimates. |
| FN-ELEC-SALES-NOLIBYA | scope | "** sans tenir compte des ventes à la Libye et hors autoproduction consommée" | C-T21 TOTAL VENTES | Excludes Libya sales. |
| FN-ELEC-BIMESTRIAL | provenance/status | "statistiques basées sur la facturation bimestrielle, dont près de la moitié est estimée" (BT) | C-T21 Basse tension | BT sales ~half estimated. |
| FN-ELEC-AUTOPROD-BTMT | scope | "(1) la production des autoproducteurs est comptabilisée (BT+MT)" | C-T20 autoproducteurs | |
| FN-ELEC-DISPO-DEF | definition | "(2) Production national + Echanges + achat Sonelgaz, Gecol − ventes Gecol" | C-T20 Disponible pour marché local | Definition of available-for-market. |
| FN-ELEC-IMPORT-NET | definition | "(5) Importation d'électricité net = Electricité importé − Electricité exporté + Solde d'échange" | C-T1 / elec import | |
| FN-ELEC-IPP-REGIME-2023 | spec-change | "A partir de janvier 2023, production solaire régime autorisations → 'IPP solaire'"; "janvier 2024 … autoproduction ER comptabilisée"; "décembre 2025 … régime concessions → IPP solaires" | C-T20 IPP/autoprod rows | Accounting reclassifications over time — series breaks. |
| FN-ELEC-GENSALES-DIFF | scope | Ventes 17 197 (M-T18) vs district total 17 090 (M-T22) | M-T18 vs M-T22 | Internal Memento discrepancy (~107 GWh) → OQ. |

---

## Prices / economics

| FN-id | Type | Text | Applies to | Effect |
|---|---|---|---|---|
| FN-PRICE-PROV-AUDIT | provenance/status | "(*) Données provisoires avant audit" | M-T28/T29 (2024 prices) | Provisional pre-audit. |
| FN-PRICE-WEIGHTED | definition | "(1) Prix moyen pondéré"; "moyennes pondérées par la quantité sur la période" | C-T5, C-T6, crude import/export prices | Quantity-weighted averages (not simple means). |
| FN-PRICE-RETAIL-DATE | provenance | "(4) Prix de vente en vigueur au public à partir du 24/11/2022" | C-T6 prix de vente | Administered price effective date. |
| FN-PRICE-TAXES-DEF | definition | "(2) Droits et Taxes: DC + RPD (3% du DC) + TVA (13-19%)"; "(3) Divers et Marges: frais mise en place + marge sociétés + forfait transport + stockage sécurité + marge revendeurs" | C-T6 price decomposition | Defines price-component columns. |
| FN-PRICE-SUBSIDY | definition | "(1) Résultat unitaire = différentiel prix de vente − coût de revient, pas forcément identique à la subvention budgétaire" | C-T8/T9 résultat unitaire | Not the budgetary subsidy. |
| FN-FX-CONVENTION | units-basis | Memento M-T26 column "Taux de change moyen (en $/DT)" but values (1.43…3.11) are DT/$ | M-T26 FX column | **Header/unit mismatch**: labeled $/DT, values are DT/$ → OQ. |

---

## Trade

| FN-id | Type | Text | Applies to | Effect |
|---|---|---|---|---|
| FN-TRADE-NOTCUSTOMS | provenance | "(1) … se base sur les données des sociétés importatrices et exportatrices … et non pas sur les déclarations douanières" | C-T2 entire trade table | Source = companies, not customs. |
| FN-TRADE-PARTNERS-INS | provenance/status | "(7) Données des exportations des partenaires estimées à partir des données de l'INS" | C-T2 PARTENAIRES rows | Partner exports estimated from INS. |
