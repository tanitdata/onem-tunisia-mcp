"""
load_bilan.py — generalized loader for the Bilan National balance matrix.

Approach (per Extraction Methodology): EXPLICIT per-template column x-anchor maps +
nearest-anchor value assignment (assign_cells concatenates split numeric fragments).
This is the alignment-safe primitive from 00_alignment_check.md, now parameterised by
template:
  - bilan-matrix-v2024  (2016-2024): 22 FR product columns, geothermal EN/AR-only
  - bilan-matrix-v2015  (2015):      adds 'GPL primaire' column
  - bilan-matrix-v2010  (2010-2014): 'GPL primaire' + 'Other fuels' cols, 'Récupération' row

Anchors are VALUE-column x-centers (right-aligned numbers), detected once from each
template's reference edition. Each edition is self-checked: the reconstructed
'Production primaire' row must sum to its 'Total tous produits' cell (row identity);
a failure flags the edition low-confidence rather than ingesting a silent misalignment.
Unpivot is BY SEMANTIC COLUMN (OQ-B1). Bilan gas cols -> PCS inferred (OQ-R1).
"""
import fitz
import onem_lib as L

# ---- per-template VALUE-column anchor maps (x-centers of the numeric columns) ----
# v2024: verified in Phase A (these x's reproduce gas=1498, gasoil-gross=1575, 3705 total)
ANCHORS_V2024 = {
    "prod.all_products":185.0, "prod.crude_oil":216.9, "prod.lgn":244.2,
    "prod.petroleum_products_total":272.0, "prod.refinery_gas":293.0, "prod.lpg":316.7,
    "prod.gasoline":340.6, "prod.kerosene":366.2, "prod.jet":390.6, "prod.naphtha":410.7,
    "prod.gasoil":431.5, "prod.fuel_oil":453.5, "prod.petcoke":477.7,
    "prod.other_pet_products":502.6, "prod.natural_gas":530.6, "prod.re_total":569.0,
    "prod.solar_thermal":596.0, "prod.solar_pv":632.1, "prod.geothermal":650.0,
    "prod.biomass":657.2, "prod.wind":681.8, "prod.hydro":707.4, "prod.heat":736.5,
    "prod.electricity":765.4,
}
# v2010: GPL primaire + Other fuels present; derived from the 2010 header value-line.
ANCHORS_V2010 = {
    "prod.all_products":185.5, "prod.crude_oil":217.1, "prod.lpg_primaire":244.0,
    "prod.petroleum_products_total":275.0, "prod.refinery_gas":301.3, "prod.lpg":312.9,
    "prod.gasoline":341.3, "prod.kerosene":367.4, "prod.jet":389.7, "prod.naphtha":410.4,
    "prod.gasoil":432.4, "prod.fuel_oil":452.4, "prod.petcoke":474.6,
    "prod.other_pet_products":501.9, "prod.natural_gas":523.9, "prod.re_total":553.9,
    "prod.solar_thermal":583.6, "prod.solar_pv":609.9, "prod.geothermal":634.0,
    "prod.biomass":658.1, "prod.wind":686.1, "prod.hydro":717.6, "prod.heat":757.4,
    "prod.electricity":786.2,
}
# v2015: 5-page trilingual but still has GPL primaire; anchors derived from 2015 edition.
ANCHORS_V2015 = {
    "prod.all_products":190.0, "prod.crude_oil":216.0, "prod.lpg_primaire":247.5,
    "prod.petroleum_products_total":279.1, "prod.refinery_gas":308.6, "prod.lpg":335.3,
    "prod.gasoline":363.8, "prod.kerosene":391.7, "prod.jet":418.1, "prod.naphtha":444.2,
    "prod.gasoil":472.6, "prod.fuel_oil":499.9, "prod.petcoke":527.9,
    "prod.other_pet_products":554.1, "prod.natural_gas":594.8, "prod.re_total":623.2,
    "prod.solar_thermal":649.7, "prod.solar_pv":676.9, "prod.geothermal":705.1,
    "prod.biomass":733.9, "prod.wind":766.6, "prod.hydro":799.5, "prod.heat":820.0,
    "prod.electricity":835.0,
}
TEMPLATE_ANCHORS = {
    "bilan-matrix-v2024": ANCHORS_V2024,
    "bilan-matrix-v2015": ANCHORS_V2015,
    "bilan-matrix-v2010": ANCHORS_V2010,
}

# Distinctive (unique) header words -> product. Used to RE-ANCHOR per edition so the
# loader follows year-to-year x-drift within a template family (verified: gas col x is
# 527 in 2024 but 540 in 2017). Columns without a unique header word (all_products,
# lpg, jet, fuel_oil, re_total, electricity, lgn, geothermal) keep the template anchor
# shifted by the median observed drift.
DISTINCTIVE = {
    "brut":"prod.crude_oil", "liquides":"prod.lgn", "raffinage":"prod.refinery_gas",
    "essences":"prod.gasoline", "lampant":"prod.kerosene", "naphtha":"prod.naphtha",
    "gasoil":"prod.gasoil", "petcoke":"prod.petcoke", "naturel":"prod.natural_gas",
    "thermique":"prod.solar_thermal", "photovoltaique":"prod.solar_pv",
    "biomasse":"prod.biomass", "eolienne":"prod.wind", "hydraulique":"prod.hydro",
    "chaleur":"prod.heat", "electricite":"prod.electricity", "petroliers":"prod.petroleum_products_total",
    "renouvelables":"prod.re_total", "primaire":None,  # 'primaire' is the row label, ignore
}


def edition_anchors(grid, template_version, py):
    """Re-anchor the template's column map to THIS edition by locating distinctive
    header words; shift non-distinctive columns by the median drift. Returns dict.

    OQ-B1: the FR matrix omits the geothermal column (canonical set includes it, but
    FR/most editions drop it because its value is ~0). We unpivot by SEMANTIC column,
    so we only keep a `prod.geothermal` anchor when a geothermal header token actually
    appears on this page; otherwise we DROP it — preventing the spurious geothermal
    anchor (which sits between solar_pv and biomass) from stealing the solar-PV value.
    Geothermal then stays absent (NULL) for FR-layout editions, exactly as ruled."""
    base = dict(TEMPLATE_ANCHORS[template_version])
    band = [w for w in grid.words if py-118 < w[1] < py-1]
    # detect geothermal header presence (EN/AR-only: 'geothermal'/'geothermie'/'جوفية')
    has_geo = any(L.norm_label(w[4]) in ("geothermal", "geothermie") or "جوفية" in w[4]
                  for w in band)
    if not has_geo:
        base.pop("prod.geothermal", None)   # FR layout -> no geothermal column
    found = {}
    for w in band:
        nl = L.norm_label(w[4])
        pid = DISTINCTIVE.get(nl)
        if pid and pid in base and pid not in found:
            found[pid] = (w[0]+w[2])/2
    if not found:
        return base
    drifts = sorted(found[p]-base[p] for p in found if p in base)
    md = drifts[len(drifts)//2] if drifts else 0.0
    return {pid: found.get(pid, x + md) for pid, x in base.items()}

FLOW_ROWS = [
    ("production primaire", "flow.primary_production"),
    ("recuperation", "flow.recovery"),
    ("importation", "flow.import"),
    ("variation des stocks", "flow.stock_change"),
    ("exportations", "flow.export"),
    ("soutes internationales", "flow.bunkers"),
    ("soutess internationales", "flow.bunkers"),
    ("consommation interieure brute", "flow.gross_inland_consumption"),
    ("consommation interieur brute", "flow.gross_inland_consumption"),
    ("entrees en transformation", "flow.transformation_input"),
    ("sortie de transformation", "flow.transformation_output"),
    ("echanges transfer restitutions", "flow.exchanges_transfers"),
    ("consommation de la branche energie", "flow.energy_branch_consumption"),
    ("pertes", "flow.losses"),
    ("disponible pour consom finale", "flow.available_final"),
    ("disponible pour consom", "flow.available_final"),
    ("consommation finale non energetique", "flow.final_non_energy"),
    ("consommation finale energetique", "flow.final_energy"),
    ("ecart statistique", "flow.statistical_difference"),
]
SECTOR_ROWS = [
    ("fabrications metalliques", "sect.ind_metal"),
    ("produits mineraux non metalliques", "sect.ind_nonmetallic"),
    ("alimentation boisson tabac", "sect.ind_food"),
    ("textiles cuir habillement", "sect.ind_textile"),
    ("papier et imprimerie", "sect.ind_paper"),
    ("fabrications mecaniques et electriques", "sect.ind_mechelec"),
    ("autres industries", "sect.ind_other"),
    ("chimie", "sect.ind_chemical"),
    ("extraction", "sect.ind_extraction"),
    ("industrie", "sect.industry"),
    ("ferroviaires", "sect.transport_rail"),
    ("ferorviaires", "sect.transport_rail"),
    ("routes", "sect.transport_road"),
    ("aeriens", "sect.transport_air"),
    ("pipeline", "sect.transport_pipeline"),
    ("transport", "sect.transport"),
    ("foyers domestiques commerce", "sect.residential_commercial"),
    ("foyers domestiques", "sect.residential"),
    ("commerce adm hotels", "sect.commercial"),
    ("agriculture et peche", "sect.agriculture"),
]
GAS_PRODUCTS = {"prod.natural_gas"}
LPG_PRIMAIRE_COL = "prod.lpg_primaire"


def find_matrix_page(d):
    for p in range(d.page_count):
        if "Production primaire" in d[p].get_text():
            return p
    return None


def _row_flow(nl):
    for slabel, sid in SECTOR_ROWS:
        if nl.startswith(slabel):
            return "flow.final_energy", sid
    for flabel, fid in FLOW_ROWS:
        if nl.startswith(flabel):
            return fid, None
    return None, None


def load(db, pdf_path="Bilan_National_de_l_Energie_2024.pdf",
         source_id="bilan_2024", template_version="bilan-matrix-v2024",
         data_status="final", ref_year=2024, version="v2"):
    d = fitz.open(pdf_path)
    pg = find_matrix_page(d)
    if pg is None:
        d.close(); return 0, "no_matrix_page"
    grid = L.GridPage(d[pg])
    pys = [w[1] for w in grid.words if w[4] == "primaire"]
    if not pys:
        d.close(); return 0, "no_primaire_row"
    py = min(pys)
    anchors = edition_anchors(grid, template_version, py)   # per-edition re-anchoring
    db.con.execute("UPDATE source SET version=COALESCE(version,?), template_version=? WHERE source_id=?",
                   [version, template_version, source_id])
    ps, pe = f"{ref_year}-01-01", f"{ref_year}-12-31"
    label_left = min(anchors.values()) - 20

    # First pass: reconstruct the Production primaire row to self-check alignment.
    lines = {}
    for w in grid.words:
        lines.setdefault(round(w[1], 1), []).append(w)
    staged = []
    for y in sorted(lines):
        if y <= py - 2:
            continue
        toks = sorted(lines[y], key=lambda z: z[0])
        label = " ".join(t[4] for t in toks if not _is_numlike(t[4]) and t[0] < label_left)
        nl = L.norm_label(label)
        if not nl:
            continue
        flow_id, sector_id = _row_flow(nl)
        if flow_id is None:
            continue
        cells = grid.assign_cells(y, anchors, tol=5.0)
        staged.append((flow_id, sector_id, cells))

    # self-check: primary production row sum ~= all_products cell
    check = "n/a"
    for flow_id, sector_id, cells in staged:
        if flow_id == "flow.primary_production" and sector_id is None:
            ap = L.parse_number(cells.get("prod.all_products", ""))
            # all_products = sum of TOP-LEVEL carriers only:
            #   crude + lgn + lpg_primaire + PP_total + natural_gas + re_total + heat + elec
            # Exclude all_products itself and the sub-components that roll up into the
            # PP_total / re_total aggregates (pet-product details + RE sub-types).
            SUBCOMPONENTS = {
                "prod.refinery_gas","prod.lpg","prod.gasoline","prod.kerosene","prod.jet",
                "prod.naphtha","prod.gasoil","prod.fuel_oil","prod.petcoke",
                "prod.other_pet_products",
                "prod.solar_thermal","prod.solar_pv","prod.geothermal","prod.biomass",
                "prod.wind","prod.hydro"}
            comps = sum(L.parse_number(v) or 0 for k, v in cells.items()
                        if k not in ({"prod.all_products"} | SUBCOMPONENTS)
                        and L.parse_number(v) is not None)
            if ap:
                check = "PASS" if abs(comps - ap) <= max(8, ap*0.02) else f"FAIL({comps:.0f}vs{ap:.0f})"
            break
    confidence = "normal" if check in ("PASS", "n/a") else "low"

    # OQ-M2: a Bilan cell is a "total" (not a summable leaf) if its PRODUCT is an
    # aggregate column (all_products / petroleum_products_total / re_total) OR its FLOW
    # is an aggregate balance line (gross-inland, transformation in/out, final-energy
    # parent, resources, demand). The 24 product columns and 40 flow rows interleave
    # aggregates with leaves; flagging per cell lets v_series_detail keep only leaves.
    AGG_PRODUCTS = {"prod.all_products", "prod.petroleum_products_total", "prod.re_total"}
    AGG_FLOWS = {"flow.gross_inland_consumption", "flow.transformation_input",
                 "flow.transformation_output", "flow.final_energy", "flow.resources",
                 "flow.demand", "flow.available_final"}
    total = 0
    for flow_id, sector_id, cells in staged:
        for pid, tok in cells.items():
            val = L.parse_number(tok)
            if val is None:
                continue
            prod_id, scope = pid, None
            if pid == LPG_PRIMAIRE_COL:
                prod_id, scope = "prod.lpg", "gpl_primaire"
            row_total = (prod_id in AGG_PRODUCTS) or (flow_id in AGG_FLOWS)
            is_gas = prod_id in GAS_PRODUCTS
            basis = "PCS" if is_gas else "NA"
            bconf = "inferred" if is_gas else "na"
            fns = ["FN-BALANCE-METHOD", "FN-BILAN-VERSION"]
            kw = {}
            if is_gas:
                fns += ["FN-PCI-PCS", "FN-BILAN-GAS-PCS"]
                kw.update(is_escalated=True, escalation_ref="OQ-R1", scope="primary_broad")
            elif scope:
                kw["scope"] = scope
            db.upsert_observation(
                indicator_id="energy_balance", value=val, value_raw=tok, unit_id="ktep",
                calorific_basis=basis, basis_confidence=bconf,
                period_type="annual", period_start=ps, period_end=pe,
                ref_year=ref_year, data_status=data_status,
                source_id=source_id, source_page=str(pg+1), source_ref="B-T1",
                template_version=template_version, extraction_method="coordinate_map",
                extraction_confidence=confidence,
                source_cell=f"row={flow_id}{('/'+sector_id) if sector_id else ''}|col={prod_id}",
                is_total=row_total,
                flow_id=flow_id, product_id=prod_id, sector_id=sector_id,
                footnotes=fns, **kw)
            total += 1
    d.close()
    return total, check


def _is_numlike(t):
    s = t.replace(" ", "").replace(",", "").replace(".", "").lstrip("-")
    return s.isdigit()


if __name__ == "__main__":
    import duckdb
    con = duckdb.connect("energy.duckdb")
    db = L.DB(con)
    n, chk = load(db)
    con.commit()
    print(f"Bilan 2024: {n} cells; self-check={chk}; stats={db.stats}")
    con.close()
