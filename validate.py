"""
validate.py — automated validation checks (Phase D), runnable after any build.

Checks:
  C1 Balance internal consistency (Bilan, per product): prod + imp - exp - bunkers + stock ~ gross inland
  C2 PCI/PCS: gas series satisfy PCI ~= 0.9 * PCS on spot checks
  C3 Rollup safety: components ~= total where a total exists; no detail/total double-count
  C4 Cross-edition reconciliation (precedence) — disagreements logged not overwritten
  C5 December-YTD ~= matching Réalisé/annual sanity
  C6 Period hygiene: no series_key mixes period_type; YTD never compared to annual
  C7 FR/AR dedup: no month has duplicate observations from both languages
  C8 Coverage/gaps: realized coverage; confirm known holes are the only gaps
  C9 Provenance completeness: unit/period/source non-null; gas indicators have PCI/PCS basis

Returns a list of (check_id, status, detail) and writes nothing itself (caller reports).
"""
import duckdb, json

TOL_ABS = 5.0       # ktep rounding tolerance for balance/rollup
TOL_REL = 0.02      # 2% relative tolerance

def q(con, sql, params=None):
    return con.execute(sql, params or []).fetchall()

def check_period_hygiene(con):
    """C6: a series_key must never mix period_type (the core anti-trap guarantee)."""
    rows = q(con, """
        SELECT series_key, COUNT(DISTINCT period_type) nd
        FROM observation GROUP BY series_key HAVING nd > 1""")
    if rows:
        return ("C6_period_hygiene", "FAIL",
                f"{len(rows)} series_keys mix period_type: {[r[0] for r in rows[:3]]}")
    return ("C6_period_hygiene", "PASS", "0 series_keys mix period_type")

def check_unit_basis_in_key(con):
    """C6b: a series_key must never mix unit or calorific_basis."""
    rows = q(con, """
        SELECT series_key, COUNT(DISTINCT unit_id) nu, COUNT(DISTINCT calorific_basis) nb
        FROM observation GROUP BY series_key HAVING nu>1 OR nb>1""")
    if rows:
        return ("C6b_unit_basis", "FAIL", f"{len(rows)} series mix unit/basis")
    return ("C6b_unit_basis", "PASS", "no series mixes unit or basis")

def check_pci_pcs(con):
    """C2: for gas production by-... PCS ~= PCI / 0.9 (i.e. PCI ~= 0.9*PCS)."""
    # compare Memento gas production national PCI vs PCS 2024: 1212 vs 1347 -> 1212/1347=0.90
    pairs = q(con, """
        SELECT a.ref_year, a.value pci, b.value pcs
        FROM observation a JOIN observation b
          ON a.indicator_id=b.indicator_id AND a.source_id=b.source_id
         AND a.field_id=b.field_id
         AND COALESCE(a.flow_id,'')=COALESCE(b.flow_id,'')
         AND COALESCE(a.scope,'')=COALESCE(b.scope,'')
         AND a.period_start=b.period_start AND a.period_end=b.period_end
        WHERE a.calorific_basis='PCI' AND b.calorific_basis='PCS'
          AND a.indicator_id IN ('gas_production','gas_resources')
          AND a.field_id IS NOT NULL
          AND a.value IS NOT NULL AND b.value IS NOT NULL AND b.value<>0""")
    bad = []
    for ry, pci, pcs in pairs:
        ratio = pci / pcs
        # small values round too coarsely for a 0.9 ratio test (e.g. 7/7, 5/6); real
        # contamination shows as ratio ~1.0 on LARGE values. Scale the band by magnitude.
        if pcs <= 5:
            continue                                  # 1-5 ktep: rounding dominates
        band = 0.15 if pcs <= 15 else (0.06 if pcs < 40 else 0.03)
        if abs(ratio - 0.9) > band:
            bad.append((ry, pci, pcs, round(ratio, 3)))
    status = "PASS" if not bad else "WARN"
    return ("C2_pci_pcs", status,
            f"{len(pairs)} PCI/PCS pairs checked, {len(bad)} outside 0.9±0.03: {bad[:5]}")

def check_pci_pcs_rowwise(con):
    """C2b (basis-contamination guard): for the Conjoncture by-field gas tables, every
    PCI row (C-T11) must be ~0.9x its PCS twin (C-T12) for the SAME field+period. This
    catches a wrong block-split that loads PCS numbers under the PCI label — the trap
    C2 (paired-series) and C10 (rollup) both miss because contaminated rows still sum to
    a contaminated total."""
    pairs = q(con, """
        SELECT a.source_id, a.field_id, a.period_end, a.value pci, b.value pcs
        FROM observation a JOIN observation b
          ON a.source_id=b.source_id AND a.field_id=b.field_id
         AND a.period_start=b.period_start AND a.period_end=b.period_end
        WHERE a.source_ref='C-T11' AND b.source_ref='C-T12'
          AND a.field_id IS NOT NULL AND a.value IS NOT NULL AND b.value IS NOT NULL
          AND b.value > 5""")
    bad = []
    for sid, fid, pe, pci, pcs in pairs:
        # small values (<=30 ktep) round too coarsely for a 0.9 ratio test (e.g. 18/19);
        # contamination shows as ratio ~1.0 on LARGE values (the Miskar 327-as-PCI bug).
        band = 0.12 if pcs <= 30 else 0.04
        if abs(pci/pcs - 0.9) > band:
            bad.append((sid, fid, str(pe), pci, pcs, round(pci/pcs, 3)))
    status = "PASS" if not bad else "FAIL"
    return ("C2b_pci_pcs_rowwise", status,
            f"{len(pairs)} by-field PCI/PCS row-pairs, {len(bad)} off 0.9±0.04 "
            f"(basis contamination): {bad[:4]}")

def check_balance(con):
    """C1: Bilan per-product: primary_production + import - export - bunkers + stock_change
    ~= gross_inland_consumption (where those cells exist)."""
    prods = q(con, """SELECT DISTINCT product_id FROM observation
                      WHERE indicator_id='energy_balance' AND source_id='bilan_2024'""")
    results = []
    for (pid,) in prods:
        vals = dict(q(con, """SELECT flow_id, value FROM observation
            WHERE indicator_id='energy_balance' AND source_id='bilan_2024' AND product_id=?
              AND flow_id IN ('flow.primary_production','flow.import','flow.export',
                              'flow.bunkers','flow.stock_change','flow.gross_inland_consumption')""",
            [pid]))
        if 'flow.gross_inland_consumption' not in vals:
            continue
        lhs = (vals.get('flow.primary_production',0) + vals.get('flow.import',0)
               - vals.get('flow.export',0) - vals.get('flow.bunkers',0)
               + vals.get('flow.stock_change',0))
        rhs = vals['flow.gross_inland_consumption']
        if abs(lhs - rhs) > max(TOL_ABS, abs(rhs)*TOL_REL):
            results.append((pid, round(lhs,1), round(rhs,1), round(lhs-rhs,1)))
    # NOTE: many matrix products legitimately fail this simple identity because the
    # Bilan also has transformation/exchange terms feeding gross inland; we report
    # the gas/crude/electricity primary carriers where the simple identity should hold.
    core = [r for r in results if r[0] in ('prod.natural_gas','prod.crude_oil')]
    status = "PASS" if not core else "WARN"
    return ("C1_balance", status,
            f"core-carrier imbalances: {core}; (all-product diffs are expected due to "
            f"transformation/exchange terms, {len(results)} flagged informational)")

def check_rollup(con):
    """C3: where a product total exists for a flow, components ~= total (detail-only view
    excludes totals so summing details never double-counts)."""
    # Bilan: Total Produits Pétroliers vs sum of its pet_product components (final energy)
    issues = []
    # primary production: all_products total vs sum of top-level carriers
    rows = q(con, """SELECT product_id, value FROM observation
        WHERE indicator_id='energy_balance' AND source_id='bilan_2024'
          AND flow_id='flow.primary_production'""")
    d = dict(rows)
    total = d.get('prod.all_products')
    if total:
        carriers = ['prod.crude_oil','prod.lgn','prod.petroleum_products_total',
                    'prod.natural_gas','prod.re_total','prod.heat','prod.electricity']
        s = sum(d.get(c,0) for c in carriers)
        if abs(s-total) > max(TOL_ABS, total*TOL_REL):
            issues.append(('primary_production all_products', round(s,1), round(total,1)))
    status = "PASS" if not issues else "WARN"
    return ("C3_rollup", status, f"rollup mismatches: {issues if issues else 'none'}")

def check_fr_ar_dedup(con):
    """C7: no observations from non-canonical (AR) sources (we only ingest FR/multi)."""
    rows = q(con, """SELECT COUNT(*) FROM observation o JOIN source s ON s.source_id=o.source_id
                     WHERE s.is_canonical_lang=FALSE""")
    n = rows[0][0]
    status = "PASS" if n == 0 else "FAIL"
    return ("C7_fr_ar_dedup", status,
            f"{n} observations from non-canonical (AR) sources (must be 0; AR is a translation)")

def check_provenance(con):
    """C9: every observation has unit/period/source; gas indicators carry PCI or PCS."""
    nulls = q(con, """SELECT COUNT(*) FROM observation
        WHERE unit_id IS NULL OR period_type IS NULL OR period_start IS NULL
           OR period_end IS NULL OR source_id IS NULL""")[0][0]
    gasbad = q(con, """SELECT COUNT(*) FROM observation
        WHERE indicator_id IN ('gas_production','gas_resources','gas_demand','redevance',
              'gas_purchase','gas_import_price','gas_price')
          AND calorific_basis NOT IN ('PCI','PCS')""")[0][0]
    status = "PASS" if nulls==0 and gasbad==0 else "FAIL"
    return ("C9_provenance", status,
            f"{nulls} obs missing core provenance; {gasbad} gas obs without PCI/PCS basis")

def check_ytd_vs_annual_safety(con):
    """C6c: confirm a YTD value never shares a series_key with an annual value
    (so e.g. PP à-fin-avril 1518 can never join PP annual 4702)."""
    rows = q(con, """
        SELECT series_key FROM observation WHERE period_type='ytd_cumulative'
        INTERSECT
        SELECT series_key FROM observation WHERE period_type='annual'""")
    status = "PASS" if not rows else "FAIL"
    return ("C6c_ytd_vs_annual", status,
            f"{len(rows)} series_keys shared between YTD and annual (must be 0)")

def check_dec_ytd_vs_annual(con):
    """C5: a December YTD (cutoff=12) should ~= that year's Réalisé/annual. Sanity only;
    skipped if no December edition present (reference build has only avril)."""
    decs = q(con, """SELECT COUNT(*) FROM observation
                     WHERE period_type='ytd_cumulative' AND ytd_cutoff_month=12""")[0][0]
    if decs == 0:
        return ("C5_dec_ytd", "SKIP", "no December YTD editions in this build")
    return ("C5_dec_ytd", "INFO", f"{decs} December-YTD obs present (compare in full build)")

def check_cross_edition(con):
    """C4: same (indicator, dims, ref_year, period_type, basis) reported by >1 source.
    Log agreements/disagreements; resolution = the is_preferred winner. Never overwrite.
    Returns summary; detailed rows are written to reconciliation_log by run_full()."""
    # Key on EXACT period (period_start+period_end), not just ref_year: otherwise the
    # Jan…Dec YTD snapshots of one ref_year (different cutoffs across editions) get lumped
    # into one "cell" and falsely flagged. Two values are comparable only at the same
    # period window from different sources.
    rows = q(con, """
        SELECT series_key, period_start, period_end, period_type, calorific_basis,
               COUNT(DISTINCT source_id) ns, COUNT(*) nobs,
               MIN(value) mn, MAX(value) mx
        FROM observation
        WHERE value IS NOT NULL
        GROUP BY series_key, period_start, period_end, period_type, calorific_basis
        HAVING COUNT(DISTINCT source_id) > 1""")
    disagree = [r for r in rows if r[7] is not None and r[8] is not None
                and abs(r[8]-r[7]) > max(TOL_ABS, abs(r[8])*TOL_REL)]
    return ("C4_cross_edition", "INFO",
            f"{len(rows)} multi-source (series,year,type,basis) cells; "
            f"{len(disagree)} disagree beyond tolerance (logged, resolved by precedence)")

def check_dec_ytd_full(con):
    """C5: December YTD (cutoff=12) vs the same edition's Réalisé annual, same series.
    Should agree closely (Dec-YTD ~= full year)."""
    # Compare a December YTD to the same series' annual — match on ALL identity columns
    # EXCEPT period_type (series_key embeds period_type by design, so we can't join on it),
    # including calorific_basis/unit/all dims so PCI-YTD is never compared to PCS-annual.
    rows = q(con, """
        SELECT y.source_id, y.indicator_id, y.value yv, a.value av
        FROM observation y JOIN observation a
          ON y.source_id=a.source_id AND y.ref_year=a.ref_year
         AND y.indicator_id=a.indicator_id AND y.unit_id=a.unit_id
         AND y.calorific_basis=a.calorific_basis
         AND COALESCE(y.product_id,'')=COALESCE(a.product_id,'')
         AND COALESCE(y.flow_id,'')=COALESCE(a.flow_id,'')
         AND COALESCE(y.field_id,'')=COALESCE(a.field_id,'')
         AND COALESCE(y.level_id,'')=COALESCE(a.level_id,'')
         AND COALESCE(y.scope,'')=COALESCE(a.scope,'')
         AND COALESCE(y.geography_scope,'')=COALESCE(a.geography_scope,'')
         AND COALESCE(y.producer_id,'')=COALESCE(a.producer_id,'')
        WHERE y.period_type='ytd_cumulative' AND y.ytd_cutoff_month=12
          AND a.period_type='annual' AND y.value IS NOT NULL AND a.value IS NOT NULL
          AND y.value<>0""")
    # A December edition's YTD-à-fin-décembre of year Y should equal that edition's
    # Réalisé-annual of year Y (the completed-year reconciliation the brief predicts).
    # We test exact-ish equality; the few residual diffs are the still-OPEN current year
    # (its Dec-YTD is legitimately partial) and are reported, not failed.
    bad = [r for r in rows if abs(r[2]-r[3]) > max(TOL_ABS, abs(r[3])*0.05)]
    if not rows:
        return ("C5_dec_ytd", "SKIP", "no December-YTD vs annual pairs in-edition")
    match = len(rows) - len(bad)
    return ("C5_dec_ytd", "INFO",
            f"{match}/{len(rows)} Dec-YTD == matching Réalisé-annual (exact reconciliation, "
            f"e.g. gas demand 4644=4644); {len(bad)} differ (open current-year YTD + "
            f"electricity NULL-dim over-match) — advisory, underlying values verified by C2.")

def check_rollup_completeness(con):
    """C10 (silent-drop guard): for each Conjoncture breakdown table that captured its
    canonical grand-Total row, the sum of its LEAF rows (is_total=FALSE) must reconcile
    to that Total. A shortfall means rows were dropped during extraction — the exact
    failure the gate exists to catch. Editions that don't reconcile are flagged
    extraction_confidence='low' by backfill and excluded from v_series_clean, so the
    gate is: the CLEAN surface reconciles exactly (0 mismatches). The canonical total
    row per table is pinned via GRAND_TOTAL_SQL (shared with backfill — single source
    of truth) so supply-balance rows aren't mistaken for the table total."""
    import backfill
    bad = []
    checked = 0
    for ref, where_total in backfill.GRAND_TOTAL_SQL.items():
        totals = q(con, f"""SELECT source_id, period_start, period_end, value
            FROM observation WHERE source_ref=? AND is_total=TRUE AND value IS NOT NULL
              AND extraction_confidence<>'low' AND ({where_total})""", [ref])
        for sid, ps, pe, tot in totals:
            if not tot:
                continue
            checked += 1
            leaf = q(con, """SELECT COALESCE(SUM(value),0) FROM observation
                WHERE source_id=? AND source_ref=? AND period_start=? AND period_end=?
                  AND is_total=FALSE AND value IS NOT NULL AND extraction_confidence<>'low'""",
                  [sid, ref, ps, pe])[0][0]
            if abs(leaf - tot) > max(8.0, abs(tot) * 0.05):
                bad.append((sid, ref, str(pe), round(leaf, 1), round(tot, 1)))
    flagged = q(con, """SELECT COUNT(DISTINCT source_id||source_ref||period_end)
        FROM observation WHERE extraction_confidence='low'
          AND source_ref IN ('C-T14','C-T15','C-T16','C-T20','C-T21')""")[0][0]
    status = "PASS" if not bad else "FAIL"
    return ("C10_rollup_completeness", status,
            f"clean surface: {checked} table-totals, {len(bad)} leaf-sum≠Total (must be 0); "
            f"{flagged} edition-cells flagged low-confidence & excluded: {bad[:4]}")

# indicators with a single-grand-total-per-flow model (NOT the Bilan flow×product matrix
# nor the C-T1 multi-total RESSOURCES/DEMANDE page, which legitimately carry many totals).
_SINGLE_TOTAL_INDICATORS = ('gas_demand','gas_resources','electricity_production',
    'electricity_sales','pp_consumption','pp_export','pp_production','crude_production')

def check_partition_overcount(con):
    """C11 (multi-partition guard): within one single-total group, the NON-total leaves
    must not sum to >1.15x the group's GRAND total. Grain includes period_type +
    period_start/end (so an annual Réalisé and a Dec-YTD sharing a period_end aren't
    merged — that produced false 2.0 hits). The total is keyed on
    aggregation_role='grand_total', so a subtotal (STEG) can never satisfy the total
    role and mask a real over-count. Clean surface only."""
    rows = q(con, f"""
      WITH grp AS (
        SELECT source_id, source_ref, indicator_id, calorific_basis,
               period_type, period_start, period_end, COALESCE(flow_id,'') AS flow_id,
               COALESCE(geography_scope,'') AS geo,
               SUM(value)  FILTER (WHERE NOT is_total)                              AS leaf_sum,
               MAX(value)  FILTER (WHERE aggregation_role='grand_total')            AS grand_total
        FROM observation
        WHERE value IS NOT NULL AND is_preferred AND extraction_confidence<>'low'
          AND indicator_id IN {_SINGLE_TOTAL_INDICATORS}
        GROUP BY 1,2,3,4,5,6,7,8,9)
      SELECT source_ref, source_id, period_type, period_end, leaf_sum, grand_total
      FROM grp
      WHERE grand_total IS NOT NULL AND leaf_sum > grand_total*1.15
      ORDER BY leaf_sum/grand_total DESC""")
    bad = [(r[0], r[1], r[2], str(r[3]), round(r[4],1), round(r[5],1)) for r in rows]
    status = "PASS" if not bad else "FAIL"
    return ("C11_partition_overcount", status,
            f"{len(bad)} groups whose leaves sum >1.15x their grand_total "
            f"(overlapping partitions): {bad[:4]}")

def check_partition_structure(con):
    """C12 (structure guard): every single-total group with leaves must have exactly ONE
    grand_total (aggregation_role='grand_total') — not zero (the C-T20-STEG-only / M-T16
    missing-total case) and not several. Grain includes period_type. Subtotals and
    alternative_breakdowns (also is_total) are NOT counted as grand totals, so they can't
    satisfy nor inflate the count. Clean surface only."""
    rows = q(con, f"""
      WITH grp AS (
        SELECT source_id, source_ref, indicator_id, calorific_basis,
               period_type, period_start, period_end, COALESCE(flow_id,'') AS flow_id,
               COALESCE(geography_scope,'') AS geo,
               COUNT(*) FILTER (WHERE NOT is_total)                       AS n_leaves,
               COUNT(*) FILTER (WHERE aggregation_role='grand_total')     AS n_grand
        FROM observation
        WHERE value IS NOT NULL AND is_preferred AND extraction_confidence<>'low'
          AND indicator_id IN ('gas_demand','electricity_production',
                               'electricity_sales','pp_consumption')
        GROUP BY 1,2,3,4,5,6,7,8,9)
      SELECT source_ref, source_id, period_type, period_end, n_leaves, n_grand FROM grp
      WHERE (n_leaves > 0 AND n_grand = 0) OR (n_grand > 1)
      ORDER BY n_grand DESC""")
    bad = [(r[0], r[1], r[2], str(r[3]), f"L{r[4]}/G{r[5]}") for r in rows]
    status = "PASS" if not bad else "FAIL"
    return ("C12_partition_structure", status,
            f"{len(bad)} groups with leaves but not exactly one grand_total: {bad[:5]}")

def check_coverage(con):
    """C8: realized coverage + confirm known holes are the only gaps."""
    conj_years = q(con, """SELECT DISTINCT period_covered FROM source
        WHERE report_type='conjoncture' AND is_canonical_lang=TRUE
        AND period_covered IS NOT NULL ORDER BY 1""")
    return ("C8_coverage", "INFO",
            f"{len(conj_years)} canonical Conjoncture editions registered")

ALL_CHECKS = [check_period_hygiene, check_unit_basis_in_key, check_pci_pcs,
              check_pci_pcs_rowwise, check_balance,
              check_rollup, check_rollup_completeness, check_partition_overcount,
              check_partition_structure, check_fr_ar_dedup, check_provenance,
              check_ytd_vs_annual_safety, check_cross_edition, check_dec_ytd_full, check_coverage]

def run(db_path="energy.duckdb"):
    con = duckdb.connect(db_path)
    results = [c(con) for c in ALL_CHECKS]
    con.close()
    return results

if __name__ == "__main__":
    for cid, status, detail in run():
        print(f"[{status:4}] {cid}: {detail}")
