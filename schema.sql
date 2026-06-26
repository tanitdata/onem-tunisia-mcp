-- =====================================================================
-- schema.sql — ONEM Tunisia Energy time-series store
-- Implements 06_proposed_schema.sql AS AMENDED BY 11_human_rulings.md.
-- Target: DuckDB (OQ-M5). SQLite-portable: DOUBLE->REAL, BOOLEAN->INTEGER 0/1,
--   drop the views' nothing-special; CHECK + FK supported in both.
--
-- Long-format ("tidy") star schema: one fact table (observation) + conformed
-- dimensions. BASE indicator + separate dimension FKs (OQ-M1), NOT
-- fully-qualified indicator names.
--
-- Ruling-driven additions vs 06:
--   * basis_confidence on observation           (OQ-R1/U3: Bilan gas PCS = inferred)
--   * scope / technology / regime / geography_scope attributes (OQ-R1/R2/R6/D2)
--   * product & flow hierarchy: parent_id + is_total + aggregation_level (OQ-M2)
--   * producer = full dimension                  (OQ-D1)
--   * sector_crosswalk table                      (OQ-S1)
--   * redevance_toggle lookup (enum, not bare bool) (OQ-M4)
--   * re_project event/status table out of the fact table (OQ-M3)
--   * reference_docs table for the 17 "Other" docs (no series)
--   * template_version on every observation       (Phase C provenance)
--   * staging_unmapped quarantine                  (08 ingestion design)
-- =====================================================================

-- (DuckDB enforces FKs by default; SQLite needs `PRAGMA foreign_keys=ON;`.)

-- =====================================================================
-- REFERENCE / CONTROLLED-VOCABULARY DIMENSIONS
-- =====================================================================

CREATE TABLE source (
    source_id        TEXT PRIMARY KEY,            -- 'bilan_2024_v2','conjoncture_2026_04','memento_2024'
    report_title     TEXT NOT NULL,
    report_type      TEXT NOT NULL CHECK (report_type IN
                       ('bilan','memento','conjoncture','rapport','covid_bulletin')),
    publisher        TEXT NOT NULL DEFAULT 'ONEM',
    language         TEXT,                         -- 'fr','ar','multi','en'
    publication_date DATE,
    version          TEXT,                         -- 'v1','v2','VF-2024', NULL
    period_covered   TEXT,                         -- '2024','à fin avril 2026'
    cadence          TEXT CHECK (cadence IN ('annual','monthly')),
    cutoff_month     INTEGER,                      -- Conjoncture YTD cutoff read FROM the report (4=avril)
    template_version TEXT,                         -- layout template tag (e.g. 'bilan-matrix-v2024')
    file_id          TEXT,                         -- manifest sha-prefixed id, for provenance
    sha256           TEXT,
    local_path       TEXT,
    supersedes_source TEXT REFERENCES source(source_id),  -- manifest supersedes link (e.g. v1)
    is_canonical_lang BOOLEAN NOT NULL DEFAULT TRUE, -- FALSE for AR translations (dedup)
    notes            TEXT
);

CREATE TABLE unit (
    unit_id          TEXT PRIMARY KEY,
    label_fr         TEXT,
    label_en         TEXT,
    quantity_kind    TEXT NOT NULL CHECK (quantity_kind IN
                       ('mass','energy','energy_elec','power','flow_rate','flow_rate_gas',
                        'price','fx_rate','monetary','intensity','ratio','count','length')),
    stock_or_flow    TEXT CHECK (stock_or_flow IN ('stock','flow','rate','price','ratio')),
    notes            TEXT
);

CREATE TABLE conversion_factor (
    from_unit        TEXT NOT NULL,
    to_unit          TEXT NOT NULL,
    factor           DOUBLE NOT NULL,
    calorific_basis  TEXT CHECK (calorific_basis IN ('PCI','PCS','NA','PCI->PCS')) DEFAULT 'NA',
    scope            TEXT,                         -- 'natural gas','electricity','volume','crude',...
    source_id        TEXT,
    note             TEXT,
    PRIMARY KEY (from_unit, to_unit, scope)
);

-- ---- flow hierarchy (balance lines). parent_flow_id + is_total + aggregation_level (OQ-M2) ----
CREATE TABLE flow (
    flow_id          TEXT PRIMARY KEY,
    label_fr         TEXT, label_en TEXT, label_ar TEXT,
    parent_flow_id   TEXT REFERENCES flow(flow_id),
    is_total         BOOLEAN NOT NULL DEFAULT FALSE,  -- parent/aggregate line (don't sum with children)
    aggregation_level INTEGER NOT NULL DEFAULT 0,     -- 0=detail, higher=more aggregate
    aliases          TEXT,
    definition       TEXT
);

-- ---- product hierarchy. parent_product_id + is_total + aggregation_level (OQ-M2) ----
CREATE TABLE product (
    product_id        TEXT PRIMARY KEY,
    label_fr          TEXT, label_en TEXT, label_ar TEXT,
    category          TEXT,                        -- fossil_primary, pet_product, renewable, carrier, aggregate
    parent_product_id TEXT REFERENCES product(product_id),
    is_total          BOOLEAN NOT NULL DEFAULT FALSE, -- 'Total Produits Pétroliers','Total tous produits',...
    aggregation_level INTEGER NOT NULL DEFAULT 0,
    aliases           TEXT,
    definition        TEXT
);

CREATE TABLE sector (
    sector_id        TEXT PRIMARY KEY,
    label_fr         TEXT, label_en TEXT,
    parent_sector_id TEXT REFERENCES sector(sector_id),
    is_total         BOOLEAN NOT NULL DEFAULT FALSE,
    aliases          TEXT,
    definition       TEXT
);

-- sector crosswalk (OQ-S1): map each source taxonomy code -> canonical sector_id.
CREATE TABLE sector_crosswalk (
    source_taxonomy  TEXT NOT NULL,               -- 'bilan_final','conjoncture_htmt_pie'
    source_label     TEXT NOT NULL,               -- 'Pompages & ser.','Tourisme',...
    sector_id        TEXT NOT NULL REFERENCES sector(sector_id),
    note             TEXT,
    PRIMARY KEY (source_taxonomy, source_label)
);

CREATE TABLE region (
    region_id        TEXT PRIMARY KEY,
    label            TEXT,
    region_type      TEXT NOT NULL CHECK (region_type IN ('gouvernorat','zone','steg_district')),
    aliases          TEXT,
    composition      TEXT,
    definition       TEXT
);

CREATE TABLE field (
    field_id         TEXT PRIMARY KEY,
    label            TEXT,
    produces         TEXT CHECK (produces IN ('oil','gas','oil_gas')),
    is_aggregate     BOOLEAN NOT NULL DEFAULT FALSE,
    aliases          TEXT,
    notes            TEXT
);

CREATE TABLE field_membership (
    aggregate_field_id TEXT REFERENCES field(field_id),
    member_field_id    TEXT REFERENCES field(field_id),
    source_id          TEXT REFERENCES source(source_id),  -- membership differs by report (OQ-F1)
    PRIMARY KEY (aggregate_field_id, member_field_id, source_id)
);

CREATE TABLE level (
    level_id         TEXT PRIMARY KEY,
    label_fr         TEXT, label_en TEXT,
    domain           TEXT CHECK (domain IN ('electricity','gas')),
    aliases          TEXT
);

CREATE TABLE producer (                            -- full dimension (OQ-D1)
    producer_id      TEXT PRIMARY KEY,
    label            TEXT,
    aliases          TEXT,
    notes            TEXT
);

-- redevance toggle as a small lookup/enum (OQ-M4) rather than a bare boolean.
CREATE TABLE redevance_toggle (
    toggle_id        TEXT PRIMARY KEY,             -- 'incl','excl', future variants
    label            TEXT NOT NULL,
    redevance_included BOOLEAN,                    -- convenience flag (NULL = irrelevant)
    note             TEXT
);

-- ---------- the indicator (BASE metric) ----------
CREATE TABLE indicator (
    indicator_id     TEXT PRIMARY KEY,
    canonical_name   TEXT NOT NULL,
    label_fr         TEXT, label_en TEXT, label_ar TEXT,
    definition       TEXT,
    category         TEXT,                         -- production/trade/consumption/price/balance/capacity/exploration
    default_unit_id  TEXT REFERENCES unit(unit_id),
    default_basis    TEXT CHECK (default_basis IN ('PCI','PCS','NA')) DEFAULT 'NA',
    applicable_dims  TEXT
);

CREATE TABLE footnote (
    footnote_id      TEXT PRIMARY KEY,
    footnote_type    TEXT CHECK (footnote_type IN
                       ('definition','scope','provenance_status','toggle',
                        'spec_change','aggregate_membership','units_basis')),
    text             TEXT NOT NULL,
    source_id        TEXT
);

-- =====================================================================
-- FACT TABLE
-- =====================================================================
CREATE TABLE observation (
    observation_id   BIGINT PRIMARY KEY,

    indicator_id     TEXT NOT NULL REFERENCES indicator(indicator_id),
    value            DOUBLE,                       -- NULL allowed ('-' -> NULL)
    value_raw        TEXT,                         -- original token preserved
    unit_id          TEXT NOT NULL REFERENCES unit(unit_id),
    calorific_basis  TEXT NOT NULL DEFAULT 'NA' CHECK (calorific_basis IN ('PCI','PCS','NA')),
    basis_confidence TEXT NOT NULL DEFAULT 'stated'
                       CHECK (basis_confidence IN ('stated','inferred','na')),  -- OQ-R1/U3

    -- OQ-M2: total-ness is a property of THE ROW (this cell), not of a dimension. The
    -- same dimension (e.g. flow.demand) is a total in the "DEMANDE" row but a leaf in
    -- "Haute pression". Loaders set this per row; v_series_detail filters on it so
    -- get_series never sums totals together with their components.
    is_total         BOOLEAN NOT NULL DEFAULT FALSE,
    -- explicit aggregation role (set by loaders; the consumer-facing semantics of
    -- is_total). 'leaf' = safe to sum within a partition; 'grand_total' = THE group
    -- total the leaves sum to; 'subtotal' = an intermediate aggregate (STEG, Gasoil);
    -- 'alternative_breakdown' = a second partition of the same total (gas-demand HP/MBP).
    -- The C11/C12 gates key on aggregation_role='grand_total' so a subtotal can never
    -- masquerade as the group total.
    aggregation_role TEXT NOT NULL DEFAULT 'leaf'
                       CHECK (aggregation_role IN
                         ('leaf','grand_total','subtotal','alternative_breakdown')),

    -- temporal semantics (Trap 1)
    period_type      TEXT NOT NULL CHECK (period_type IN
                       ('annual','ytd_cumulative','monthly','point_in_time')),
    period_start     DATE NOT NULL,
    period_end       DATE NOT NULL,
    ytd_cutoff_month INTEGER,
    ref_year         INTEGER,

    -- data quality / provenance
    data_status      TEXT NOT NULL CHECK (data_status IN
                       ('final','provisional','estimated','revised')),
    source_id        TEXT NOT NULL REFERENCES source(source_id),
    source_page      TEXT,
    source_ref       TEXT,                         -- 'C-T1','B-T1', figure id, etc.
    source_type      TEXT NOT NULL DEFAULT 'table'
                       CHECK (source_type IN ('table','chart_label')),  -- OQ-C2
    template_version TEXT,                          -- layout template that parsed this cell
    -- extraction methodology provenance (see Extraction Methodology addendum):
    extraction_method TEXT NOT NULL DEFAULT 'text_geometry'
                       CHECK (extraction_method IN
                         ('text_geometry','coordinate_map','ocr','chart_label')),
    extraction_confidence TEXT NOT NULL DEFAULT 'normal'
                       CHECK (extraction_confidence IN ('normal','low')),
    source_cell      TEXT,                           -- cell-level provenance: 'row=…|col=…' (re-derivable)

    -- dimensions (nullable; an observation uses the subset that applies)
    flow_id          TEXT REFERENCES flow(flow_id),
    product_id       TEXT REFERENCES product(product_id),
    sector_id        TEXT REFERENCES sector(sector_id),
    region_id        TEXT REFERENCES region(region_id),
    field_id         TEXT REFERENCES field(field_id),
    level_id         TEXT REFERENCES level(level_id),
    producer_id      TEXT REFERENCES producer(producer_id),

    -- attributes (OQ-D2, OQ-R1, OQ-R2, OQ-R6)
    technology       TEXT,                          -- CC/TG/TV/ER (elec)
    regime           TEXT,                          -- concession/autorisation/autoproduction/STEG (RE)
    scope            TEXT,                          -- 'commercial_dry'/'primary_broad'/'incl_gpl_condensat'/...
    geography_scope  TEXT,                          -- 'local'/'incl_exports' (elec sales OQ-R6)

    -- redevance toggle (Trap 3) as enum FK + convenience boolean
    redevance_toggle_id TEXT REFERENCES redevance_toggle(toggle_id),
    redevance_included  BOOLEAN,

    -- derived flag (Trap 9)
    is_derived       BOOLEAN NOT NULL DEFAULT FALSE,
    derivation_note  TEXT,

    -- ingestion / supersession bookkeeping
    series_key       TEXT NOT NULL,
    upsert_key       TEXT NOT NULL,
    is_preferred     BOOLEAN NOT NULL DEFAULT TRUE,
    -- supersedes_id is a plain column (NOT a self-FK): DuckDB enforces FK checks on
    -- ANY update to a referenced table, which would block the is_preferred recompute.
    supersedes_id    BIGINT,
    confidence       TEXT NOT NULL DEFAULT 'normal' CHECK (confidence IN ('normal','low')),
    is_escalated     BOOLEAN NOT NULL DEFAULT FALSE,  -- OQ-R1 / OQ-F2 isolated items
    escalation_ref   TEXT,                            -- 'OQ-R1','OQ-F2'
    ingested_at      TIMESTAMP,
    UNIQUE (upsert_key),
    -- A-7.1: declare the temporal invariant (was procedural).
    CHECK (period_start <= period_end)
);

-- observation_id kept as a plain column (no FK): a FK here makes DuckDB re-check on
-- every UPDATE to observation (e.g. the is_preferred recompute), which it forbids.
CREATE TABLE observation_footnote (
    observation_id   BIGINT,
    footnote_id      TEXT REFERENCES footnote(footnote_id),
    PRIMARY KEY (observation_id, footnote_id)
);

-- RE-project status/milestones (OQ-M3): event/status data, NOT time-series numbers.
CREATE TABLE re_project (
    project_id       TEXT PRIMARY KEY,
    project_name     TEXT,
    technology       TEXT,                          -- pv / wind
    regime           TEXT,                          -- concession/autorisation/autoproduction/STEG
    capacity_mw      DOUBLE,
    status           TEXT,                          -- en service / en cours / etc.
    region_id        TEXT REFERENCES region(region_id),
    producer_id      TEXT REFERENCES producer(producer_id),
    source_id        TEXT REFERENCES source(source_id),
    source_ref       TEXT,
    as_of_date       DATE,
    notes            TEXT
);

-- Scope / attribute glossary (FIX 5): defines the qualifier tokens that appear on
-- observations (scope, geography_scope, redevance toggle, calorific_basis), each with
-- its meaning and the "do not sum/equate across values" rule. The MCP references this so
-- a blind LLM understands commercial_dry vs primary_broad, local vs incl_exports, etc.
CREATE TABLE scope_glossary (
    attribute   TEXT NOT NULL,        -- 'scope','geography_scope','calorific_basis','redevance_toggle'
    token       TEXT NOT NULL,        -- 'commercial_dry','primary_broad','local',...
    definition  TEXT NOT NULL,
    never_sum_with TEXT,              -- tokens this must NOT be summed/equated with
    PRIMARY KEY (attribute, token)
);

-- The 17 "Other" docs (studies/guides/strategy) — catalog only, NO time series.
CREATE TABLE reference_docs (
    doc_id           TEXT PRIMARY KEY,
    title            TEXT,
    doc_type         TEXT,                          -- guide / study / strategy / methodology / esia
    doc_date         TEXT,
    language         TEXT,
    local_path       TEXT,
    source_url       TEXT,
    notes            TEXT
);

-- Unknown source labels are quarantined, never guessed (08 ingestion design).
CREATE TABLE staging_unmapped (
    id               BIGINT PRIMARY KEY,
    source_id        TEXT,
    source_ref       TEXT,
    dimension        TEXT,                          -- which dim failed to map
    raw_label        TEXT,
    context          TEXT,
    ingested_at      TIMESTAMP
);

-- Cross-edition reconciliation log (Phase D): disagreements surfaced, NOT overwritten.
CREATE TABLE reconciliation_log (
    id               BIGINT PRIMARY KEY,
    series_key       TEXT,
    ref_year         INTEGER,
    period_type      TEXT,
    calorific_basis  TEXT,
    metric           TEXT,
    values_json      TEXT,                          -- {source_id: value, ...}
    resolution       TEXT,                          -- precedence winner / 'ESCALATED'
    note             TEXT
);

-- ---------- indexes ----------
CREATE INDEX ix_obs_series   ON observation(series_key);
CREATE INDEX ix_obs_period   ON observation(period_type, period_start, period_end);
CREATE INDEX ix_obs_ind      ON observation(indicator_id);
CREATE INDEX ix_obs_dims     ON observation(product_id, flow_id, field_id, region_id, level_id);
CREATE INDEX ix_obs_pref     ON observation(is_preferred);

-- ---------- convenience views ----------
-- only preferred observations, with labels:
CREATE VIEW v_series AS
SELECT o.observation_id, o.series_key, i.canonical_name AS indicator,
       o.value, o.unit_id, o.calorific_basis, o.basis_confidence, o.period_type,
       o.period_start, o.period_end, o.ref_year, o.data_status,
       o.flow_id, o.product_id, o.sector_id, o.region_id, o.field_id, o.level_id, o.producer_id,
       o.technology, o.regime, o.scope, o.geography_scope,
       o.redevance_included, o.is_derived, o.is_total, o.aggregation_role,
       o.source_type, o.confidence,
       o.extraction_method, o.extraction_confidence,
       o.is_escalated, s.report_type, s.version, o.template_version,
       -- cell-level provenance so the MCP can cite "Conjoncture avril 2026, p5, C-T1":
       o.source_id, o.source_page, o.source_ref, o.source_cell
FROM observation o
JOIN indicator i ON i.indicator_id = o.indicator_id
JOIN source s    ON s.source_id    = o.source_id
WHERE o.is_preferred = TRUE;

-- The MCP's DEFAULT surface: preferred + trustworthy. Excludes low-confidence
-- extractions and the ESCALATED (OQ-R1/F2) series. Use v_series for "everything,
-- with flags".
CREATE VIEW v_series_clean AS
SELECT * FROM v_series
WHERE extraction_confidence <> 'low'
  AND is_escalated = FALSE;

-- DETAIL-ONLY view for safe summation (OQ-M2): leaves only, so the MCP never
-- double-counts totals + components. Total-ness is the ROW flag observation.is_total
-- (set per row by the loaders), NOT a dimension property — the same flow.demand is a
-- total in one row and a leaf in another.
CREATE VIEW v_series_detail AS
SELECT *
FROM observation
WHERE is_preferred = TRUE
  AND is_total = FALSE;

-- Footnote text per observation (so the MCP can surface the real caveat sentences, not
-- footnote ids). One row per (observation, footnote) with the resolved text + type.
CREATE VIEW v_observation_footnotes AS
SELECT of.observation_id, f.footnote_id, f.footnote_type, f.text
FROM observation_footnote of
JOIN footnote f ON f.footnote_id = of.footnote_id;
