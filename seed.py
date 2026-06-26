"""
seed.py — populate the DuckDB store's reference/dimension tables from the
vocab_*.csv files, 03_units_and_conversions.csv, 04_footnotes.md and the manifest.

Idempotent: drops + recreates from schema.sql, then loads dimensions.
Run before the loaders.
"""
import csv, re, sys, os
import duckdb
import onem_lib as L

DB_PATH = "energy.duckdb"

# product / flow hierarchy flags (OQ-M2). is_total marks aggregate rows that must
# NOT be summed with their components.
PRODUCT_TOTALS = {"prod.petroleum_products_total","prod.re_total"}
# 'Total tous produits' is a matrix column too -> add it as a product aggregate.
PRODUCT_PARENT = {
    "prod.gasoline_ssp":"prod.gasoline", "prod.gasoline_super":"prod.gasoline",
    "prod.gasoline_premium":"prod.gasoline",
    "prod.gasoil_ordinaire":"prod.gasoil","prod.gasoil_ss":"prod.gasoil","prod.gasoil_premium":"prod.gasoil",
    "prod.fuel_hts":"prod.fuel_oil","prod.fuel_bts":"prod.fuel_oil",
    "prod.solar_thermal":"prod.re_total","prod.solar_pv":"prod.re_total","prod.geothermal":"prod.re_total",
    "prod.biomass":"prod.re_total","prod.wind":"prod.re_total","prod.hydro":"prod.re_total",
}
FLOW_TOTALS = {"flow.transformation_input","flow.transformation_output","flow.final_energy",
               "flow.gross_inland_consumption","flow.resources","flow.demand"}

def seed_sources(con):
    """Register one source row per in-scope PDF (deterministic source_id).
    AR editions are flagged is_canonical_lang=FALSE (translation, not a 2nd observation)."""
    man=L.load_manifest()
    seen=set()
    cadence_by_type={"bilan":"annual","memento":"annual","conjoncture":"monthly",
                     "covid_bulletin":"monthly","rapport":"annual"}
    for r in man:
        if r["report_family"]=="Other":
            continue
        sid, typ = L.derive_source_id(r)
        if sid in seen:
            sid = sid + "_" + r["file_id"][:6]
        seen.add(sid)
        lang=r["language"]
        is_canon = lang in ("fr","multi","en")
        cutoff = L.parse_cutoff_from_period(r["period"])
        con.execute("""INSERT INTO source
            (source_id,report_title,report_type,language,publication_date,version,
             period_covered,cadence,cutoff_month,file_id,sha256,local_path,
             is_canonical_lang,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [sid, r["local_path"].split("/")[-1], typ, lang, None,
             r.get("version") or None, r.get("period") or None,
             cadence_by_type.get(typ,"annual"), cutoff, r["file_id"], r.get("sha256"),
             r["local_path"], is_canon, r.get("notes")])

def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = duckdb.connect(DB_PATH)
    con.execute(open("schema.sql",encoding="utf-8").read())
    v = L.Vocab(".")

    seed_sources(con)   # must precede field_membership (FK to source)
    # flow.recovery exists only in early Bilan layouts (Récupération row); add it.
    con.execute("INSERT OR IGNORE INTO flow (flow_id,label_fr,label_en,aggregation_level) VALUES ('flow.recovery','Récupération','Recovery',0)")

    # ---- flow ----
    for r in v.rows["flow"]:
        fid=r["flow_id"]
        con.execute("INSERT INTO flow (flow_id,label_fr,label_en,label_ar,is_total,aggregation_level,aliases,definition) VALUES (?,?,?,?,?,?,?,?)",
            [fid, r["label_fr"], r["label_en"], r.get("label_ar"),
             fid in FLOW_TOTALS, 1 if fid in FLOW_TOTALS else 0,
             r.get("aliases"), r.get("notes")])

    # ---- product ----  (add the synthetic 'Total tous produits' aggregate)
    con.execute("INSERT INTO product (product_id,label_fr,label_en,category,is_total,aggregation_level,aliases) VALUES ('prod.all_products','Total tous produits','All products total','aggregate',TRUE,2,'Total tous produits|All products')")
    for r in v.rows["product"]:
        pid=r["product_id"]
        is_tot = pid in PRODUCT_TOTALS
        con.execute("INSERT INTO product (product_id,label_fr,label_en,label_ar,category,parent_product_id,is_total,aggregation_level,aliases,definition) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [pid, r["label_fr"], r["label_en"], r.get("label_ar"), r.get("category"),
             PRODUCT_PARENT.get(pid), is_tot, 1 if is_tot else 0,
             r.get("aliases"), r.get("notes")])

    # ---- sector ----
    for r in v.rows["sector"]:
        sid=r["sector_id"]
        is_tot = sid in ("sect.industry","sect.transport","sect.residential_commercial")
        con.execute("INSERT INTO sector (sector_id,label_fr,label_en,parent_sector_id,is_total,aliases,definition) VALUES (?,?,?,?,?,?,?)",
            [sid, r["label_fr"], r["label_en"], r.get("parent") or None, is_tot,
             r.get("aliases"), r.get("notes")])

    # sector crosswalk (OQ-S1): Conjoncture HT&MT pie taxonomy -> canonical sectors
    cw = [
        ("conjoncture_htmt_pie","Industries","sect.industry"),
        ("conjoncture_htmt_pie","Agriculture","sect.agriculture"),
        ("conjoncture_htmt_pie","Pompages & ser.","sect.agriculture"),
        ("conjoncture_htmt_pie","Pompages","sect.agriculture"),
        ("conjoncture_htmt_pie","Sanitaires","sect.sanitary"),
        ("conjoncture_htmt_pie","Transport","sect.transport"),
        ("conjoncture_htmt_pie","Tourisme","sect.commercial"),
        ("conjoncture_htmt_pie","Services","sect.commercial"),
        ("bilan_final","Industrie","sect.industry"),
        ("bilan_final","Transport","sect.transport"),
        ("bilan_final","Foyers domestiques, commerce, adm, etc.","sect.residential_commercial"),
        ("bilan_final","Agriculture et pêche","sect.agriculture"),
    ]
    for tax,lab,sid in cw:
        con.execute("INSERT INTO sector_crosswalk VALUES (?,?,?,?)",[tax,lab,sid,None])

    # ---- region ----
    for r in v.rows["region"]:
        con.execute("INSERT INTO region (region_id,label,region_type,aliases,composition) VALUES (?,?,?,?,?)",
            [r["region_id"], r["label"], r["region_type"], r.get("aliases"),
             r.get("composition_or_notes")])

    # ---- field ----
    AGG_FIELDS={"field.gaz_com_sud","field.autres","field.chalbia_benefsej","field.franig_baguel_trafa"}
    for r in v.rows["field"]:
        fid=r["field_id"]
        con.execute("INSERT INTO field (field_id,label,produces,is_aggregate,aliases,notes) VALUES (?,?,?,?,?,?)",
            [fid, r["label"], r["produces"], fid in AGG_FIELDS, r.get("aliases"), r.get("notes")])

    # field membership (OQ-F1): Gaz Com Sud differs Memento vs Conjoncture
    GCS="field.gaz_com_sud"
    mem_members=["field.el_borma","field.ouedzar","field.adam","field.djebel_grouz","field.cherouq",
                 "field.durra","field.anaguid_est","field.bochra","field.abir"]  # + SITEP/EB407 (no field id)
    con_members=["field.el_borma","field.ouedzar","field.djebel_grouz","field.adam","field.cherouq",
                 "field.durra","field.anaguid_est","field.bochra","field.abir"]  # adds ChouchEss (no id), drops SITEP/EB407
    for m in mem_members:
        con.execute("INSERT OR IGNORE INTO field_membership VALUES (?,?,?)",[GCS,m,"memento_2024"])
    for m in con_members:
        con.execute("INSERT OR IGNORE INTO field_membership VALUES (?,?,?)",[GCS,m,"conjoncture_2026_04"])

    # ---- level ----
    for r in v.rows["level"]:
        con.execute("INSERT INTO level (level_id,label_fr,label_en,domain,aliases) VALUES (?,?,?,?,?)",
            [r["level_id"], r["label_fr"], r["label_en"], r["domain"], r.get("aliases")])

    # ---- producer (OQ-D1) ----
    producers=[("prod.steg","STEG","STEG"),("prod.ipp","IPP","IPP|Producteurs indépendants"),
        ("prod.autoproducteurs","Autoproducteurs","Autoproduction|autoproducteurs"),
        ("prod.stir","STIR","STIR"),("prod.etap","ETAP","ETAP"),
        ("prod.sonatrach","Sonatrach (Algérie)","Sonatrach|Sonelgaz"),
        ("prod.gecol","Gecol (Libye)","Gecol"),("prod.partenaires","Partenaires","Partenaires"),
        ("prod.steg_thermal_main","STEG centrales activité principale","Centrales thermiques: activité principale"),
        ("prod.tiers","Tiers","Achat tiers|tiers")]
    for pid,lab,al in producers:
        con.execute("INSERT INTO producer VALUES (?,?,?,?)",[pid,lab,al,None])

    # ---- redevance toggle enum (OQ-M4) ----
    con.execute("INSERT INTO redevance_toggle VALUES ('incl','Avec comptabilisation de la redevance',TRUE,'redevance counted as national resource')")
    con.execute("INSERT INTO redevance_toggle VALUES ('excl','Sans comptabilisation de la redevance',FALSE,'redevance NOT a national resource')")

    # ---- units + conversions (03) ----
    with open("03_units_and_conversions.csv",encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["record_type"]=="unit":
                con.execute("INSERT OR IGNORE INTO unit (unit_id,label_fr,quantity_kind,notes) VALUES (?,?,?,?)",
                    [r["unit_code"], r["unit_label_fr"], r["quantity_kind"] or "ratio", r.get("note")])
            elif r["record_type"]=="conversion":
                con.execute("INSERT OR IGNORE INTO conversion_factor (from_unit,to_unit,factor,calorific_basis,scope,source_id,note) VALUES (?,?,?,?,?,?,?)",
                    [r["from_unit"], r["to_unit"], float(r["factor"]),
                     r["calorific_basis"] or "NA", r["scope"] or None, None, r.get("note")])
    # ensure 'ktep' plain unit + percent present
    for uid,lab,qk in [("ktep","Mille tep","energy"),("%","Pourcentage","ratio"),
                       ("kt","Kilo tonne","mass"),("GWh","Giga Watt heure","energy_elec"),
                       ("MW","Méga Watt","power")]:
        con.execute("INSERT OR IGNORE INTO unit (unit_id,label_fr,quantity_kind) VALUES (?,?,?)",[uid,lab,qk])

    # ---- indicators (base metrics) ----
    indicators=[
      ("energy_balance","Bilan énergétique (matrice)","balance","ktep","NA","flow,product,sector,producer"),
      ("crude_production","Production de pétrole brut","production","kt","NA","field,region,flow,scope"),
      ("gas_production","Production de gaz naturel","production","ktep","PCI","field,flow,scope"),
      ("gas_resources","Ressources en gaz naturel","balance","ktep","PCI","field,flow"),
      ("gas_demand","Demande de gaz naturel","consumption","ktep","PCI","flow,level"),
      ("pp_production","Production de produits pétroliers","production","ktep","NA","product"),
      ("pp_import","Importation de produits pétroliers","trade","ktep","NA","product"),
      ("pp_export","Exportation de produits pétroliers","trade","ktep","NA","product"),
      ("pp_consumption","Consommation de produits pétroliers","consumption","ktep","NA","product"),
      ("electricity_production","Production d'électricité","production","GWh","NA","producer,technology,flow"),
      ("electricity_sales","Ventes d'électricité","consumption","GWh","NA","level,region,geography_scope"),
      ("electricity_supply","Bilan électrique (supply)","balance","GWh","NA","flow,producer"),
      ("gas_sales","Ventes de gaz naturel","consumption","ktep","PCI","level,region"),
      ("redevance","Redevance / Forfait fiscal","balance","ktep","PCI","flow"),
      ("gas_purchase","Achats de gaz","trade","ktep","PCI","flow"),
      ("primary_balance","Bilan d'énergie primaire","balance","ktep","PCI","flow,product"),
      ("solde","Solde énergétique","balance","ktep","PCI","flow"),
      ("brent_price","Prix du baril Brent","price","$/baril","NA",""),
      ("fx_rate","Taux de change DT/$","price","DT/$","NA",""),
      ("gas_import_price","Prix import gaz algérien","price","DT/tep-pcs","PCS",""),
      ("gas_price","Prix gaz (vente/revient)","price","DT/tep-pcs","PCS",""),
      ("electricity_price","Prix électricité (vente/revient)","price","millimes/kWh","NA",""),
      ("pp_price","Prix produits pétroliers","price","millimes/litre","NA","product"),
      ("crude_price","Prix pétrole brut import/export","price","$/bbl","NA","flow"),
      ("refining_kpi","Indicateurs de raffinage","production","ktep","NA","product"),
      ("exploration_kpi","Exploration & développement","exploration","count","NA",""),
      ("peak_power","Pointe électrique","capacity","MW","NA",""),
      ("specific_consumption","Consommation spécifique","intensity","tep-pcs/GWh","PCS","producer"),
      ("re_capacity","Capacité énergies renouvelables","capacity","MW","NA","technology,regime"),
      ("trade_value","Valeur des échanges énergétiques","trade","MDT","NA","flow,product"),
      ("trade_quantity","Quantité des échanges énergétiques","trade","kt","NA","flow,product"),
    ]
    # Real one-sentence definitions (FIX 5): what the metric measures, its scope, and the
    # key "do not sum/equate" caveats — so a blind LLM never conflates incomparable series.
    DEFS = {
      "energy_balance":"Full national energy balance matrix: each cell is one flow line (row) × product/carrier (column) in ktep for a given year; aggregate rows/columns (totals) carry is_total and must not be summed with their components.",
      "crude_production":"Crude oil production. Scope matters: 'excl_gpl_condensat' = pétrole brut only (the canonical crude, barils/jour basis); 'incl_gpl_condensat' = crude + primary LPG + Gabès condensate (a larger aggregate). Never equate the two scopes.",
      "gas_production":"Natural-gas commercial production. Scope 'commercial_dry' = accounted dry commercial gas (Memento); 'primary_broad' = the Bilan's broader primary gas. Reported in BOTH PCI and PCS (1 PCI = 0.9 PCS); never blend bases or scopes.",
      "gas_resources":"Gas resources = production nationale + redevance (forfait fiscal) + achats; the ressources grand total ('PRODUCTION NATIONALE +F.Fiscal') is distinct from production-only. PCI and PCS variants kept separate.",
      "gas_demand":"Natural-gas demand. Two alternative partitions of the same total: USAGE (production électrique + hors-prod-électrique = canonical leaves) and PRESSURE (HP + MBP, alternative breakdown, flagged is_total). PCI and PCS kept separate; never sum both partitions.",
      "pp_production":"Petroleum-products production (STIR refinery), by product, ktep.",
      "pp_import":"Petroleum-products imports, by product, ktep.",
      "pp_export":"Petroleum-products exports, by product, ktep.",
      "pp_consumption":"Petroleum-products final consumption, by product, ktep. Mid-level products (GPL, Essences, Gasoil, Fuel, …) are the canonical leaves summing to the Total; their sub-variants (Essence Sans Plomb/Super/premium, Gasoil ordinaire/SS/premium) are a finer breakdown flagged is_total.",
      "electricity_production":"Electricity production, GWh. PRODUCTION NATIONALE = STEG (sum of its carrier rows) + IPP + autoproducteurs + achat tiers; STEG and supply rows (échanges/achats) are subtotals/alternatives flagged is_total.",
      "electricity_sales":"Electricity sales, GWh. geography_scope: 'local' = HT+MT+BT domestic (≈17090 in 2024); 'incl_exports' = local + Ventes externes/Libya (≈17197); 'exports_only' = the Ventes externes sliver. Never equate local with incl_exports (OQ-R6).",
      "electricity_supply":"Electricity supply balance (production + exchanges + purchases − sales), GWh, by flow/producer.",
      "gas_sales":"Natural-gas sales (STEG distribution) by pressure level and region, PCI.",
      "redevance":"Algerian gas transit royalty (redevance totale / forfait fiscal), the in-kind gas Tunisia receives; PCI and PCS variants. Subject to the redevance toggle in solde/independence calcs.",
      "gas_purchase":"Gas purchased from Algeria (Sonatrach/Sonelgaz), distinct from product imports; PCI and PCS.",
      "primary_balance":"Primary energy balance (ressources/demande/solde), classical-balance boundary (excludes biomass-energy, field self-consumption, transmed compression). Mixed period types per column (annual Réalisé + YTD à-fin-mois).",
      "solde":"Energy balance deficit (solde). Exists in TWO versions via the redevance toggle: 'incl' counts the royalty as a national resource (smaller deficit), 'excl' does not. Never average the two.",
      "brent_price":"Brent crude price, annual or monthly average, $/baril.",
      "fx_rate":"Exchange rate, dinars per US dollar (DT/$); the source 'taux $/DT' label is an error — values are DT/$ (OQ-U2).",
      "gas_import_price":"Algerian gas import price, DT per tep on PCS basis (DT/tep-pcs).",
      "gas_price":"Natural-gas price (vente / coût de revient), DT/tep-pcs.",
      "electricity_price":"Electricity price (vente / coût de revient), millimes/kWh.",
      "pp_price":"Petroleum-product retail/administered prices, by product, millimes/litre (or DT/t, millimes/kg).",
      "crude_price":"Crude oil import/export price, quantity-weighted, $/bbl.",
      "refining_kpi":"STIR refining indicators (production, coverage rate, days of operation).",
      "exploration_kpi":"Exploration & development KPIs (permits, wells, discoveries), counts.",
      "peak_power":"Electricity peak power demand (pointe), MW.",
      "specific_consumption":"Specific consumption of power generation, tep-pcs per GWh (PCS basis).",
      "re_capacity":"Renewable-energy installed capacity, MW, by technology and regime.",
      "trade_value":"Value of energy trade (import/export), million dinars (MDT), by flow/product.",
      "trade_quantity":"Quantity of energy trade (import/export), kt, by flow/product.",
    }
    for iid,name,cat,unit,basis,dims in indicators:
        con.execute("INSERT INTO indicator (indicator_id,canonical_name,label_fr,definition,category,default_unit_id,default_basis,applicable_dims) VALUES (?,?,?,?,?,?,?,?)",
            [iid,name,name,DEFS.get(iid,name),cat,unit,basis,dims])

    # ---- footnotes (04) ----  parse the FN-* table rows: | FN-id | Type | Text | Applies | Effect |
    # Populate the REAL caveat text (verbatim Text + Effect) and map Type to the schema enum,
    # so observation_footnote links resolve to meaningful sentences, not echoes of the id.
    TYPE_MAP = {
        "toggle":"toggle", "units-basis":"units_basis", "provenance/status":"provenance_status",
        "scope/definition":"scope", "scope":"scope", "definition":"definition",
        "spec-change":"spec_change", "aggregate-membership":"aggregate_membership",
        "provenance":"provenance_status",
    }
    fn_ids=set()
    for line in open("04_footnotes.md",encoding="utf-8"):
        if not line.lstrip().startswith("|"):
            continue
        cols=[c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 5:
            continue
        m=re.match(r"\*{0,2}(FN-[A-Z0-9\-]+)\*{0,2}$", cols[0])
        if not m:
            continue
        fid=m.group(1)
        if fid in fn_ids:
            continue
        fn_ids.add(fid)
        ftype = TYPE_MAP.get(cols[1].lower().strip("*"), "definition")
        verbatim, effect = cols[2], cols[4]
        # full caveat = verbatim source text + its effect-on-meaning, both stripped of MD bold
        text = (verbatim + " — " + effect).replace("**","").strip(" —")
        con.execute("INSERT OR IGNORE INTO footnote (footnote_id,footnote_type,text) VALUES (?,?,?)",
                    [fid, ftype, text])
    # add our two new isolation footnotes
    con.execute("INSERT OR IGNORE INTO footnote VALUES ('FN-BILAN-GAS-PCS','units_basis','Bilan gas matrix columns inferred PCS-basis (gross-inland 4972 matches Memento PCS 4992); blanket PCI note applies to solde, not matrix cells. OQ-R1 ESCALATED.',NULL)")
    con.execute("INSERT OR IGNORE INTO footnote VALUES ('FN-OQ-F2-FIELD','scope','Barka(oil)/Baraka(gas, Maâmoura et Baraka) kept as distinct field records pending ONEM confirmation. OQ-F2 ESCALATED.',NULL)")


    # ---- scope/attribute glossary (FIX 5) ----
    glossary=[
      ("scope","commercial_dry","Accounted dry commercial natural gas (gaz sec) — Memento 'production nationale commerciale'.","primary_broad"),
      ("scope","primary_broad","Bilan 'production primaire' natural gas — broader than commercial-dry (likely incl. field/raw gas). OQ-R1 ESCALATED.","commercial_dry"),
      ("scope","incl_gpl_condensat","Crude aggregate INCLUDING primary LPG + Gabès condensate (Memento M-T2, kt).","excl_gpl_condensat"),
      ("scope","excl_gpl_condensat","Canonical crude: pétrole brut ONLY, sans GPL primaire ni condensat (barils/jour basis).","incl_gpl_condensat"),
      ("scope","gpl_primaire","Primary LPG (GPL champs hors Franig/Baguel/Terfa et Ghrib + GPL usine Gabès).",None),
      ("scope","power_generation","Gas demand for electricity production (canonical usage-partition leaf).",None),
      ("scope","non_power","Gas demand outside power generation (canonical usage-partition leaf; equals HP+MBP pressure split).",None),
      ("scope","re_sourced","Primary electricity sourced from renewables.",None),
      ("scope","market_local","Electricity production destined for the local market.",None),
      ("geography_scope","local","Electricity sales to the domestic market (HT+MT+BT). ≈17090 GWh in 2024.","incl_exports|exports_only"),
      ("geography_scope","incl_exports","Total electricity sales INCLUDING Libya exports = local + Ventes externes. ≈17197 GWh in 2024 (OQ-R6).","local"),
      ("geography_scope","exports_only","The Ventes externes (Libya export) sliver alone. ≈107.9 GWh in 2024.","local|incl_exports"),
      ("calorific_basis","PCI","Lower heating value (pouvoir calorifique inférieur). 1 PCI = 0.9 PCS. NEVER blend with PCS.","PCS"),
      ("calorific_basis","PCS","Higher heating value (pouvoir calorifique supérieur). ktep-pcs = ktep-pci / 0.9. NEVER blend with PCI.","PCI"),
      ("calorific_basis","NA","No calorific basis (mass, volume, electricity, price, count).",None),
      ("redevance_toggle","incl","Solde/independence WITH the Algerian royalty counted as a national resource (smaller deficit).","excl"),
      ("redevance_toggle","excl","Solde/independence WITHOUT the royalty as a national resource (larger deficit). NEVER average with 'incl'.","incl"),
    ]
    for attr,tok,defn,never in glossary:
        con.execute("INSERT OR IGNORE INTO scope_glossary VALUES (?,?,?,?)",[attr,tok,defn,never])

    # ---- reference_docs (the 17 Other) ----
    def classify(path):
        p=path.lower()
        if "impact-assessment" in p or "esia" in p or "metbassta" in p or "resettlement" in p or "grievance" in p or "stakeholder" in p or "esms" in p: return "esia"
        if "guide" in p: return "guide"
        if "strateg" in p or "plan_action" in p or "pst" in p: return "strategy"
        if "revue" in p or "depot_etude" in p: return "study"
        return "other"
    for r in L.load_manifest():
        path=r["local_path"]; fname=path.split("/")[-1]
        if r["report_family"]=="Other":
            con.execute("INSERT OR IGNORE INTO reference_docs VALUES (?,?,?,?,?,?,?,?)",
                [r["file_id"], fname, classify(path), r["period"] or None, r["language"],
                 path, r["source_url"], None])
        # ANME Memento-2014 (energy-efficiency, not ONEM supply series) -> catalog only
        elif r["report_family"]=="Memento" and r["period"]=="2014":
            con.execute("INSERT OR IGNORE INTO reference_docs VALUES (?,?,?,?,?,?,?,?)",
                [r["file_id"], fname, "anme_efficiency", "2014", r["language"], path,
                 r["source_url"], "ANME Chiffres clés Maîtrise de l'Energie — NOT the ONEM supply Memento; no time series"])
        # COVID bulletins (narrative, no structured tables) -> catalog only
        elif r["report_family"]=="Bulletin-COVID":
            con.execute("INSERT OR IGNORE INTO reference_docs VALUES (?,?,?,?,?,?,?,?)",
                [r["file_id"], fname, "covid_bulletin", r["period"] or None, r["language"], path,
                 r["source_url"], "Narrative monthly COVID energy bulletin; qualitative, no series (months covered by tabular Conjoncture 2020)"])

    con.commit()
    # report
    for t in ["flow","product","sector","sector_crosswalk","region","field","field_membership",
              "level","producer","redevance_toggle","unit","conversion_factor","indicator",
              "footnote","reference_docs"]:
        n=con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:20} {n}")
    con.close()
    print("SEED OK")

if __name__=="__main__":
    main()
