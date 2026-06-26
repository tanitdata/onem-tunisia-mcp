"""
load_memento.py — reference loader for the Chiffres clés / Memento (annual).

Memento tables are vertical "year-pair" tables: a left-hand row label + columns
'2023','2024','Var (%)'. Each table is described by a SPEC (page, y-range,
indicator, dimension type + how to map the row label, unit, basis). Period columns
are auto-detected by locating the '2023'/'2024' header tokens inside the y-range
(so the loader tolerates per-page x-drift — important for older editions in Phase C).

Row labels are mapped to controlled-vocabulary IDs via Vocab; unmapped labels are
quarantined (never guessed). Var% columns are NOT ingested (derived; Trap 9).
"""
import fitz
import onem_lib as L

# A spec maps a table REGION to (indicator, dim, unit, basis, ...) with an explicit
# column x-range map (anchors) + value/label x-bounds, per the extraction methodology
# (column x-ranges are defined once per template, applied by code). Anchors are the
# x0 of each year header; cell_xmin/xmax fence off side-by-side neighbour tables.
# dim in {field, product, flow_gas, level, flow_or_level, elec_src}
SPECS = [
    # --- crude production by field (M-T2, kt) page 4, LEFT table (right is M-T3 b/j) ---
    dict(page=3, y0=100, y1=300, indicator="crude_production", dim="field",
         unit="kt", basis="NA", product="prod.crude_oil", flow="flow.primary_production",
         ref="M-T2", scope="incl_gpl_condensat", footnotes=["FN-OIL-GPLCOND-INCL"],
         anchors={"2023":168.5,"2024":222.3}, label_xmax=160, cell_xmin=150, cell_xmax=270),
    # --- gas production by field, PCI (M-T6) page 6 TOP table (label "...COMMERCIAL",
    #     header says ktep-pci; Nawara 2023=518). Verified by PCI<PCS validation check. ---
    dict(page=5, y0=85, y1=190, indicator="gas_production", dim="field",
         unit="ktep-pci", basis="PCI", product="prod.natural_gas", flow="flow.primary_production",
         ref="M-T6", scope="commercial_dry", footnotes=["FN-PCI-PCS","FN-GAS-COMMERCIAL","FN-GAZSUD-MEMBERS-M"],
         anchors={"2023":210.0,"2024":255.0}, label_xmax=200, cell_xmin=200, cell_xmax=290),
    # --- gas production by field, PCS (M-T5) page 6 BOTTOM table (header ktep-pcs;
    #     Nawara 2023=576). ---
    dict(page=5, y0=262, y1=366, indicator="gas_production", dim="field",
         unit="ktep-pcs", basis="PCS", product="prod.natural_gas", flow="flow.primary_production",
         ref="M-T5", scope="commercial_dry", footnotes=["FN-PCI-PCS","FN-GAS-COMMERCIAL","FN-GAZSUD-MEMBERS-M"],
         anchors={"2023":210.0,"2024":255.0}, label_xmax=200, cell_xmin=200, cell_xmax=290),
    # --- gas supply PCI (M-T7) page 7 top ---
    dict(page=6, y0=78, y1=120, indicator="gas_resources", dim="flow_gas",
         unit="ktep-pci", basis="PCI", product="prod.natural_gas",
         ref="M-T7", footnotes=["FN-PCI-PCS"],
         anchors={"2023":171.5,"2024":231.9}, label_xmax=160, cell_xmin=160, cell_xmax=275),
    # --- gas supply PCS (M-T8) page 7 bottom ---
    dict(page=6, y0=208, y1=250, indicator="gas_resources", dim="flow_gas",
         unit="ktep-pcs", basis="PCS", product="prod.natural_gas",
         ref="M-T8", footnotes=["FN-PCI-PCS"],
         anchors={"2023":171.5,"2024":233.0}, label_xmax=160, cell_xmin=160, cell_xmax=275),
    # --- PP exports (M-T9) page 8 top ---
    dict(page=7, y0=92, y1=210, indicator="pp_export", dim="product",
         unit="ktep", basis="NA", flow="flow.export", ref="M-T9",
         anchors={"2023":172.0,"2024":223.0}, label_xmax=160, cell_xmin=160, cell_xmax=270),
    # --- electricity production by source (M-T12) page 9, RIGHT-top table ---
    dict(page=8, y0=70, y1=190, indicator="electricity_production", dim="elec_src",
         unit="GWh", basis="NA", flow="flow.primary_production", ref="M-T12",
         footnotes=["FN-ELEC-NOAUTOTHERM"],
         anchors={"2023":607.0,"2024":647.0}, label_xmin=455, label_xmax=600,
         cell_xmin=600, cell_xmax=710),
    # --- PP consumption (M-T15) page 11 ---
    dict(page=10, y0=135, y1=285, indicator="pp_consumption", dim="product",
         unit="ktep", basis="NA", flow="flow.consumption", ref="M-T15",
         footnotes=["FN-PP-CONS-AUTOSTIR"],
         anchors={"2023":159.0,"2024":222.0}, label_xmax=158, cell_xmin=155, cell_xmax=270),
    # --- gas demand PCI (M-T16) page 13 top ---
    dict(page=12, y0=78, y1=185, indicator="gas_demand", dim="flow_or_level",
         unit="ktep-pci", basis="PCI", product="prod.natural_gas", ref="M-T16",
         footnotes=["FN-PCI-PCS"],
         anchors={"2023":171.0,"2024":231.0}, label_xmax=165, cell_xmin=160, cell_xmax=275),
    # --- gas demand PCS (M-T17) page 13 bottom ---
    dict(page=12, y0=205, y1=320, indicator="gas_demand", dim="flow_or_level",
         unit="ktep-pcs", basis="PCS", product="prod.natural_gas", ref="M-T17",
         footnotes=["FN-PCI-PCS"],
         anchors={"2023":171.0,"2024":231.0}, label_xmax=165, cell_xmin=160, cell_xmax=275),
    # --- electricity sales by voltage (M-T18) page 14 top ---
    dict(page=13, y0=100, y1=175, indicator="electricity_sales", dim="level",
         unit="GWh", basis="NA", flow="flow.sales", ref="M-T18",
         anchors={"2023":182.5,"2024":234.8}, label_xmax=175, cell_xmin=170, cell_xmax=290),
]

# electricity production source rows -> (producer/technology mapping)
ELEC_SRC = {
    "FUEL + GASOIL": dict(technology="thermal_fuel", producer_id="prod.steg"),
    "GAZ NATUREL":   dict(technology="thermal_gas", producer_id="prod.steg"),
    "HYDRAULIQUE":   dict(technology="hydro", producer_id="prod.steg"),
    "EOLIENNE":      dict(technology="wind", producer_id="prod.steg"),
    "Solaire PV":    dict(technology="pv", producer_id="prod.steg"),
    "STEG":          dict(producer_id="prod.steg", is_total=True),
    "IPP + autoproducteurs (Solaire)": dict(technology="pv", producer_id="prod.ipp"),
    "TOTAL":         dict(is_total=True),
}
# gas supply flow rows
GAS_FLOW = {
    "Production nationale": "flow.primary_production",
    "Redevance totale": "flow.royalty",
    "Achats": "flow.purchase",
    "Demande totale": "flow.demand",
}


def detect_year_anchors(grid, y0, y1):
    """Find x-centers of the '2023'/'2024' header tokens within [y0,y1]."""
    anchors = {}
    for w in grid.words:
        if y0 <= w[1] <= y1 and w[4] in ("2023", "2024"):
            anchors[w[4]] = (w[0] + w[2]) / 2
    return anchors


def resolve_dim(spec, label, vocab):
    """Return dict of dimension fields for a row label, or None if unmapped."""
    dim = spec["dim"]
    if dim == "field":
        fid = vocab.match("field", label)
        if not fid and L.norm_label(label) in ("total",):
            return {"is_total": True}     # field=NULL aggregate
        return {"field_id": fid} if fid else None
    if dim == "product":
        if L.norm_label(label) in ("total",):
            return {"is_total": True}
        pid = vocab.match("product", label)
        return {"product_id": pid} if pid else None
    if dim == "flow_gas":
        fl = GAS_FLOW.get(label.strip())
        return {"flow_id": fl} if fl else None
    if dim == "level":
        nl = L.norm_label(label)
        if nl in ("total","total ventes","total ventes locales"):
            return {"is_total": True}
        if nl.startswith("ventes externes"):     # Libya exports only (OQ-R6): 107.9.
            # NOT incl_exports — it's the exports sliver alone; the incl_exports TOTAL
            # (17197 = local 17090 + 107.9) is emitted as a derived total below.
            return {"flow_id": "flow.sales", "geography_scope": "exports_only", "is_total": True}
        if nl in ("moyen tension","moyenne tension"):
            return {"level_id": "lvl.mt", "flow_id": "flow.sales", "geography_scope": "local"}
        if nl in ("basse tension",):
            return {"level_id": "lvl.bt", "flow_id": "flow.sales", "geography_scope": "local"}
        if nl in ("haute tension",):
            return {"level_id": "lvl.ht", "flow_id": "flow.sales", "geography_scope": "local"}
        lid = vocab.match("level", label)
        return {"level_id": lid, "flow_id": "flow.sales", "geography_scope": "local"} if lid else None
    if dim == "flow_or_level":
        nl = L.norm_label(label)
        # Gas demand has TWO partitions of the SAME total (OQ-M2): a USAGE split
        # (power_generation + non_power) and a PRESSURE split (HP + MBP). To avoid the
        # double-count, exactly ONE partition is the canonical leaf set: the USAGE split.
        # The pressure rows are an ALTERNATIVE breakdown -> is_total=TRUE (excluded from
        # the default-sum partition; still queryable). Consistent with load_conjoncture.
        if nl.startswith("production d electricite") or nl.startswith("production delectricite"):
            return {"flow_id": "flow.demand", "scope": "power_generation"}     # leaf
        if nl.startswith("hors prod"):
            return {"flow_id": "flow.demand", "scope": "non_power"}            # leaf
        if nl in ("haute pression",):
            return {"level_id": "lvl.hp", "flow_id": "flow.demand", "is_total": True}   # alt
        if nl in ("moy basse pression","moyenne basse pression","moy&basse pression",
                  "moyenne et basse pression"):
            return {"level_id": "lvl.mbp", "flow_id": "flow.demand", "is_total": True}  # alt
        lid = vocab.match("level", label)
        if lid:
            return {"level_id": lid, "flow_id": "flow.demand", "is_total": True}
        fl = GAS_FLOW.get(label.strip())
        if fl:
            return {"flow_id": fl}
        if nl.startswith("demande"):
            return {"flow_id": "flow.demand", "is_total": True}
        return None
    if dim == "elec_src":
        m = ELEC_SRC.get(label.strip())
        return dict(m) if m else None
    return None


def load(db, vocab, pdf_path="Chiffres_clés_énergie_2024.pdf",
         source_id="memento_2024", template_version="memento-onem-v2024", ref_year=2024):
    """Load an ONEM Memento. The reference geometry (SPECS anchors/y-bands) is the 2024
    unrotated layout. Editions sharing that geometry (2022-2024) load directly; the year
    columns are the (ref_year-1, ref_year) pair. Rotated editions (2018-2021) have a
    divergent/transposed layout and are skipped here (flagged as a coverage gap in
    coverage_gaps.md) — they need per-edition calibration."""
    d = fitz.open(pdf_path)
    if d[3].rotation != 0:
        d.close()
        return -1   # signal: rotated layout, deferred
    db.con.execute("UPDATE source SET template_version=? WHERE source_id=?",
                   [template_version, source_id])
    py, cy = ref_year-1, ref_year
    # The current (latest) year column is provisional pre-audit (OQ-P2); prior is final.
    PERIODS = {
        str(py): ("annual", f"{py}-01-01", f"{py}-12-31", py, "final"),
        str(cy): ("annual", f"{cy}-01-01", f"{cy}-12-31", cy, "provisional"),
    }
    total = 0
    for spec in SPECS:
        pg = d[spec["page"]]
        grid = L.GridPage(pg)
        # Detect this edition's year-column value anchors: find the two year-header
        # tokens (py, cy) sitting just above the table, within the spec's x window.
        cxmin = spec.get("cell_xmin", 0.0); cxmax = spec.get("cell_xmax", 1e9)
        det = {}
        for w in grid.words:
            if spec["y0"]-30 <= w[1] <= spec["y0"]+8 and cxmin-15 <= (w[0]+w[2])/2 <= cxmax+15:
                if w[4] == str(py): det["2023"] = (w[0]+w[2])/2
                elif w[4] == str(cy): det["2024"] = (w[0]+w[2])/2
        # map detected positions onto the spec's logical column keys
        anchors = dict(spec["anchors"])
        if "2023" in det: anchors["2023"] = det["2023"]
        if "2024" in det: anchors["2024"] = det["2024"]
        rows = grid.read_rows(
            spec["y0"], spec["y1"], anchors,
            label_xmin=spec.get("label_xmin", 0.0), label_xmax=spec.get("label_xmax", 160.0),
            cell_xmin=spec.get("cell_xmin", 0.0), cell_xmax=spec.get("cell_xmax", 1e9))
        # accumulate leaves per period so we can emit DERIVED totals where the PDF prints
        # none (FIX 1: M-T16/17 gas demand; FIX 3: M-T18 elec sales local + incl-exports).
        usage_sum = {}     # gas demand: power_generation + non_power
        local_sum = {}     # elec sales: HT + MT + BT (local)
        exports_val = {}   # elec sales: Ventes externes sliver
        for label, cells, y in rows:
            dimfields = resolve_dim(spec, label, vocab)
            if dimfields is None:
                db.quarantine(source_id, spec["ref"], spec["dim"], label,
                              context=f"page{spec['page']+1}")
                continue
            row_total = bool(dimfields.pop("is_total", False))
            row_scope = dimfields.get("scope")
            row_geo = dimfields.get("geography_scope")
            row_level = dimfields.get("level_id")
            for col, tok in cells.items():
                if col not in PERIODS:
                    continue
                val = L.parse_number(tok)
                if val is None:
                    continue
                if spec["indicator"] == "gas_demand" and row_scope in ("power_generation", "non_power"):
                    usage_sum[col] = usage_sum.get(col, 0.0) + val
                if spec["indicator"] == "electricity_sales":
                    if row_geo == "local" and row_level:
                        local_sum[col] = local_sum.get(col, 0.0) + val
                    elif row_geo == "exports_only":
                        exports_val[col] = val
                pt, ps, pe, ry, status = PERIODS[col]
                kw = dict(
                    indicator_id=spec["indicator"], value=val, value_raw=tok,
                    unit_id=spec["unit"], calorific_basis=spec["basis"],
                    basis_confidence="stated" if spec["basis"] != "NA" else "na",
                    period_type=pt, period_start=ps, period_end=pe, ref_year=ry,
                    data_status=status, source_id=source_id, source_page=str(spec["page"]+1),
                    source_ref=spec["ref"], template_version=template_version,
                    product_id=spec.get("product"), flow_id=spec.get("flow"),
                    scope=spec.get("scope"), footnotes=spec.get("footnotes", []),
                    extraction_method="coordinate_map", extraction_confidence="normal",
                    source_cell=f"row={label}|col={col}", is_total=row_total,
                )
                kw.update(dimfields)
                db.upsert_observation(**kw)
                total += 1
        # emit the derived gas-demand grand total (usage partition) so the group has a
        # single declared total and the rollup gates have something to check against.
        if spec["indicator"] == "gas_demand":
            for col, s in usage_sum.items():
                pt, ps, pe, ry, status = PERIODS[col]
                db.upsert_observation(
                    indicator_id="gas_demand", value=round(s, 1), value_raw=None,
                    unit_id=spec["unit"], calorific_basis=spec["basis"],
                    basis_confidence="stated" if spec["basis"] != "NA" else "na",
                    period_type=pt, period_start=ps, period_end=pe, ref_year=ry,
                    data_status=status, source_id=source_id, source_page=str(spec["page"]+1),
                    source_ref=spec["ref"], template_version=template_version,
                    product_id=spec.get("product"), flow_id="flow.demand",
                    is_total=True, is_grand=True, is_derived=True,
                    derivation_note="demande totale = production électrique + hors prod (usage partition)",
                    extraction_method="coordinate_map", extraction_confidence="normal",
                    source_cell=f"row=DEMANDE(derived)|col={col}",
                    footnotes=spec.get("footnotes", []))
                total += 1
        # FIX 3 (OQ-R6): elec sales — emit derived LOCAL total (HT+MT+BT) and the
        # INCL-EXPORTS total (local + Ventes externes), both is_total, distinct geography.
        if spec["indicator"] == "electricity_sales":
            for col, loc in local_sum.items():
                pt, ps, pe, ry, status = PERIODS[col]
                base = dict(indicator_id="electricity_sales", unit_id=spec["unit"],
                    calorific_basis="NA", basis_confidence="na", period_type=pt,
                    period_start=ps, period_end=pe, ref_year=ry, data_status=status,
                    source_id=source_id, source_page=str(spec["page"]+1), source_ref=spec["ref"],
                    template_version=template_version, flow_id="flow.sales", is_total=True,
                    is_grand=True, is_derived=True, extraction_method="coordinate_map",
                    extraction_confidence="normal", footnotes=spec.get("footnotes", []))
                db.upsert_observation(value=round(loc,1), value_raw=None,
                    geography_scope="local",
                    derivation_note="ventes locales totales = HT+MT+BT (OQ-R6)",
                    source_cell=f"row=Ventes locales(derived)|col={col}", **base)
                total += 1
                if col in exports_val:
                    db.upsert_observation(value=round(loc+exports_val[col],1), value_raw=None,
                        geography_scope="incl_exports",
                        derivation_note="ventes totales incl. exports = locales + Ventes externes (OQ-R6)",
                        source_cell=f"row=Ventes incl. exports(derived)|col={col}", **base)
                    total += 1
    d.close()
    return total


if __name__ == "__main__":
    import duckdb
    con = duckdb.connect("energy.duckdb")
    db = L.DB(con)
    v = L.Vocab(".")
    n = load(db, v)
    con.commit()
    print(f"Memento: {n} obs; stats={db.stats}")
    con.close()
