"""
load_conjoncture.py — reference loader for the Conjoncture énergétique (monthly YTD).

THE alignment-critical loader. Each table has the column model proved in
00_alignment_check.md:

    [ Réalisé 20XX (annual) | 2015 (a) ytd | 20YY (b) ytd | 20ZZ (c) ytd | Var% | TCAM% ]

- Réalisé column      -> period_type=annual,  full prior year, provisional
- (a)/(b)/(c) columns -> period_type=ytd_cumulative, Jan 1 .. cutoff, the report's
  cutoff month is read FROM the report (here avril -> 4), NOT assumed.
- Var% / TCAM% columns -> derived, NOT ingested (recomputed downstream; Trap 9).

The reference period years (Réalisé year, and the three YTD years) and the cutoff
month are detected from the header, so this same loader generalises to other editions.
Redevance toggle (Trap 3) -> two SOLDE rows -> redevance_toggle_id incl/excl.
"""
import re
import fitz
import onem_lib as L

# Column value-anchors for the standard 6-col Conjoncture table on C-T1-style pages.
# Values right-align; anchors are the right-ish x of each value column.
STD_ANCHORS = {"real": 250.0, "a": 310.0, "b": 361.0, "c": 412.0, "var": 461.0, "tcam": 513.0}

# C-T1 primary-balance rows -> indicator + dimensions
CT1_ROWS = {
    "RESSOURCES":            dict(indicator="primary_balance", flow="flow.resources", is_total=True),
    "Pétrole":               dict(indicator="primary_balance", flow="flow.primary_production", product="prod.crude_oil", scope="incl_gpl_condensat"),
    "GPL primaire":          dict(indicator="primary_balance", flow="flow.primary_production", product="prod.lpg", scope="gpl_primaire"),
    "Gaz naturel":           dict(indicator="primary_balance", flow="flow.resources", product="prod.natural_gas", is_total=True),
    "Production":            dict(indicator="gas_production", flow="flow.primary_production", product="prod.natural_gas", scope="commercial_dry"),
    "Redevance":             dict(indicator="redevance", flow="flow.royalty", product="prod.natural_gas"),
    "Elec primaire":         dict(indicator="primary_balance", flow="flow.primary_production", product="prod.electricity", scope="re_sourced"),
    "DEMANDE":               dict(indicator="primary_balance", flow="flow.demand", is_total=True),
    "Produits pétroliers":   dict(indicator="primary_balance", flow="flow.demand", product="prod.petroleum_products_total"),
    # 'Gaz naturel' under DEMANDE handled by position (2nd occurrence) -> see loader
    "Avec comptabilisation de la redevance": dict(indicator="solde", flow="flow.solde", redevance_toggle_id="incl", redevance_included=True),
    "Sans comptabilisation de la redevance": dict(indicator="solde", flow="flow.solde", redevance_toggle_id="excl", redevance_included=False),
}


def parse_header_years(grid, y0, y1):
    """Read the Réalisé year and the (a)/(b)/(c) YTD years + cutoff from the header band.

    Layout (proved): the (a)(b)(c) YTD years sit on ONE header line under the
    'A fin <month>' label, left-to-right; the Réalisé year sits on its own line under
    the 'Réalisé en' label (a different x and y). Strategy: group year tokens by line
    (y); the line carrying >=3 year tokens is the YTD triplet (a,b,c in x-order); the
    Réalisé year is the remaining year token (typically the one nearest the 'Réalisé'
    label x and not on the triplet line)."""
    toks = [(w[1], w[0], w[4]) for w in grid.words if y0 <= w[1] <= y1]
    # cutoff month from 'A fin <month>'
    joined = " ".join(t for _, _, t in sorted(toks))
    cutoff = None
    for name, m in sorted(L.MONTHS_FR.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b" + re.escape(name), joined, re.IGNORECASE):
            cutoff = m
            break
    # group year tokens by line (rounded y)
    by_line = {}
    for yy, xx, t in toks:
        if re.fullmatch(r"20\d{2}", t):
            by_line.setdefault(round(yy), []).append((xx, int(t)))
    # YTD line = the line with the most year tokens (>=3 ideally)
    triplet_line = max(by_line, key=lambda k: len(by_line[k])) if by_line else None
    ytd_years = [y for _, y in sorted(by_line.get(triplet_line, []))]
    # Réalisé year = a year token NOT on the triplet line
    real_year = None
    for k, items in by_line.items():
        if k == triplet_line:
            continue
        for _, y in items:
            real_year = y
    if real_year is None and len(ytd_years) >= 4:
        real_year = ytd_years.pop(0)
    return dict(real_year=real_year, ytd_years=ytd_years[:3], cutoff_month=cutoff,
                all_years=sorted({y for items in by_line.values() for _, y in items}))


def period_for_column(col, hdr):
    """Map a column key to (period_type, start, end, ref_year, cutoff)."""
    cm = hdr["cutoff_month"] or 4
    last_day = {1:31,2:28,3:31,4:30,5:31,6:30,7:31,8:31,9:30,10:31,11:30,12:31}[cm]
    if col == "real":
        y = hdr["real_year"]
        return ("annual", f"{y}-01-01", f"{y}-12-31", y, None)
    idx = {"a":0,"b":1,"c":2}[col]
    y = hdr["ytd_years"][idx]
    return ("ytd_cumulative", f"{y}-01-01", f"{y}-{cm:02d}-{last_day:02d}", y, cm)


def period_for_column_n(col, hdr, ncols):
    """Period mapping for tables that DROP the 2015(a) baseline column. C-T10/C-T13 use
    a 4-value layout [Réalisé | à-fin (b) prev-year | à-fin (c) cur-year | Var]; there is
    no (a) baseline and no TCAM. We detect this via ncols and shift the YTD year indices:
    the two YTD columns are the two most-recent ytd_years (b,c), not (a,b)."""
    if ncols >= 5:                      # standard 6-col model (real,a,b,c,var,tcam)
        return period_for_column(col, hdr)
    cm = hdr["cutoff_month"] or 4
    last_day = {1:31,2:28,3:31,4:30,5:31,6:30,7:31,8:31,9:30,10:31,11:30,12:31}[cm]
    yrs = hdr["ytd_years"]
    bc = yrs[-2:] if len(yrs) >= 2 else yrs   # the two most-recent YTD years
    if col == "real":
        y = hdr["real_year"]; return ("annual", f"{y}-01-01", f"{y}-12-31", y, None)
    if col == "b":
        y = bc[0]
    elif col == "c":
        y = bc[-1]
    else:
        return None
    return ("ytd_cumulative", f"{y}-01-01", f"{y}-{cm:02d}-{last_day:02d}", y, cm)


def status_for(col, report_type="conjoncture"):
    # Conjoncture: Réalisé annual = provisional; YTD = provisional; current-month estimated handled per-table
    return "provisional"


def detect_anchors_from_row(grid, ry, expect=6):
    """Derive the value-column x-centers by clustering the numeric tokens on a known
    row (RESSOURCES). Returns dict {real,a,b,c,var,tcam} mapped left->right. This makes
    the loader follow per-edition x-drift (the 6-col layout is stable; the x's are not)."""
    toks = sorted((x0, x1, t) for (x0, x1, t) in grid.row_tokens(ry, tol=5.0)
                  if L._looks_numeric(t) or t.startswith("-"))
    # Merge split numeric fragments ('5','024' -> one value) by the EDGE gap between a
    # token's start and the previous token's end. Fragments of one number sit flush
    # (gap ~0-6pt); separate columns are ~20pt+ apart. Edge-gap is robust to the
    # per-edition font/spacing drift that broke center-to-center clustering.
    centers = []
    cur = []          # list of (x0, x1)
    for x0, x1, t in toks:
        if cur and x0 - cur[-1][1] <= 14:
            cur.append((x0, x1))
        else:
            if cur:
                centers.append((cur[0][0] + cur[-1][1]) / 2)
            cur = [(x0, x1)]
    if cur:
        centers.append((cur[0][0] + cur[-1][1]) / 2)
    # Map detected centers to column keys by COUNT (per-edition layouts vary):
    #   6 -> real,a,b,c,var,tcam   5 -> real,a,b,c,var
    #   4 -> real,b,c,var (no 2015 baseline, no TCAM — e.g. C-T10/C-T13)
    #   3 -> real,b,c
    n = len(centers)
    if n >= 5:
        keys = ["real", "a", "b", "c", "var", "tcam"]
    elif n == 4:
        keys = ["real", "b", "c", "var"]
    else:
        keys = ["real", "b", "c"][:n]
    out = {keys[i]: centers[i] for i in range(min(n, len(keys)))}
    out["_ncols"] = n
    return out


def load_ct1(db, grid, source_id, template_version, page="5"):
    """Load the C-T1 primary energy balance (mixed period types + redevance toggle).
    Header years/cutoff and value-column anchors are detected per edition."""
    # locate the RESSOURCES row to anchor geometry
    ress_y = None
    for w in grid.words:
        if w[4] == "RESSOURCES":
            ress_y = w[1]; break
    if ress_y is None:
        return 0, {"real_year": None, "ytd_years": [], "cutoff_month": None}
    hdr = parse_header_years(grid, max(0, ress_y-65), ress_y-2)
    anchors = detect_anchors_from_row(grid, ress_y)
    anchors.pop("_ncols", None)
    if len(anchors) < 4:
        anchors = dict(STD_ANCHORS)
    global STD_ANCHORS_DYN
    STD_ANCHORS_DYN = anchors
    n = 0
    seen_gaz = 0
    matched_once = set()    # each row label maps once (except 'Gaz naturel' twice)
    # Rows are driven by the y of the VALUE tokens (the 'real' column near x~247).
    # The label may be split across 1-2 sub-lines near that y, so we gather label
    # words within +/-9pt of the value-row y. This tolerates the 'Produits/pétroliers'
    # two-line label and footnote markers.
    real_x = anchors.get("real", 250.0)
    last_x = anchors.get("tcam", anchors.get("c", real_x+260)) + 20
    # scan from just above RESSOURCES down ~290pt (covers RESSOURCES..SOLDE block)
    y_lo, y_hi = ress_y - 5, ress_y + 290
    value_ys = sorted({round(w[1], 1) for w in grid.words
                       if y_lo <= w[1] <= y_hi and abs((w[0]+w[2])/2 - real_x) <= 14
                       and L._looks_numeric(w[4])})
    # de-dupe value rows that are within 3pt of each other
    rows_y = []
    for y in value_ys:
        if not rows_y or y - rows_y[-1] > 3:
            rows_y.append(y)
    label_x_max = real_x - 6
    for y in rows_y:
        label = " ".join(
            w[4] for w in sorted(grid.words, key=lambda z: z[0])
            if abs(w[1] - y) <= 9 and w[0] < label_x_max and not L._looks_numeric(w[4])
        ).strip()
        if not label:
            continue
        # find the row spec
        spec = None
        nl = L.norm_label(label)
        for k, v in CT1_ROWS.items():
            if nl.startswith(L.norm_label(k)):
                spec = dict(v); matched_key = k
                break
        if spec is None:
            continue
        # 'Gaz naturel' appears twice: 1st under RESSOURCES (resources), 2nd under DEMANDE (demand)
        if matched_key == "Gaz naturel":
            seen_gaz += 1
            if seen_gaz > 2:
                continue
            if seen_gaz == 2:
                spec = dict(indicator="primary_balance", flow="flow.demand", product="prod.natural_gas")
        else:
            if matched_key in matched_once:
                continue        # ignore re-matches from a following sub-table
            matched_once.add(matched_key)
        cells = grid.assign_cells(y, anchors, tol=6.0,
                                  cell_xmin=real_x-20, cell_xmax=last_x)
        for col in ("real", "a", "b", "c"):
            tok = cells.get(col)
            val = L.parse_number(tok) if tok else None
            if val is None:
                continue
            pt, ps, pe, ry, cm = period_for_column(col, hdr)
            fns = ["FN-PROVISIONAL", "FN-BALANCE-METHOD"]
            if spec.get("product") == "prod.natural_gas" or spec["indicator"] in ("gas_production","redevance"):
                fns.append("FN-PCI-PCS")
            if spec.get("redevance_toggle_id"):
                fns.append("FN-REDEVANCE-TOGGLE")
            # S-1: the 240 Mm³ "en cours de régularisation" STEG↔State overdraw caveat
            # (FN-REDEVANCE-OVERDRAW) is the decisive explanation for the 2025→2026
            # redevance drop. 04_footnotes.md scopes it to redevance rows for 2025/2026;
            # attach it to those observations so describe_series can surface it (it was
            # stranded with 0 links). Both PCI and PCS legs of the redevance series.
            if spec["indicator"] == "redevance" and ry in (2025, 2026):
                fns.append("FN-REDEVANCE-OVERDRAW")
            db.upsert_observation(
                indicator_id=spec["indicator"], value=val, value_raw=tok,
                unit_id="ktep-pci", calorific_basis="PCI", basis_confidence="stated",
                period_type=pt, period_start=ps, period_end=pe, ref_year=ry,
                ytd_cutoff_month=cm, data_status=status_for(col),
                source_id=source_id, source_page=page, source_ref="C-T1",
                template_version=template_version,
                extraction_method="coordinate_map",
                source_cell=f"row={matched_key}|col={col}",
                is_total=bool(spec.get("is_total", False)),
                flow_id=spec.get("flow"), product_id=spec.get("product"),
                scope=spec.get("scope"),
                redevance_toggle_id=spec.get("redevance_toggle_id"),
                redevance_included=spec.get("redevance_included"),
                footnotes=fns)
            n += 1
    return n, hdr


# =====================================================================
# GENERIC tabular-breakdown engine (Blocker 1)
# A TableSpec describes one Conjoncture breakdown table. The engine locates it by a
# title token, derives the 6-column anchors from an anchor row (same per-edition
# geometry as C-T1), then walks value-rows mapping each label via a resolver. This
# reuses the proven column model so the new tables share C-T1's alignment guarantees.
# =====================================================================

# ---- row resolvers (label -> dimension dict) reused/added ----
# keys are NORMALIZED (norm_label: lowercase, no accents/punct). '+' -> space.
ELEC_SRC_C = {
    "fuel gasoil":   dict(technology="thermal_fuel", producer_id="prod.steg"),
    "gaz naturel":   dict(technology="thermal_gas", producer_id="prod.steg"),
    "hydraulique":   dict(technology="hydro", producer_id="prod.steg"),
    "eolienne":      dict(technology="wind", producer_id="prod.steg"),
    "solaire":       dict(technology="pv", producer_id="prod.steg"),
    "steg":          dict(producer_id="prod.steg", is_total=True),
    "ipp gaz naturel": dict(technology="thermal_gas", producer_id="prod.ipp"),
    "ipp solaire":   dict(technology="pv", producer_id="prod.ipp"),
    "autoproducteurs solaire": dict(technology="pv", producer_id="prod.autoproducteurs"),
    "achat tiers":   dict(producer_id="prod.tiers"),
    # supply-balance rows below PRODUCTION NATIONALE: not production components ->
    # flag is_total so they're excluded from the production rollup (a different partition)
    "echanges":      dict(flow="flow.exchanges_transfers", is_total=True),
    "exportation":   dict(flow="flow.export", is_total=True),
    "importation":   dict(flow="flow.import", is_total=True),
    "production pour marche local": dict(scope="market_local", is_total=True),
    "disponible pour marche local": dict(scope="available_local", is_total=True),
    "production nationale": dict(is_total=True),
}
# Gas demand has TWO partitions of the SAME total (OQ-M2):
#   USAGE:    DEMANDE = production_électrique + hors_prod_élec
#   PRESSURE: DEMANDE = Haute pression + Moy&Basse pression   (= hors_prod_élec, actually
#             HP+MBP sum to the NON-power part in Memento; in Conjoncture they tile DEMANDE)
# Canonical leaf partition = USAGE (consistent with load_memento). The PRESSURE rows are
# an ALTERNATIVE breakdown -> is_total=TRUE (excluded from default sum, still queryable).
GAS_DEMAND_C = {
    "demande":            dict(flow="flow.demand", is_total=True),
    "production d electricite": dict(flow="flow.demand", scope="power_generation"),   # leaf
    "hors prod elec":     dict(flow="flow.demand", scope="non_power"),                # leaf
    "hors prod electrique": dict(flow="flow.demand", scope="non_power"),              # leaf
    "haute pression":     dict(level="lvl.hp", flow="flow.demand", is_total=True),    # alt
    "moy basse pression": dict(level="lvl.mbp", flow="flow.demand", is_total=True),   # alt
}
LEVEL_C = {"haute tension":"lvl.ht", "moyenne tension":"lvl.mt", "moyen tension":"lvl.mt",
           "basse tension":"lvl.bt"}

# In the PP-consumption table the CANONICAL leaf partition is the mid-level product
# lines (GPL, Essences, Gasoil, Fuel, Pétrole lampant, Jet, Coke...) that sum directly
# to the Total. Their SUB-children (Essence Sans Pb / Super / premium; Gasoil ordinaire
# / SS / premium; Fuel STEG&STIR / Hors) are a deeper breakdown -> flag is_total so the
# detail view keeps the canonical leaves and the rollup sums exactly once. Deeper detail
# stays queryable in v_series, just excluded from naive summation.
_PP_SUBCHILDREN = {"prod.gasoil_ordinaire", "prod.gasoil_ss", "prod.gasoil_premium",
                   "prod.gasoline_ssp", "prod.gasoline_super", "prod.gasoline_premium"}

def _resolve_product(vocab, label):
    nl = L.norm_label(label)
    if nl in ("total", "total produits petroliers", "consommation totale"):
        return {"product": "prod.petroleum_products_total", "is_total": True}
    pid = vocab.match("product", label)
    if not pid:
        return None
    return {"product": pid, "is_total": pid in _PP_SUBCHILDREN}

def _resolve_field(vocab, label):
    nl = L.norm_label(label)
    if nl == "total":
        return {"is_total": True}
    fid = vocab.match("field", label)
    return {"field": fid} if fid else None

def _resolve_gas_demand(vocab, label):
    nl = L.norm_label(label)
    for k, v in GAS_DEMAND_C.items():
        if nl.startswith(k):
            return dict(v)
    return None

def _resolve_elec_src(vocab, label):
    nl = L.norm_label(label)
    for k, v in ELEC_SRC_C.items():
        if nl.startswith(k):
            return dict(v)
    return None

def _resolve_level(vocab, label):
    nl = L.norm_label(label)
    if nl.startswith("total"):
        return {"is_total": True, "flow": "flow.sales"}
    for k, lid in LEVEL_C.items():
        if nl.startswith(k):
            return {"level": lid, "flow": "flow.sales"}
    return None

def _resolve_gasfield(vocab, label):
    """C-T11/12: by-field gas + redevance + achats rows. Three DISTINCT aggregate lines
    must not collapse to one series_key (the collision guard caught this):
      - 'PRODUCTION NATIONALE +F.Fiscal' = ressources grand total (production+redevance)
      - 'Production nationale'           = gas production only (sum of field rows)
      - 'Redevance totale' / 'Achats'    = their own flows
    """
    nl = L.norm_label(label)
    # Redevance / Achats FIRST (the redevance label contains 'forfait fiscal', which must
    # not be mistaken for the 'PRODUCTION NATIONALE +F.Fiscal' ressources total).
    if nl.startswith("redevance"):
        return {"indicator": "redevance", "flow": "flow.royalty"}
    if nl.startswith("achats") or nl.startswith("achat"):
        return {"indicator": "gas_purchase", "flow": "flow.purchase"}
    # ressources grand total = 'PRODUCTION NATIONALE +F.Fiscal' (production + redevance)
    if nl.startswith("production nationale f") or "nationale f fiscal" in nl \
       or nl.startswith("ressources"):
        return {"indicator": "gas_resources", "flow": "flow.resources", "is_total": True}
    # gas production subtotal (sum of field rows), not the ressources total
    if nl.startswith("production nationale") or nl == "production":
        return {"indicator": "gas_production", "flow": "flow.primary_production",
                "scope": "commercial_dry", "is_total": True}
    if nl.startswith("total"):
        return {"indicator": "gas_production", "flow": "flow.primary_production",
                "scope": "commercial_dry", "is_total": True}
    fid = vocab.match("field", label)
    if fid:
        return {"indicator": "gas_production", "flow": "flow.primary_production",
                "field": fid, "scope": "commercial_dry"}
    return None

# ---- table specs ----  locator = a token at/near the first data row used to anchor.
TABLE_SPECS = [
    dict(ref="C-T14", title="CONSOMMATION DES PRODUITS PETROLIERS", indicator="pp_consumption",
         unit="ktep", basis="NA", flow="flow.consumption", resolver="product",
         anchor_label="GPL",
         # S-1.3: table-wide consumption caveats (were stranded with 0 links) — the
         # non-energy-consumption scope note and the gasoil 50ppm spec change.
         footnotes=["FN-PROVISIONAL","FN-PP-CONS-AUTOSTIR","FN-DEMAND-NONENERGY","FN-GASOIL-50PPM"]),
    dict(ref="C-T15", title="DEMANDE DE GAZ NATUREL", indicator="gas_demand",
         unit="ktep-pci", basis="PCI", resolver="gas_demand", anchor_label="DEMANDE",
         block=0, footnotes=["FN-PROVISIONAL","FN-PCI-PCS"], product="prod.natural_gas",
         derive_total="DEMANDE", derive_flow="flow.demand"),
    dict(ref="C-T16", title="DEMANDE DE GAZ NATUREL", indicator="gas_demand",
         unit="ktep-pcs", basis="PCS", resolver="gas_demand", anchor_label="DEMANDE",
         block=1, footnotes=["FN-PROVISIONAL","FN-PCI-PCS"], product="prod.natural_gas",
         derive_total="DEMANDE", derive_flow="flow.demand"),
    dict(ref="C-T20", title="PRODUCTION D'ELECTRICITE", indicator="electricity_production",
         unit="GWh", basis="NA", flow="flow.primary_production", resolver="elec_src",
         anchor_label="STEG",
         # S-1.3: production-table caveats (were stranded) — autoproducteurs BT+MT
         # counting, the 2023 IPP-regime spec change, and the 'disponible local' def.
         footnotes=["FN-PROVISIONAL","FN-ELEC-AUTOCONSPV","FN-ELEC-AUTOPROD-BTMT",
                    "FN-ELEC-IPP-REGIME-2023","FN-ELEC-DISPO-DEF"],
         derive_total="PRODUCTION NATIONALE", derive_flow="flow.primary_production"),
    dict(ref="C-T21", title="VENTES D'ELECTRICITE", indicator="electricity_sales",
         unit="GWh", basis="NA", resolver="level", anchor_label="Haute",
         # S-1.3: sales-table caveats (were stranded) — bimestrial-billing basis and
         # the 'sans ventes Libye / hors autoproduction' scope of TOTAL VENTES.
         footnotes=["FN-PROVISIONAL","FN-ELEC-BIMESTRIAL","FN-ELEC-SALES-NOLIBYA"],
         derive_total="TOTAL VENTES", derive_flow="flow.sales"),
    # NB anchor on the block-TOP row ("PRODUCTION NATIONALE +F.Fiscal"), NOT on a field
    # like Nawara: fields sit near the BOTTOM of each block, so a Nawara anchor + downward
    # scan would read the NEXT (PCS) block's rows into the PCI table — the basis
    # contamination caught by ground-truth. The PCI block is above the PCS block.
    dict(ref="C-T11", title="RESSOURCES EN GAZ NATUREL", indicator="gas_production",
         unit="ktep-pci", basis="PCI", resolver="gasfield", anchor_label="PRODUCTION",
         block=0, product="prod.natural_gas",
         # S-1.3: Nawara start-of-commercialization provenance (was stranded; Nawara is a live field).
         footnotes=["FN-PROVISIONAL","FN-PCI-PCS","FN-GAZSUD-MEMBERS-C","FN-NAWARA-START"]),
    dict(ref="C-T12", title="RESSOURCES EN GAZ NATUREL", indicator="gas_production",
         unit="ktep-pcs", basis="PCS", resolver="gasfield", anchor_label="PRODUCTION",
         block=1, product="prod.natural_gas",
         footnotes=["FN-PROVISIONAL","FN-PCI-PCS","FN-GAZSUD-MEMBERS-C","FN-NAWARA-START"]),
    dict(ref="C-T10", title="PRODUCTION DES PRINCIPAUX CHAMPS", indicator="crude_production",
         unit="kt", basis="NA", flow="flow.primary_production", product="prod.crude_oil",
         resolver="field", anchor_label="El", scope="excl_gpl_condensat",
         footnotes=["FN-PROVISIONAL","FN-CRUDE-ESTIM"]),
]
RESOLVERS = {"product": _resolve_product, "field": _resolve_field,
             "gas_demand": _resolve_gas_demand, "elec_src": _resolve_elec_src,
             "level": _resolve_level, "gasfield": _resolve_gasfield}


def find_table_page(d, title, occurrence=0):
    """Return the page index whose text contains the (normalized) title token."""
    nt = L.norm_label(title)
    seen = 0
    for p in range(d.page_count):
        if nt in L.norm_label(d[p].get_text()):
            if seen == occurrence:
                return p
            seen += 1
    return None


def load_table(db, vocab, d, spec, source_id, template_version, hdr):
    """Load one breakdown table per spec, using the proven 6-column model + per-edition
    anchors. block=1 selects the 2nd stacked block on a page (e.g. PCS under PCI)."""
    if spec.get("skip"):
        return 0
    # find the page that has BOTH the title and a real anchor data row (>=4 numerics
    # on a line led by anchor_label). This rejects pages that merely mention the title
    # (e.g. C-T1 references 'production d'électricité').
    nt = L.norm_label(spec["title"]); alab0 = L.norm_label(spec["anchor_label"]).split()[0]
    pg = None
    for p in range(d.page_count):
        if nt not in L.norm_label(d[p].get_text()):
            continue
        g = L.GridPage(d[p])
        ok = False
        for w in g.words:
            if L.norm_label(w[4]) == alab0:
                if sum(1 for (x0, x1, t) in g.row_tokens(w[1], tol=5.0)
                       if L._looks_numeric(t)) >= 4:
                    ok = True; break
        if ok:
            pg = p; break
    if pg is None:
        return 0
    grid = L.GridPage(d[pg])
    resolver = RESOLVERS[spec["resolver"]]
    # Candidate anchor rows: a row whose leading label == anchor_label AND which carries
    # >=4 numeric value tokens (i.e. a real data row, not a title/footnote mention).
    alab = L.norm_label(spec["anchor_label"])
    cand = []
    seen_y = set()
    for w in grid.words:
        if L.norm_label(w[4]) != alab.split()[0] and not L.norm_label(w[4]).startswith(alab):
            continue
        ay = round(w[1], 1)
        if ay in seen_y:
            continue
        seen_y.add(ay)
        nnum = sum(1 for (x0, x1, t) in grid.row_tokens(ay, tol=5.0) if L._looks_numeric(t))
        if nnum >= 4:
            cand.append(ay)
    cand = _distinct_blocks(sorted(cand))
    block = spec.get("block", 0)
    if block >= len(cand):
        return 0
    ay = cand[block]
    blocks = cand
    anchors = detect_anchors_from_row(grid, ay)
    ncols = anchors.pop("_ncols", len(anchors))
    if len([k for k in anchors if not k.startswith("_")]) < 3:
        return 0
    real_x = anchors.get("real")
    last_x = anchors.get("tcam", anchors.get("var", anchors.get("c", real_x+260))) + 20
    # ------------------------------------------------------------------
    # Band detection (Blocker A fix): a table block's data rows run from its anchor
    # (always the block's FIRST row — GPL for C-T14, DEMANDE for C-T15/16) DOWNWARD to
    # the next stacked block's anchor (PCS under PCI), with NO fixed length cap. The
    # round-1 bug was a too-short fixed window (ay+170) that cut off Jet/Coke/Total;
    # the fix is to fence on the next block and follow the contiguous run all the way
    # down. (A bidirectional scan was rejected: it bled a block's rows into its
    # neighbour, since PCI sub-rows sit between the PCI and PCS anchors.)
    # ------------------------------------------------------------------
    next_block = min([b for b in blocks if b > ay + 8], default=None)
    hard_hi = (next_block - 6) if next_block is not None else 1e9
    down_value_ys = sorted({round(w[1], 1) for w in grid.words
                            if ay - 4 <= w[1] <= hard_hi
                            and abs((w[0]+w[2])/2 - real_x) <= 16 and L._looks_numeric(w[4])})
    collapsed = []
    for y in down_value_ys:
        if not collapsed or y - collapsed[-1] > 3:
            collapsed.append(y)
    # follow the contiguous run from the anchor downward; a large gap ends the block
    # (footnote prose / next section). Allow up to ROW_GAP between adjacent data rows.
    # 30pt tolerates a slightly larger gap before the PRODUCTION NATIONALE total row
    # (observed 27pt after ACHAT TIERS) without bleeding into the following prose.
    ROW_GAP = 30.0
    rows_y = []
    for y in collapsed:
        if not rows_y:
            if abs(y - ay) <= 6:
                rows_y.append(y)
            continue
        if y - rows_y[-1] <= ROW_GAP:
            rows_y.append(y)
        else:
            break
    n = 0
    leaf_sum = {}          # col -> sum of leaf values (for a derived grand total)
    grand_seen = set()     # cols where a canonical grand total was captured
    for y in rows_y:
        label = " ".join(w[4] for w in sorted(grid.words, key=lambda z: z[0])
                         if abs(w[1]-y) <= 9 and w[0] < real_x-6 and not L._looks_numeric(w[4])).strip()
        if not label:
            continue
        cells = grid.assign_cells(y, anchors, tol=6.0, cell_xmin=real_x-20, cell_xmax=last_x)
        has_value = any(L.parse_number(cells.get(c)) is not None for c in ("real", "a", "b", "c"))
        dims = resolver(vocab, label)
        if dims is None:
            # quarantine WITH its values so a dropped data row is never silent (Blocker A)
            if has_value:
                db.quarantine(source_id, spec["ref"], spec["resolver"], label,
                              context=f"page{pg+1} y={y:.0f} values={[cells.get(c) for c in ('real','a','b','c')]}")
            continue
        row_total = bool(dims.pop("is_total", False))
        indicator = dims.pop("indicator", spec["indicator"])
        is_grand = _is_grand_total(spec["ref"], dims, row_total)
        is_leaf = not row_total
        for col in ("real", "a", "b", "c"):
            tok = cells.get(col)
            val = L.parse_number(tok) if tok else None
            if val is None:
                continue
            per = period_for_column_n(col, hdr, ncols)
            if per is None:
                continue
            pt, ps, pe, ry, cm = per
            if is_leaf:
                leaf_sum[col] = leaf_sum.get(col, 0.0) + val
            # A printed grand total of 0 (or blank) while leaves are non-zero is a mis-read;
            # skip storing it and let the derived total stand (avoids a wrong is_total row
            # and a collision with the derived one).
            if is_grand and (not val or abs(val) <= 0.01) and spec.get("derive_total"):
                continue
            if is_grand and val and abs(val) > 0.01:
                grand_seen.add(col)
            db.upsert_observation(
                indicator_id=indicator, value=val, value_raw=tok,
                unit_id=spec["unit"], calorific_basis=spec["basis"],
                basis_confidence="stated" if spec["basis"] != "NA" else "na",
                period_type=pt, period_start=ps, period_end=pe, ref_year=ry,
                ytd_cutoff_month=cm, data_status=status_for(col),
                source_id=source_id, source_page=str(pg+1), source_ref=spec["ref"],
                template_version=template_version, extraction_method="coordinate_map",
                source_cell=f"row={label}|col={col}", is_total=row_total, is_grand=is_grand,
                flow_id=dims.get("flow", spec.get("flow")),
                product_id=dims.get("product", spec.get("product")),
                field_id=dims.get("field"), level_id=dims.get("level"),
                producer_id=dims.get("producer_id"), technology=dims.get("technology"),
                scope=dims.get("scope", spec.get("scope")),
                footnotes=spec.get("footnotes", []))
            n += 1
    # Emit a DERIVED grand total for breakdown tables where the PDF prints none for a
    # column (older C-T20/C-T21 etc.) so every group has exactly one canonical grand
    # total (FIX 2 / C12) and the rollup gates have a reference.
    if spec.get("derive_total"):
        gflow = spec.get("derive_flow", spec.get("flow"))
        for col, s in leaf_sum.items():
            if col in grand_seen:
                continue
            per = period_for_column_n(col, hdr, ncols)
            if per is None:
                continue
            pt, ps, pe, ry, cm = per
            # belt-and-suspenders: skip if a grand total already exists in the DB for this
            # exact series+period (printed total ingested earlier), avoiding a redundant
            # derived row + its collision-quarantine noise.
            seen_db = db.con.execute(
                """SELECT 1 FROM observation WHERE source_id=? AND source_ref=? AND is_total
                   AND period_start=? AND period_end=? AND calorific_basis=?
                   AND COALESCE(flow_id,'')=? AND COALESCE(producer_id,'')=''
                   AND COALESCE(technology,'')='' AND COALESCE(scope,'')=''
                   AND COALESCE(level_id,'')='' LIMIT 1""",
                [source_id, spec["ref"], ps, pe, spec["basis"], gflow or ""]).fetchone()
            if seen_db:
                continue
            db.upsert_observation(
                indicator_id=spec["indicator"], value=round(s, 1), value_raw=None,
                unit_id=spec["unit"], calorific_basis=spec["basis"],
                basis_confidence="stated" if spec["basis"] != "NA" else "na",
                period_type=pt, period_start=ps, period_end=pe, ref_year=ry,
                ytd_cutoff_month=cm, data_status=status_for(col),
                source_id=source_id, source_page=str(pg+1), source_ref=spec["ref"],
                template_version=template_version, extraction_method="coordinate_map",
                is_total=True, is_grand=True, is_derived=True,
                derivation_note=f"{spec['derive_total']} = sum of leaf rows (printed total absent)",
                source_cell=f"row=GRAND_TOTAL(derived)|col={col}",
                flow_id=spec.get("derive_flow", spec.get("flow")),
                product_id=spec.get("product"), footnotes=spec.get("footnotes", []))
            n += 1
    return n


def _is_grand_total(ref, dims, row_total):
    """True iff this row is the table's CANONICAL grand total: an is_total row carrying
    NONE of the distinguishing sub-dimensions (no producer/technology/scope/level/field/
    geography). Resolvers return only the distinguishing keys (flow is defaulted from the
    spec later), so we test on absence-of-sub-dimension rather than a specific flow value
    — this correctly identifies PRODUCTION NATIONALE / DEMANDE / Total PP, while STEG
    (producer), Gasoil (product handled in C-T14 below), pressure rows (level),
    marché-local (scope) and Echanges (flow override) are excluded."""
    if not row_total:
        return False
    if ref == "C-T14":
        return dims.get("product") == "prod.petroleum_products_total"
    # grand total = is_total row with no producer/technology/scope/level/field/geography
    # sub-dimension. A resolver-set flow is allowed ONLY if it is the table's main flow
    # (flow.demand for C-T15/16, flow.primary_production for C-T20, flow.sales for C-T21);
    # supply flows (Echanges/export/import) are alternatives, not the grand total.
    MAIN_FLOW = {"C-T15": "flow.demand", "C-T16": "flow.demand",
                 "C-T20": "flow.primary_production", "C-T21": "flow.sales"}
    if (dims.get("producer_id") or dims.get("technology") or dims.get("scope")
            or dims.get("level") or dims.get("field") or dims.get("geography_scope")):
        return False
    f = dims.get("flow")
    return f is None or f == MAIN_FLOW.get(ref)


def _distinct_blocks(ys, gap=30):
    """Collapse anchor-y hits into distinct table blocks (>gap apart)."""
    out = []
    for y in sorted(ys):
        if not out or y - out[-1] > gap:
            out.append(y)
    return out


def load(db, vocab, pdf_path="Conjoncture_énergétique__avril_2026.pdf",
         source_id="conjoncture_2026_04", template_version="conjoncture-tabular-v2024",
         cutoff_month=4):
    d = fitz.open(pdf_path)
    db.con.execute("UPDATE source SET template_version=? WHERE source_id=?",
                   [template_version, source_id])
    total = 0
    hdr = {"real_year": None, "ytd_years": [], "cutoff_month": None}
    # C-T1 primary balance (also fixes hdr for the rest)
    ct1_pg = None
    for p in range(d.page_count):
        t = d[p].get_text()
        if "RESSOURCES" in t and ("DEMANDE" in t or "SOLDE" in t):
            ct1_pg = p; break
    if ct1_pg is not None:
        grid = L.GridPage(d[ct1_pg])
        n, hdr = load_ct1(db, grid, source_id, template_version, page=str(ct1_pg+1))
        total += n
    # remaining breakdown tables (need hdr years/cutoff)
    if hdr.get("real_year"):
        for spec in TABLE_SPECS:
            try:
                total += load_table(db, vocab, d, spec, source_id, template_version, hdr)
            except Exception:
                pass
    d.close()
    return total, hdr


if __name__ == "__main__":
    import duckdb
    con = duckdb.connect("energy.duckdb")
    db = L.DB(con)
    v = L.Vocab(".")
    n, hdr = load(db, v)
    con.commit()
    print(f"Conjoncture C-T1: {n} obs; header={hdr}; stats={db.stats}")
    con.close()
