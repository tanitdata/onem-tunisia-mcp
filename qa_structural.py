"""
qa_structural.py — Agent A structural / schema QA suite (re-runnable).

Read-only. Tests STRUCTURE, KEYS, RELATIONSHIPS, CONSTRAINTS, and READ-PATH
soundness of energy.duckdb — assuming VALUE correctness is audited elsewhere
(see audit_round4.md). Each check emits PASS/FAIL/ADVISE + offending rows.

Designed to re-run unchanged after fixes and after each future ingest:
    python qa_structural.py                 # human-readable
    python qa_structural.py --json          # machine-readable
    python qa_structural.py --db other.duckdb

Severity tagging on findings:
    BLOCK    — integrity violation / can produce wrong answers
    ADVISE   — correct but suboptimal (clarity/perf/maintainability/hardening)
    COSMETIC — naming/style, no correctness impact

A check's STATUS is PASS when no offending rows, else the check's declared
severity (BLOCK by default). ADVISE/INFO checks never gate.
"""
import duckdb, json, sys

DB_DEFAULT = "energy.duckdb"

# ---- in-domain enum vocabularies (mirror schema.sql CHECK clauses) ----
ENUMS = {
    ("observation", "period_type"): {"annual", "ytd_cumulative", "monthly", "point_in_time"},
    ("observation", "calorific_basis"): {"PCI", "PCS", "NA"},
    ("observation", "basis_confidence"): {"stated", "inferred", "na"},
    ("observation", "data_status"): {"final", "provisional", "estimated", "revised"},
    ("observation", "extraction_confidence"): {"normal", "low"},
    ("observation", "confidence"): {"normal", "low"},
    ("observation", "source_type"): {"table", "chart_label"},
    ("observation", "extraction_method"): {"text_geometry", "coordinate_map", "ocr", "chart_label"},
}
BOOL_COLS = ["is_total", "is_preferred", "is_escalated", "is_derived"]

# Every observation FK column -> (dim table, dim PK)
OBS_FKS = [
    ("indicator_id", "indicator", "indicator_id"),
    ("unit_id", "unit", "unit_id"),
    ("flow_id", "flow", "flow_id"),
    ("product_id", "product", "product_id"),
    ("sector_id", "sector", "sector_id"),
    ("region_id", "region", "region_id"),
    ("field_id", "field", "field_id"),
    ("level_id", "level", "level_id"),
    ("producer_id", "producer", "producer_id"),
    ("source_id", "source", "source_id"),
    ("redevance_toggle_id", "redevance_toggle", "toggle_id"),
]

# mandatory provenance / payload columns that must never be NULL
MANDATORY_NONNULL = ["source_id", "source_page", "period_type", "unit_id",
                     "extraction_method", "template_version", "indicator_id",
                     "period_start", "period_end", "series_key", "upsert_key"]


class Result:
    def __init__(self, cid, area, severity, status, summary, rows=None, note=""):
        self.cid, self.area, self.severity = cid, area, severity
        self.status, self.summary = status, summary
        self.rows = rows or []
        self.note = note

    def as_dict(self):
        return dict(id=self.cid, area=self.area, severity=self.severity,
                    status=self.status, summary=self.summary,
                    offending=self.rows[:25], note=self.note)


def q(con, sql, params=None):
    return con.execute(sql, params or []).fetchall()


def mk(cid, area, severity, offending, summary_ok, summary_bad, note=""):
    """PASS if no offending rows, else the check's severity. summary_bad may use {n}."""
    if offending:
        return Result(cid, area, severity, severity, summary_bad.format(n=len(offending)),
                      offending, note)
    return Result(cid, area, severity, "PASS", summary_ok, [], note)


# =====================================================================
# 1. KEYS & UNIQUENESS
# =====================================================================
def c1_pk_observation(con):
    rows = q(con, "SELECT observation_id, COUNT(*) FROM observation GROUP BY 1 HAVING COUNT(*)>1")
    nulls = q(con, "SELECT COUNT(*) FROM observation WHERE observation_id IS NULL")[0][0]
    off = rows + ([("<NULL pk>", nulls)] if nulls else [])
    return mk("1.1_obs_pk", "keys", "BLOCK", off,
              "observation_id is a working PK (unique, non-null)",
              "{n} observation_id duplicates/nulls")


def c1_upsert_unique(con):
    rows = q(con, "SELECT upsert_key, COUNT(*) FROM observation GROUP BY 1 HAVING COUNT(*)>1")
    return mk("1.2_upsert_unique", "keys", "BLOCK", rows,
              "UNIQUE(upsert_key) holds: 0 idempotency-key violations",
              "{n} upsert_keys appear on >1 observation (idempotency broken)")


def c1_upsert_definition(con):
    """upsert_key MUST equal series_key#period_start#period_end#source_id (ingestion_notes §3)."""
    rows = q(con, """SELECT observation_id, upsert_key FROM observation
        WHERE upsert_key <> series_key||'#'||CAST(period_start AS VARCHAR)||'#'||
              CAST(period_end AS VARCHAR)||'#'||source_id LIMIT 25""")
    return mk("1.3_upsert_definition", "keys", "BLOCK", rows,
              "upsert_key matches its documented definition for every row",
              "{n} rows whose upsert_key != series_key#start#end#source_id")


def c1_serieskey_collapse(con):
    """No two observations differing ONLY in period/source collapse to one row:
    every (series_key, period_start, period_end, source_id) tuple is unique
    (== upsert_key identity). Inverse already covered by 1.2."""
    rows = q(con, """SELECT series_key, period_start, period_end, source_id, COUNT(*)
        FROM observation GROUP BY 1,2,3,4 HAVING COUNT(*)>1 LIMIT 25""")
    return mk("1.4_seriesid_identity", "keys", "BLOCK", rows,
              "no (series_key,period,source) tuple maps to >1 observation",
              "{n} (series_key,period,source) tuples collapse >1 distinct observation")


def c1_serieskey_field_count(con):
    """series_key shape is stable (16 fields / 15 pipes) — a malformed key would
    mean the identity grammar drifted between loaders."""
    rows = q(con, """SELECT DISTINCT LENGTH(series_key)-LENGTH(REPLACE(series_key,'|','')) AS pipes
        FROM observation""")
    bad = [r for r in rows if r[0] != 15]
    return mk("1.5_seriskey_grammar", "keys", "BLOCK", bad,
              "every series_key has the canonical 16-field grammar",
              "{n} distinct malformed series_key pipe-counts (expected 15 pipes)")


# =====================================================================
# 2. REFERENTIAL INTEGRITY
# =====================================================================
def c2_obs_orphans(con):
    off = []
    for col, tbl, pk in OBS_FKS:
        n = q(con, f"""SELECT COUNT(*) FROM observation o
            WHERE o.{col} IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM {tbl} d WHERE d.{pk}=o.{col})""")[0][0]
        if n:
            off.append((col, tbl, n))
    return mk("2.1_obs_fk_orphans", "ref-integrity", "BLOCK", off,
              "every non-null observation FK resolves to a dimension row",
              "{n} observation FK columns have orphan references")


def c2_satellite_orphans(con):
    """observation_footnote, sector_crosswalk, field_membership, reconciliation_log,
    staging_unmapped reference valid keys (those without declared FKs tested here)."""
    off = []
    # observation_footnote.observation_id has NO declared FK (intentional) -> test it
    n = q(con, """SELECT COUNT(*) FROM observation_footnote f
        WHERE NOT EXISTS (SELECT 1 FROM observation o WHERE o.observation_id=f.observation_id)""")[0][0]
    if n:
        off.append(("observation_footnote.observation_id", n))
    # observation.supersedes_id is a plain column (no FK) -> verify it points at a real obs
    n = q(con, """SELECT COUNT(*) FROM observation o
        WHERE o.supersedes_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM observation t WHERE t.observation_id=o.supersedes_id)""")[0][0]
    if n:
        off.append(("observation.supersedes_id", n))
    # source.supersedes_source (declared FK, but verify data)
    n = q(con, """SELECT COUNT(*) FROM source s
        WHERE s.supersedes_source IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM source t WHERE t.source_id=s.supersedes_source)""")[0][0]
    if n:
        off.append(("source.supersedes_source", n))
    # reconciliation_log.series_key should match a real series_key when present
    n = q(con, """SELECT COUNT(*) FROM reconciliation_log r
        WHERE r.series_key IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM observation o WHERE o.series_key=r.series_key)""")[0][0]
    if n:
        off.append(("reconciliation_log.series_key", n))
    # staging_unmapped.source_id (no declared FK) — verify non-collision rows resolve
    n = q(con, """SELECT COUNT(*) FROM staging_unmapped u
        WHERE u.source_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM source s WHERE s.source_id=u.source_id)""")[0][0]
    if n:
        off.append(("staging_unmapped.source_id", n))
    return mk("2.2_satellite_orphans", "ref-integrity", "BLOCK", off,
              "satellite tables (footnote-link, supersedes, recon-log, staging) reference valid keys",
              "{n} satellite tables carry dangling references")


def c2_dead_dims(con):
    """Dimension members never referenced by any observation. ADVISE only:
    distinguish deliberately-seeded-for-future from dead. Reports counts per dim."""
    off = []
    for col, tbl, pk in OBS_FKS:
        if tbl == "source":
            continue  # sources legitimately exist without observations (AR, reference)
        unref = q(con, f"""SELECT {pk} FROM {tbl} d
            WHERE NOT EXISTS (SELECT 1 FROM observation o WHERE o.{col}=d.{pk})""")
        for (mid,) in unref:
            off.append((tbl, mid))
    return mk("2.3_unreferenced_dims", "ref-integrity", "ADVISE", off,
              "every dimension member is referenced by >=1 observation",
              "{n} dimension members are unreferenced (seeded-for-future vs dead — review)")


# =====================================================================
# 3. HIERARCHY INTEGRITY (highest priority)
# =====================================================================
def _hier_checks(con, tbl, idc, parc):
    """Returns (self_parent, dangling_parent, cycle) offending lists for one hierarchy."""
    self_p = q(con, f"SELECT {idc} FROM {tbl} WHERE {parc}={idc}")
    dangle = q(con, f"""SELECT {idc}, {parc} FROM {tbl} t
        WHERE {parc} IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM {tbl} p WHERE p.{idc}=t.{parc})""")
    # cycle detection via recursive walk with path
    cyc = q(con, f"""
        WITH RECURSIVE walk(start_id, cur, depth, path, looped) AS (
            SELECT {idc}, {parc}, 0, [{idc}], FALSE FROM {tbl} WHERE {parc} IS NOT NULL
            UNION ALL
            SELECT w.start_id, t.{parc}, w.depth+1,
                   list_append(w.path, t.{idc}),
                   list_contains(w.path, t.{idc})
            FROM walk w JOIN {tbl} t ON t.{idc}=w.cur
            WHERE w.cur IS NOT NULL AND NOT w.looped AND w.depth < 50
        )
        SELECT DISTINCT start_id FROM walk WHERE looped""")
    return self_p, dangle, cyc


def c3_hierarchy(con):
    off = []
    for tbl, idc, parc in [("product", "product_id", "parent_product_id"),
                           ("flow", "flow_id", "parent_flow_id"),
                           ("sector", "sector_id", "parent_sector_id")]:
        sp, dg, cy = _hier_checks(con, tbl, idc, parc)
        for (x,) in sp:
            off.append((tbl, "self_parent", x))
        for r in dg:
            off.append((tbl, "dangling_parent", r))
        for (x,) in cy:
            off.append((tbl, "CYCLE", x))
    return mk("3.1_hierarchy_acyclic", "hierarchy", "BLOCK", off,
              "product/flow/sector graphs: no self-parent, no dangling parent, no cycle (rooted forest)",
              "{n} hierarchy defects (self-parent / dangling / cycle)")


def c3_istotal_partition(con):
    """is_total semantics per ingestion_notes §7b: is_total is a per-ROW property of the
    observation (NOT the dimension). The summable partition = observation rows with
    is_total=FALSE; the detail view sums exactly those. Verify that within any single
    summable group (same source/ref/period/indicator/flow) the is_total=FALSE product
    rows form a NON-overlapping partition: no product appears as both a leaf AND, in the
    same group, as the parent of another leaf on the default path."""
    # parent product present as a detail (is_total=FALSE) leaf while its child is ALSO a
    # detail leaf in the same group => double-count risk on the default sum path.
    rows = q(con, """
        SELECT c.source_id, c.source_ref, CAST(c.period_end AS VARCHAR), c.indicator_id,
               p.parent_product_id AS parent, c.product_id AS child
        FROM v_series_detail c
        JOIN product p ON p.product_id=c.product_id AND p.parent_product_id IS NOT NULL
        JOIN v_series_detail par
          ON par.product_id=p.parent_product_id
         AND par.source_id=c.source_id
         AND COALESCE(par.source_ref,'')=COALESCE(c.source_ref,'')
         AND par.period_start=c.period_start AND par.period_end=c.period_end
         AND par.indicator_id=c.indicator_id
         AND COALESCE(par.flow_id,'')=COALESCE(c.flow_id,'')
         AND COALESCE(par.field_id,'')=COALESCE(c.field_id,'')
        LIMIT 25""")
    return mk("3.2_product_partition", "hierarchy", "BLOCK", rows,
              "product partition: no parent co-occurs with its child as summable leaves",
              "{n} groups where a parent product sums together with its own children")


def c3_flow_partition(con):
    rows = q(con, """
        SELECT c.source_id, c.source_ref, CAST(c.period_end AS VARCHAR), c.indicator_id,
               p.parent_flow_id AS parent, c.flow_id AS child
        FROM v_series_detail c
        JOIN flow p ON p.flow_id=c.flow_id AND p.parent_flow_id IS NOT NULL
        JOIN v_series_detail par
          ON par.flow_id=p.parent_flow_id
         AND par.source_id=c.source_id
         AND COALESCE(par.source_ref,'')=COALESCE(c.source_ref,'')
         AND par.period_start=c.period_start AND par.period_end=c.period_end
         AND par.indicator_id=c.indicator_id
         AND COALESCE(par.product_id,'')=COALESCE(c.product_id,'')
        LIMIT 25""")
    return mk("3.3_flow_partition", "hierarchy", "BLOCK", rows,
              "flow partition: no parent flow co-occurs with its child as summable leaves",
              "{n} groups where a parent flow sums together with its own children")


def c3_relation_type_presence(con):
    """STRUCTURAL FINDING (not a defect-per-edge): hierarchy edges carry no per-edge
    relation_type column — only parent_*_id + is_total + aggregation_level. Report
    whether edge meaning is recoverable without it. ADVISE; Agent B judges meaning."""
    cols = {r[1] for r in q(con, "PRAGMA table_info('product')")}
    has_rt = "relation_type" in cols or "edge_type" in cols
    off = [] if has_rt else [("product/flow", "no relation_type column on hierarchy edges")]
    return Result("3.4_relation_type", "hierarchy", "ADVISE",
                  "PASS" if has_rt else "ADVISE",
                  ("hierarchy carries an explicit per-edge relation_type"
                   if has_rt else
                   "hierarchy edges have NO relation_type column; meaning rests on "
                   "is_total + aggregation_level + parent_id. Recoverable for the current "
                   "is-a/part-of trees, but an MCP consumer cannot distinguish edge KINDS "
                   "(component-of vs alias-of vs basis-variant) from structure alone. "
                   "See Agent B for whether the MCP needs one."),
                  off)


# =====================================================================
# 4. ROLLUP SAFETY (structural — double-count impossible by construction)
# =====================================================================
def c4_detail_no_totals(con):
    n = q(con, "SELECT COUNT(*) FROM v_series_detail WHERE is_total=TRUE")[0][0]
    off = [("v_series_detail", n)] if n else []
    return mk("4.1_detail_excludes_totals", "rollup", "BLOCK", off,
              "v_series_detail contains 0 is_total=TRUE rows (sum path can't include totals)",
              "{n} is_total rows leaked into v_series_detail")


def c4_detail_only_preferred(con):
    n = q(con, "SELECT COUNT(*) FROM v_series_detail WHERE is_preferred=FALSE")[0][0]
    off = [("v_series_detail", n)] if n else []
    return mk("4.2_detail_preferred_only", "rollup", "BLOCK", off,
              "v_series_detail exposes only is_preferred rows",
              "{n} non-preferred rows leaked into v_series_detail")


# =====================================================================
# 5. QUALIFIER HYGIENE
# =====================================================================
def c5_enum_domains(con):
    off = []
    for (tbl, col), allowed in ENUMS.items():
        vals = q(con, f"SELECT DISTINCT {col} FROM {tbl} WHERE {col} IS NOT NULL")
        for (v,) in vals:
            if v not in allowed:
                off.append((tbl, col, v))
    return mk("5.1_enum_domains", "qualifier", "BLOCK", off,
              "all qualifier enums hold only in-domain values",
              "{n} out-of-domain enum values present")


def c5_no_mixed_period_basis(con):
    rows = q(con, """SELECT series_key, COUNT(DISTINCT period_type) pt,
        COUNT(DISTINCT calorific_basis) cb, COUNT(DISTINCT unit_id) u
        FROM observation GROUP BY series_key
        HAVING pt>1 OR cb>1 OR u>1 LIMIT 25""")
    return mk("5.2_series_no_mix", "qualifier", "BLOCK", rows,
              "no series_key mixes period_type / calorific_basis / unit",
              "{n} series_keys mix period_type, basis, or unit")


def c5_pcipcs_ytd_twins_distinct(con):
    """PCI/PCS twins and YTD/annual twins must be DISTINCT series_keys (already implied
    by 5.2, but assert positively that both bases & both period_types coexist as
    separate keys where the data has them)."""
    shared_basis = q(con, """SELECT series_key FROM observation WHERE calorific_basis='PCI'
        INTERSECT SELECT series_key FROM observation WHERE calorific_basis='PCS'""")
    shared_pt = q(con, """SELECT series_key FROM observation WHERE period_type='ytd_cumulative'
        INTERSECT SELECT series_key FROM observation WHERE period_type='annual'""")
    off = [("PCI/PCS share key", r[0]) for r in shared_basis] + \
          [("YTD/annual share key", r[0]) for r in shared_pt]
    return mk("5.3_twins_distinct", "qualifier", "BLOCK", off,
              "PCI/PCS twins and YTD/annual twins are distinct series_keys",
              "{n} series_keys illegally span PCI&PCS or YTD&annual")


def c5_period_consistency(con):
    off = []
    bad_range = q(con, "SELECT COUNT(*) FROM observation WHERE period_start > period_end")[0][0]
    if bad_range:
        off.append(("period_start>period_end", bad_range))
    ytd_no_cut = q(con, """SELECT COUNT(*) FROM observation
        WHERE period_type='ytd_cumulative' AND ytd_cutoff_month IS NULL""")[0][0]
    if ytd_no_cut:
        off.append(("ytd_cumulative missing ytd_cutoff_month", ytd_no_cut))
    # point_in_time: start==end is expected
    pit_bad = q(con, """SELECT COUNT(*) FROM observation
        WHERE period_type='point_in_time' AND period_start<>period_end""")[0][0]
    if pit_bad:
        off.append(("point_in_time start<>end", pit_bad))
    # annual: should span ~1 year (same ref_year)
    return mk("5.4_period_consistency", "qualifier", "BLOCK", off,
              "period_start<=period_end; YTD carries cutoff; point_in_time start==end",
              "{n} period-consistency violations")


# =====================================================================
# 6. TYPES, DOMAINS, MANDATORY FIELDS
# =====================================================================
def c6_mandatory_nonnull(con):
    off = []
    for col in MANDATORY_NONNULL:
        n = q(con, f"SELECT COUNT(*) FROM observation WHERE {col} IS NULL")[0][0]
        if n:
            off.append((col, n))
    return mk("6.1_mandatory_nonnull", "types", "BLOCK", off,
              "mandatory provenance columns are non-null on every observation",
              "{n} mandatory columns carry NULLs")


def c6_value_null_documented(con):
    """value may be NULL only where value_raw documents the missing/dash token."""
    rows = q(con, """SELECT observation_id, value_raw FROM observation
        WHERE value IS NULL AND (value_raw IS NULL OR TRIM(value_raw)='') LIMIT 25""")
    return mk("6.2_value_null_documented", "types", "ADVISE", rows,
              "every NULL value carries a documenting value_raw token",
              "{n} NULL values lack any value_raw documentation")


def c6_bool_clean(con):
    """DuckDB BOOLEAN is type-safe, but assert no unexpected NULLs in NOT NULL booleans."""
    off = []
    for col in BOOL_COLS:
        n = q(con, f"SELECT COUNT(*) FROM observation WHERE {col} IS NULL")[0][0]
        if n:
            off.append((col, n))
    return mk("6.3_bool_clean", "types", "BLOCK", off,
              "is_total/is_preferred/is_escalated/is_derived are non-null booleans",
              "{n} boolean flag columns carry NULLs")


# =====================================================================
# 7. CONSTRAINT ENFORCEMENT (declared vs procedural)
# =====================================================================
def c7_declared_constraints(con):
    """Inventory declared constraints; flag load-bearing rules enforced ONLY
    procedurally (in validate.py) that DuckDB *could* declare. ADVISE."""
    cons = q(con, "SELECT table_name, constraint_type FROM duckdb_constraints()")
    by_type = {}
    for t, ct in cons:
        by_type.setdefault(ct, set()).add(t)
    # Things validated procedurally but not declarable as simple CHECK/FK in DuckDB:
    advisories = []
    # supersedes_id has no FK (documented DuckDB limitation: FK re-check blocks recompute)
    advisories.append(("observation.supersedes_id", "no FK (documented DuckDB limitation: "
                       "FK re-check on UPDATE blocks is_preferred recompute) — verified live by 2.2"))
    # observation_footnote.observation_id has no FK (same documented limitation)
    advisories.append(("observation_footnote.observation_id", "no FK (same documented limitation) "
                       "— verified live by 2.2"))
    # period_start<=period_end is not a declared CHECK (DuckDB supports row CHECK) -> could harden
    advisories.append(("observation period_start<=period_end", "enforced procedurally (5.4) — "
                       "DuckDB SUPPORTS a row-level CHECK(period_start<=period_end); candidate to declare"))
    advisories.append(("series_key never mixes period_type/basis/unit", "enforced by construction + "
                       "validate C6/C6b — not declarable as a table CHECK; keep procedural"))
    summary = (f"declared: {sum(len(v) for v in by_type.values())} constraints across "
               f"{len(by_type)} types ({', '.join(sorted(by_type))}). "
               f"{len(advisories)} load-bearing rules are procedural.")
    return Result("7.1_constraint_enforcement", "constraints", "ADVISE", "ADVISE",
                  summary, advisories,
                  note="2 procedural choices are real DuckDB FK limitations (documented & "
                       "verified live); the period_start<=period_end CHECK is a safe hardening candidate.")


# =====================================================================
# 8. SILENT-DROP & COLLISION MACHINERY (round-4)
# =====================================================================
def c8_quarantine_has_value(con):
    """Round-4 invariant: every staging_unmapped row records its value(s)/context.
    Data-row quarantines carry values=…; collision quarantines carry value=… context."""
    no_ctx = q(con, """SELECT id, dimension, raw_label FROM staging_unmapped
        WHERE context IS NULL OR TRIM(context)='' LIMIT 25""")
    data_no_val = q(con, """SELECT id, dimension, raw_label FROM staging_unmapped
        WHERE dimension <> 'SERIES_KEY_COLLISION'
          AND context NOT LIKE '%value%' LIMIT 25""")
    off = [("no_context", r) for r in no_ctx] + [("data_row_no_value", r) for r in data_no_val]
    return mk("8.1_quarantine_has_value", "silent-drop", "BLOCK", off,
              "every staging_unmapped row records its value(s) in context (nothing dropped blind)",
              "{n} quarantine rows lack a recorded value/context")


def c8_collision_no_silent_overwrite(con):
    """SERIES_KEY_COLLISION path is sound: same upsert_key + different value is
    quarantined-with-context, never silently overwritten. Two structural assertions:
    (a) no surviving observation pair shares upsert_key with different values (UNIQUE
    guarantees this — re-assert), (b) every collision quarantine records both incoming
    and existing value + the colliding series_key (full context)."""
    dup_val = q(con, """SELECT upsert_key FROM observation
        GROUP BY upsert_key HAVING COUNT(DISTINCT value)>1 LIMIT 25""")
    bad_ctx = q(con, """SELECT id, context FROM staging_unmapped
        WHERE dimension='SERIES_KEY_COLLISION'
          AND (context NOT LIKE '%value=%' OR context NOT LIKE '%series_key=%') LIMIT 25""")
    off = [("surviving_dup_value", r[0]) for r in dup_val] + \
          [("collision_ctx_incomplete", r[0]) for r in bad_ctx]
    return mk("8.2_collision_sound", "silent-drop", "BLOCK", off,
              "collision path quarantines-with-full-context; no surviving silent overwrite",
              "{n} collision-path soundness violations")


# =====================================================================
# 9. VIEWS & READ-PATH SOUNDNESS
# =====================================================================
def c9_views_resolve(con):
    off = []
    for v in ["v_series", "v_series_clean", "v_series_detail"]:
        try:
            con.execute(f"SELECT * FROM {v} LIMIT 1").fetchall()
        except Exception as e:
            off.append((v, str(e)[:120]))
    return mk("9.1_views_resolve", "views", "BLOCK", off,
              "all three views resolve (no broken dependencies)",
              "{n} views fail to resolve")


def c9_views_expose_qualifiers(con):
    """v_series / v_series_detail must expose the qualifier + cell-provenance columns
    (structural: column present + resolves). Value-correctness is the data audit's lane;
    meaning/labeling is Agent B's."""
    needed_vseries = {"period_type", "period_start", "period_end", "calorific_basis",
                      "data_status", "extraction_confidence", "is_escalated",
                      "source_id", "source_page", "source_ref"}
    off = []
    for v, needed in [("v_series", needed_vseries),
                      ("v_series_detail", needed_vseries)]:
        cols = {r[1] for r in q(con, f"PRAGMA table_info('{v}')")}
        missing = needed - cols
        for m in sorted(missing):
            off.append((v, "missing column", m))
    return mk("9.2_views_qualifiers", "views", "BLOCK", off,
              "v_series & v_series_detail expose all qualifier + cell-provenance columns",
              "{n} qualifier/provenance columns missing from a view")


def c9_clean_surface(con):
    """v_series_clean: 0 low-confidence, 0 escalated leak; qualifiers still reachable."""
    leaks = q(con, """SELECT COUNT(*) FILTER(WHERE extraction_confidence='low'),
        COUNT(*) FILTER(WHERE is_escalated=TRUE) FROM v_series_clean""")[0]
    off = []
    if leaks[0]:
        off.append(("low_confidence_leak", leaks[0]))
    if leaks[1]:
        off.append(("escalated_leak", leaks[1]))
    # qualifiers still present in clean view (it's SELECT * from v_series, so should be)
    cols = {r[1] for r in q(con, "PRAGMA table_info('v_series_clean')")}
    for needed in ["calorific_basis", "data_status", "extraction_confidence", "period_type"]:
        if needed not in cols:
            off.append(("clean_view_hides_qualifier", needed))
    return mk("9.3_clean_surface", "views", "BLOCK", off,
              "v_series_clean: 0 low-conf & 0 escalated leaks; qualifiers still reachable",
              "{n} clean-surface leaks or hidden qualifiers")


# =====================================================================
# 10. NORMALIZATION SANITY
# =====================================================================
def c10_redundant_redevance(con):
    """redevance_included is denormalized alongside redevance_toggle_id. Check it never
    DISAGREES with the lookup (an update anomaly). ADVISE — documented denorm."""
    rows = q(con, """SELECT o.observation_id, o.redevance_toggle_id, o.redevance_included,
        t.redevance_included AS lookup_val
        FROM observation o JOIN redevance_toggle t ON t.toggle_id=o.redevance_toggle_id
        WHERE o.redevance_included IS DISTINCT FROM t.redevance_included LIMIT 25""")
    return mk("10.1_redevance_consistency", "normalization", "ADVISE", rows,
              "denormalized redevance_included agrees with redevance_toggle lookup (no anomaly)",
              "{n} rows where redevance_included disagrees with its toggle lookup")


def c10_template_version_consistency(con):
    """observation.template_version is also on source. Flag rows that disagree with their
    source's template_version (denormalization update-anomaly probe). ADVISE."""
    rows = q(con, """SELECT o.source_id, o.template_version AS obs_tv, s.template_version AS src_tv,
        COUNT(*) FROM observation o JOIN source s ON s.source_id=o.source_id
        WHERE o.template_version IS DISTINCT FROM s.template_version
        GROUP BY 1,2,3 LIMIT 25""")
    return Result("10.2_template_version", "normalization", "ADVISE",
                  "ADVISE" if rows else "PASS",
                  ("observation.template_version always matches source.template_version"
                   if not rows else
                   f"{len(rows)} (source,obs_tv,src_tv) groups where observation.template_version "
                   "differs from source.template_version — expected when one source spans multiple "
                   "layout templates (per-edition re-anchoring); informational, not an anomaly."),
                  rows)


ALL_CHECKS = [
    c1_pk_observation, c1_upsert_unique, c1_upsert_definition, c1_serieskey_collapse,
    c1_serieskey_field_count,
    c2_obs_orphans, c2_satellite_orphans, c2_dead_dims,
    c3_hierarchy, c3_istotal_partition, c3_flow_partition, c3_relation_type_presence,
    c4_detail_no_totals, c4_detail_only_preferred,
    c5_enum_domains, c5_no_mixed_period_basis, c5_pcipcs_ytd_twins_distinct, c5_period_consistency,
    c6_mandatory_nonnull, c6_value_null_documented, c6_bool_clean,
    c7_declared_constraints,
    c8_quarantine_has_value, c8_collision_no_silent_overwrite,
    c9_views_resolve, c9_views_expose_qualifiers, c9_clean_surface,
    c10_redundant_redevance, c10_template_version_consistency,
]


def run(db_path=DB_DEFAULT):
    con = duckdb.connect(db_path, read_only=True)
    results = []
    for c in ALL_CHECKS:
        try:
            results.append(c(con))
        except Exception as e:
            results.append(Result(c.__name__, "ERROR", "BLOCK", "ERROR",
                                  f"check raised: {e}", [str(e)]))
    con.close()
    return results


def main():
    db = DB_DEFAULT
    as_json = "--json" in sys.argv
    if "--db" in sys.argv:
        db = sys.argv[sys.argv.index("--db") + 1]
    results = run(db)
    if as_json:
        print(json.dumps([r.as_dict() for r in results], indent=2, default=str))
        return
    print(f"\n=== qa_structural.py on {db} ===\n")
    for r in results:
        tag = r.status
        line = f"[{tag:6}] {r.cid:32} ({r.severity:8}) {r.summary}"
        print(line)
        if r.rows and r.status != "PASS":
            for row in r.rows[:6]:
                print(f"           -> {row}")
        if r.note:
            print(f"           · note: {r.note}")
    # summary
    blocks = [r for r in results if r.status == "BLOCK" or r.status == "ERROR"]
    advises = [r for r in results if r.status == "ADVISE"]
    passes = [r for r in results if r.status == "PASS"]
    print("\n--- SUMMARY ---")
    print(f"PASS: {len(passes)}   ADVISE: {len(advises)}   BLOCK/ERROR: {len(blocks)}")
    verdict = "GO (structural)" if not blocks else "FIX-FIRST (structural)"
    print(f"VERDICT: {verdict}")
    if blocks:
        for r in blocks:
            print(f"   BLOCK -> {r.cid}: {r.summary}")
    sys.exit(1 if blocks else 0)


if __name__ == "__main__":
    main()
