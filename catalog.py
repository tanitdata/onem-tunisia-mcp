"""
catalog.py — Phase E. Emits series_catalog.{md,csv} (one row per distinct series,
seeding the future MCP semantic layer) and reference_docs.csv (the 17 'Other' docs).
"""
import csv
import duckdb

DB = "energy.duckdb"

_MONTH = {1:"janvier",2:"février",3:"mars",4:"avril",5:"mai",6:"juin",7:"juillet",
          8:"août",9:"septembre",10:"octobre",11:"novembre",12:"décembre"}

def display_name(row):
    """Mechanically build a disambiguated name from the distinguishing attributes so no
    two physically-different series (PCI vs PCS, annual vs YTD, scope/geo twins) collapse
    to the same string. `row` is a dict of the catalog columns."""
    parts = [row["indicator"]]
    dims = []
    for key, label in [("flow","flow"),("product","product"),("field","field"),("level","level"),
                       ("sector","sector"),("region","region"),("producer","producer"),
                       ("technology","tech"),("scope","scope"),("geography_scope","geo"),
                       ("redevance_toggle","redevance")]:
        if row.get(key):
            dims.append(f"{label}={row[key].split('.')[-1]}")
    if dims:
        parts.append("[" + ", ".join(dims) + "]")
    # basis + unit
    if row["calorific_basis"] and row["calorific_basis"] != "NA":
        parts.append(f"basis {row['calorific_basis']}")
    parts.append(f"({row['unit']})")
    # period semantics
    pt = row["period_type"]
    if pt == "ytd_cumulative":
        parts.append("year-to-date")
    elif pt == "annual":
        parts.append("annual")
    elif pt == "monthly":
        parts.append("monthly")
    return " — ".join([parts[0], " ".join(parts[1:])]).strip()

def aggregation_role(d):
    """FIX 6: human/LLM-readable aggregation role, so totals/subtotals/leaves/alternatives
    aren't flat siblings. Derived from is_total + the dimensions present:
      leaf                  -> is_total = FALSE (safe to sum within a partition)
      grand_total           -> is_total, with no sub-dimension (the table/flow total)
      alternative_breakdown -> is_total pressure/usage rows that re-partition a total
                               (gas-demand HP/MBP; the non-canonical partition)
      subtotal              -> is_total with a sub-dimension that aggregates finer leaves
                               (Gasoil over its variants; STEG over its carriers)
    """
    if not d.get("is_total"):
        return "leaf"
    # alternative-breakdown: gas-demand pressure rows (level set on a demand flow)
    if d.get("level") and d.get("flow") == "flow.demand":
        return "alternative_breakdown"
    # subtotal: carries a sub-dimension (product/producer/field) that has finer children
    if d.get("product") in ("prod.gasoil","prod.gasoline","prod.fuel_oil") \
       or d.get("producer") == "prod.steg":
        return "subtotal"
    # grand_total: no distinguishing sub-dimension beyond the flow/indicator
    return "grand_total"

def main():
    con = duckdb.connect(DB)
    # One row per series_key (the stable series ID), with definition + dimensions.
    rows = con.execute("""
        SELECT o.series_key,
               i.canonical_name AS indicator,
               i.definition,
               o.unit_id, o.calorific_basis, o.basis_confidence, o.period_type,
               o.flow_id, o.product_id, o.sector_id, o.region_id, o.field_id,
               o.level_id, o.producer_id, o.technology, o.scope, o.geography_scope,
               o.redevance_toggle_id,
               BOOL_OR(o.is_total)            AS is_total,
               MAX(o.aggregation_role)        AS aggregation_role,
               COUNT(*)                       AS n_obs,
               MIN(o.ref_year)                AS first_year,
               MAX(o.ref_year)                AS last_year,
               COUNT(DISTINCT o.source_id)    AS n_sources,
               STRING_AGG(DISTINCT s.report_type, '/') AS families,
               STRING_AGG(DISTINCT o.template_version, '/') AS templates,
               MIN(o.extraction_confidence)   AS confidence,
               BOOL_OR(o.is_escalated)        AS escalated
        FROM observation o
        JOIN indicator i ON i.indicator_id=o.indicator_id
        JOIN source   s ON s.source_id=o.source_id
        GROUP BY ALL
        ORDER BY i.canonical_name, o.series_key
    """).fetchall()
    raw_cols = ["series_id","indicator","definition","unit","calorific_basis","basis_confidence",
            "period_type","flow","product","sector","region","field","level","producer",
            "technology","scope","geography_scope","redevance_toggle","is_total","aggregation_role",
            "n_obs","first_year",
            "last_year","n_sources","families","templates","confidence","escalated"]
    # display_name + definition; aggregation_role comes from the observation column.
    tail = [c for c in raw_cols[3:] if c != "aggregation_role"]
    out_cols = ["series_id","display_name","aggregation_role","definition"] + tail
    out_rows = []
    seen_names = {}
    for r in rows:
        d = dict(zip(raw_cols, r))
        role = d["aggregation_role"]
        dn = display_name(d)
        # guarantee uniqueness: if a name still repeats, append the full stable series_id
        if dn in seen_names and seen_names[dn] != d["series_id"]:
            dn = dn + f"  «{d['series_id']}»"
        seen_names[dn] = d["series_id"]
        definition = (f"{d['indicator']}. {dn}. Source families: {d['families']}; "
                      f"period_type={d['period_type']}; years {d['first_year']}–{d['last_year']}. "
                      f"Aggregation role: {role}.")
        out_rows.append([d["series_id"], dn, role, definition] + [d[c] for c in tail])
    with open("series_catalog.csv","w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(out_cols)
        for r in out_rows: w.writerow(r)
    cols = raw_cols  # for downstream md section

    with open("series_catalog.md","w",encoding="utf-8") as f:
        f.write("# series_catalog.md — Phase E\n\n")
        f.write(f"Every distinct time series in the store ({len(rows)} series), keyed by the stable "
                "`series_id` (= `series_key`: controlled-vocabulary IDs only, stable across re-runs). "
                "This seeds the future MCP semantic layer: each row is one sliceable series with its "
                "definition, dimensions, unit, calorific basis, period_type, source families, and "
                "template-version provenance.\n\n")
        f.write("**series_id composition:** "
                "`indicator|flow|product|sector|region|field|level|producer|basis|unit|period_type|"
                "redevance|scope|technology|regime|geography_scope`.\n\n")
        # group by indicator for readability
        by_ind={}
        for r in rows:
            by_ind.setdefault(r[1],[]).append(r)
        f.write("## Series by indicator\n\n")
        f.write("| indicator | #series | unit(s) | basis | period_types | years | families |\n|---|---|---|---|---|---|---|\n")
        agg=con.execute("""SELECT i.canonical_name, COUNT(DISTINCT o.series_key),
                STRING_AGG(DISTINCT o.unit_id,','), STRING_AGG(DISTINCT o.calorific_basis,','),
                STRING_AGG(DISTINCT o.period_type,','),
                MIN(o.ref_year)||'-'||MAX(o.ref_year),
                STRING_AGG(DISTINCT s.report_type,'/')
              FROM observation o JOIN indicator i ON i.indicator_id=o.indicator_id
              JOIN source s ON s.source_id=o.source_id
              GROUP BY i.canonical_name ORDER BY 1""").fetchall()
        for name,ns,units,basis,pts,yrs,fams in agg:
            f.write(f"| {name} | {ns} | {units} | {basis} | {pts} | {yrs} | {fams} |\n")
        f.write("\nFull per-series detail (stable IDs, dimensions) in **series_catalog.csv**.\n")

    # reference_docs.csv (the 17 Other docs)
    refs = con.execute("""SELECT doc_id,title,doc_type,doc_date,language,local_path,source_url
                          FROM reference_docs ORDER BY doc_type,title""").fetchall()
    with open("reference_docs.csv","w",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        w.writerow(["doc_id","title","type","date","language","local_path","source_url"])
        for r in refs: w.writerow(r)
    # also add the ANME Memento-2014 + COVID bulletins as cataloged non-series docs
    con.close()
    print(f"series_catalog: {len(rows)} series; reference_docs: {len(refs)} docs")

if __name__ == "__main__":
    main()
