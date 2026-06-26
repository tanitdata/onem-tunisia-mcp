"""onem_store.py — read-only data/logic layer for the ONEM energy MCP server.

This module is the *only* place that touches `energy.duckdb`. It is deliberately
separate from the MCP wrapper (`mcp_server.py`) so the logic — qualifier
envelopes, out-of-scope detection, aggregation safety, the comparison guardrail —
can be unit-tested without an MCP runtime.

Read `CLAUDE.md` first; its conventions override anything here that conflicts.
The non-negotiables this layer enforces:
  #1 query the clean views, never raw tables (DEFAULT v_series_clean / v_series_detail);
  #3 every returned observation carries its full qualifier set + provenance;
  #4 "not in scope" != "no data" (deferred families get an explicit signal);
  #5 no double-count (aggregation respects aggregation_role; leaves + grand_total);
  #6 the DB is opened read-only;
  #3(twins) PCI/PCS, annual/ytd, local/incl_exports, commercial_dry/primary_broad,
     crude incl/excl are kept distinguishable and never silently mixed.
"""

from __future__ import annotations

import csv
import json
import os
import threading
from typing import Any, Optional

import duckdb

# --------------------------------------------------------------------------
# Paths / connection (read-only, hard constraint #6)
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ONEM_DB", os.path.join(HERE, "energy.duckdb"))
CATALOG_CSV = os.path.join(HERE, "series_catalog.csv")
COVERAGE_GAPS = os.path.join(HERE, "coverage_gaps.md")

_conn_lock = threading.Lock()
_conn: Optional[duckdb.DuckDBPyConnection] = None


def get_conn() -> duckdb.DuckDBPyConnection:
    """Return a process-wide read-only DuckDB connection.

    Opened with read_only=True so the server can NEVER acquire a write lock
    (a stale writer holding the file mid-write has already caused an incident —
    constraint #6). A read-only handle also lets many readers share the file.
    """
    global _conn
    if _conn is None:
        with _conn_lock:
            if _conn is None:
                _conn = duckdb.connect(DB_PATH, read_only=True)
    return _conn


def _q(sql: str, params: Optional[list] = None) -> list[dict]:
    """Run a query and return list-of-dict rows (thread-safe via a cursor)."""
    con = get_conn()
    with _conn_lock:
        cur = con.cursor()
        try:
            cur.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            cur.close()


# --------------------------------------------------------------------------
# Static reference loaded once: catalog + deferred-family map
# --------------------------------------------------------------------------

# Indicators that are DEFINED in the schema but have ZERO ingested series.
# Per CLAUDE.md #5 these are "out of scope / not ingested", NEVER "no data".
# Populated at import from the DB (indicator table vs v_series) so it can never
# drift from reality. Keyword aliases route natural-language queries here.
_DEFERRED_KEYWORDS = {
    "brent_price": ["brent", "prix du baril", "oil price"],
    "crude_price": ["crude price", "prix petrole", "prix brut", "import price crude"],
    "gas_price": ["gas price", "prix gaz", "prix du gaz"],
    "gas_import_price": ["algerian gas", "prix import gaz", "imported gas price"],
    "pp_price": ["product price", "prix produits", "fuel price", "gasoil price", "petrol price"],
    "electricity_price": ["electricity price", "prix electricite", "kwh price", "tariff"],
    "fx_rate": ["exchange rate", "taux de change", "dt/$", "dinar dollar", "forex"],
    "trade_value": ["trade value", "valeur des echanges", "import value", "export value", "mdt"],
    "trade_quantity": ["trade quantity", "quantite des echanges", "transit", "transmed",
                       "throughput", "pipeline volume", "gas transit", "transit volume",
                       "transit throughput", "gazoduc", "volume transite", "debit gazoduc",
                       "algeria to italy", "algerie italie"],
    "pp_import": ["product import", "import produits petroliers", "imported products"],
    "pp_production": ["product production", "production produits petroliers", "refinery output"],
    "refining_kpi": ["refining", "raffinage", "stir", "refinery"],
    "exploration_kpi": ["exploration", "drilling", "forages", "permis", "decouvertes"],
    "re_capacity": ["renewable capacity", "capacite renouvelable", "installed capacity", "mw installed"],
    "peak_power": ["peak power", "pointe", "peak demand", "puissance de pointe"],
    "electricity_supply": ["electricity supply balance", "bilan electrique", "elec supply"],
    "specific_consumption": ["specific consumption", "consommation specifique", "energy intensity"],
    "gas_sales": ["gas sales", "ventes de gaz", "gas sold"],
}

# Generic, single-word family terms a real user actually types ("prices",
# "refining", "imports") that don't appear in the precise phrase keywords above.
# Each maps to a representative deferred indicator so list_series("prices") /
# search_series("refining") fire the out-of-scope signal (B-2) instead of a bare
# n:0 that reads as "no data exists" (CLAUDE.md #5).
_DEFERRED_FAMILY_TERMS = {
    "price": "brent_price", "prices": "brent_price", "pricing": "brent_price",
    "prix": "brent_price", "tarif": "electricity_price", "tariff": "electricity_price",
    "refining": "refining_kpi", "refinery": "refining_kpi", "raffinage": "refining_kpi",
    "exploration": "exploration_kpi", "drilling": "exploration_kpi",
    "import": "pp_import", "imports": "pp_import", "importation": "pp_import",
    "capacity": "re_capacity", "capacite": "re_capacity",
    "peak": "peak_power", "pointe": "peak_power",
    "trade": "trade_value", "echanges": "trade_value",
    "transit": "trade_quantity", "transmed": "trade_quantity",
    "throughput": "trade_quantity", "gazoduc": "trade_quantity", "pipeline": "trade_quantity",
    "intensity": "specific_consumption",
    "forex": "fx_rate", "fx": "fx_rate",
}


# S-3 (retrieval robustness): map high-value Arabic / Tunisian-dialect energy terms to
# the canonical FR/EN tokens the catalog is indexed on, so a dialect query resolves to
# the same series an FR/EN query does. This is SEARCH robustness, NOT AR ingestion
# (CLAUDE.md #8: AR is registered non-canonical with 0 observations — we never ingest AR
# cells; we only help the query reach the canonical series). Keyed on substrings so we
# don't depend on Arabic word-boundary tokenization.
_DIALECT_SYNONYMS = {
    # royalty / forfait fiscal (the specimen's Variant-A term)
    "ريع": "redevance royalty",
    "جباي": "redevance forfait fiscal royalty",   # جبائي / جباية (fiscal)
    "إتاوة": "redevance royalty",
    "اتاوة": "redevance royalty",
    # gas / Algerian
    "غاز": "gaz gas natural_gas",
    "الغاز": "gaz gas natural_gas",
    "جزائر": "algerien algerian algeria",          # الجزائري / الجزائر
    # a few other high-value carriers
    "كهرباء": "electricite electricity",
    "نفط": "petrole crude oil",
    "بترول": "petrole crude oil",
}


def _expand_dialect(query: str) -> str:
    """Append canonical FR/EN tokens for any recognized dialect substring, so the
    scorer (which indexes FR/EN labels) can reach the canonical series. Additive —
    the original query is preserved."""
    extra = []
    for dialect, canon in _DIALECT_SYNONYMS.items():
        if dialect in query:
            extra.append(canon)
    return (query + " " + " ".join(extra)) if extra else query


def _load_catalog() -> list[dict]:
    with open(CATALOG_CSV, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


CATALOG: list[dict] = _load_catalog()
CATALOG_BY_ID: dict[str, dict] = {r["series_id"]: r for r in CATALOG}


def _build_label_index() -> dict[str, str]:
    """Map every dimension/indicator id to a searchable blob of its
    label_fr + label_en + aliases, so a free-text query in French or English
    ('Essence Sans Plomb', 'crude oil production') reaches series the catalog
    stores only by id ('prod.gasoline_ssp', 'crude_production')."""
    idx: dict[str, str] = {}

    def add(_id, *texts):
        if _id:
            idx[_id] = " ".join(t for t in texts if t)

    for tbl, idc, cols in [
        ("product", "product_id", "label_fr, label_en, aliases"),
        ("flow", "flow_id", "label_fr, label_en, aliases"),
        ("sector", "sector_id", "label_fr, label_en, aliases"),
        ("region", "region_id", "label, aliases"),
        ("field", "field_id", "label, aliases"),
        ("level", "level_id", "label_fr, label_en, aliases"),
        ("producer", "producer_id", "label, aliases"),
        ("indicator", "indicator_id", "canonical_name, label_en, label_ar"),
    ]:
        for r in _q(f"SELECT {idc} AS id, {cols} FROM {tbl}"):
            add(r["id"], *[str(v) for k, v in r.items() if k != "id" and v])
    return idx


_LABEL_INDEX: dict[str, str] = _build_label_index()


def _build_display_labels() -> dict[str, dict]:
    """Map every dimension/indicator id to its PRIMARY human-readable labels
    {fr, en} for DISPLAY (BLOCK B-3). Distinct from _LABEL_INDEX (which blends
    fr+en+aliases for SEARCH recall): here we keep one clean FR label and one EN
    label so search/list output shows 'Essence Sans Plomb' / 'Low voltage', never
    raw ids like 'prod.gasoline_ssp' / 'lvl.bt'."""
    labels: dict[str, dict] = {}
    for tbl, idc, fr, en in [
        ("product", "product_id", "label_fr", "label_en"),
        ("flow", "flow_id", "label_fr", "label_en"),
        ("sector", "sector_id", "label_fr", "label_en"),
        ("region", "region_id", "label", "label"),
        ("field", "field_id", "label", "label"),
        ("level", "level_id", "label_fr", "label_en"),
        ("producer", "producer_id", "label", "label"),
        ("indicator", "indicator_id", "canonical_name", "label_en"),
    ]:
        for r in _q(f"SELECT {idc} AS id, {fr} AS fr, {en} AS en FROM {tbl}"):
            if r["id"]:
                labels[r["id"]] = {"fr": r["fr"], "en": r["en"]}
    return labels


_DISPLAY_LABELS: dict[str, dict] = _build_display_labels()


def _label(dim_id: Optional[str], lang: str = "fr") -> Optional[str]:
    """Resolve a dimension/indicator id to its primary FR (or EN) label; falls
    back to the other language, then to the raw id only if no label exists."""
    if not dim_id:
        return None
    rec = _DISPLAY_LABELS.get(dim_id)
    if not rec:
        return dim_id
    return rec.get(lang) or rec.get("en") or rec.get("fr") or dim_id


# qualifier tokens (scope/basis/geo/period) that are not dimension ids but should
# read cleanly in a labelled name.
_QUALIFIER_LABELS = {
    "PCI": "PCI", "PCS": "PCS",
    "commercial_dry": "commercial dry gas", "primary_broad": "primary (broad) gas",
    "incl_gpl_condensat": "incl. GPL+condensat", "excl_gpl_condensat": "excl. GPL+condensat",
    "local": "local", "incl_exports": "incl. exports", "exports_only": "exports only",
    "annual": "annual", "ytd_cumulative": "year-to-date", "monthly": "monthly",
}


def _labelled_dimensions(row: dict) -> dict:
    """Return a series' dimensions as {dim: {id, label_fr, label_en}} so the
    consumer sees human labels AND can still cite the stable id."""
    out = {}
    for k in ("flow", "product", "sector", "region", "field", "level",
              "producer", "technology"):
        v = row.get(k)
        if v:
            rec = _DISPLAY_LABELS.get(v)
            out[k] = {"id": v,
                      "label_fr": (rec or {}).get("fr"),
                      "label_en": (rec or {}).get("en")} if rec else {"id": v}
    return out


def _labelled_name(row: dict) -> str:
    """A human-readable display name built from RESOLVED labels (B-3), e.g.
    'Production de gaz naturel — Miskar, commercial dry gas, PCI (ktep-pci), annual'
    instead of the id-laden '[flow=primary_production, product=natural_gas, …]'."""
    iid = row["series_id"].split("|")[0]
    head = _label(iid, "fr") or row.get("display_name", "")
    parts = []
    # most-specific dimensions first
    for k in ("field", "product", "level", "sector", "region", "producer", "technology"):
        lbl = _label(row.get(k)) if row.get(k) else None
        if lbl:
            parts.append(lbl)
    for q in (row.get("scope"), row.get("geography_scope")):
        if q:
            parts.append(_QUALIFIER_LABELS.get(q, q))
    basis = row.get("calorific_basis")
    if basis and basis != "NA":
        parts.append(basis)
    if row.get("unit"):
        parts.append(f"({row['unit']})")
    pt = row.get("period_type")
    if pt:
        parts.append(_QUALIFIER_LABELS.get(pt, pt))
    return head + (" — " + ", ".join(parts) if parts else "")


def _catalog_tiers(row: dict) -> tuple[str, str, str, str]:
    """Four weighted text tiers for a series, built from RESOLVED LABELS ONLY
    (never the raw series_id or display_name — those embed dimension ids like
    'primary_production' whose substring 'production' would wrongly match a
    Production-de-… query against a Bilan-primaire series). Lowercased.

      t1 indicator name/labels  — the WHAT (highest signal)
      t2 product/field/technology labels — the WHICH
      t3 sector/region/level/producer labels + scope/geo/basis tokens
      t4 flow label + definition + families + period_type — weak/noisy context
    """
    iid = row["series_id"].split("|")[0]
    t1 = _LABEL_INDEX.get(iid, "")
    t2 = " ".join([
        _LABEL_INDEX.get(row.get("product") or "", ""),
        _LABEL_INDEX.get(row.get("field") or "", ""),
        str(row.get("technology") or ""),
    ])
    t3 = " ".join([
        _LABEL_INDEX.get(row.get("sector") or "", ""),
        _LABEL_INDEX.get(row.get("region") or "", ""),
        _LABEL_INDEX.get(row.get("level") or "", ""),
        _LABEL_INDEX.get(row.get("producer") or "", ""),
        str(row.get("scope") or ""), str(row.get("geography_scope") or ""),
        str(row.get("calorific_basis") or ""),
    ])
    t4 = " ".join([
        _LABEL_INDEX.get(row.get("flow") or "", ""),
        str(row.get("definition", "")), str(row.get("families", "")),
        str(row.get("period_type", "")),
    ])
    return t1.lower(), t2.lower(), t3.lower(), t4.lower()


# Precompute tiers once (593 series) for fast scoring.
_TIERS: dict[str, tuple[str, str, str, str]] = {r["series_id"]: _catalog_tiers(r) for r in CATALOG}


def _load_indicators() -> dict[str, dict]:
    """All 31 indicators with category + whether any series were ingested."""
    rows = _q(
        """
        SELECT i.indicator_id, i.canonical_name, i.label_en, i.definition,
               i.category, i.default_unit_id, i.default_basis,
               (SELECT count(DISTINCT series_key) FROM v_series v
                  WHERE v.indicator = i.canonical_name) AS n_series
        FROM indicator i
        ORDER BY i.canonical_name
        """
    )
    return {r["indicator_id"]: r for r in rows}


INDICATORS: dict[str, dict] = _load_indicators()
# canonical_name -> indicator_id (v_series exposes the French canonical_name)
_NAME_TO_IID = {v["canonical_name"]: k for k, v in INDICATORS.items()}
DEFERRED_INDICATORS = {k for k, v in INDICATORS.items() if v["n_series"] == 0}


# --------------------------------------------------------------------------
# Qualifier envelope (hard constraint #3) — no tool returns a bare number
# --------------------------------------------------------------------------

# These are the v_series columns we surface. v_series_clean = precedence-winning,
# non-low-confidence, non-escalated (the DEFAULT trustworthy surface).
_VALUE_SELECT = """
    series_key, indicator, value, unit_id, calorific_basis, basis_confidence,
    period_type, period_start, period_end, ref_year, data_status,
    flow_id, product_id, sector_id, region_id, field_id, level_id, producer_id,
    technology, regime, scope, geography_scope, redevance_included,
    is_derived, is_total, aggregation_role, source_type, confidence,
    extraction_method, extraction_confidence, is_escalated,
    report_type, version, template_version,
    source_id, source_page, source_ref, source_cell
"""


def _iso(d: Any) -> Optional[str]:
    return d.isoformat() if hasattr(d, "isoformat") else (str(d) if d is not None else None)


def _envelope(row: dict) -> dict:
    """Wrap a raw v_series row into the canonical qualifier-carrying object.

    Every value object exposes period/basis/scope/aggregation_role + provenance
    so a consuming model cannot launder a qualifier-stripped number into a
    confident wrong answer.
    """
    period_end = row.get("period_end")
    # YTD twin (CLAUDE.md #3): YTD carries a cutoff month — derive it from
    # period_end (e.g. 2026-04-30 -> 4). Done here rather than reading the raw
    # observation table, keeping constraint #1 intact.
    ytd_cutoff = None
    if row.get("period_type") == "ytd_cumulative" and hasattr(period_end, "month"):
        ytd_cutoff = period_end.month

    warnings: list[str] = []
    if row.get("confidence") == "low" or row.get("extraction_confidence") == "low":
        warnings.append("LOW_CONFIDENCE: extraction flagged uncertain; excluded from the clean default surface.")
    if row.get("is_escalated"):
        warnings.append("ESCALATED: flagged uncertain (gas basis/scope OQ-R1 or field-name OQ-F2); presented provisionally, awaiting ONEM confirmation. Do not reconcile into other series.")
    if row.get("is_derived"):
        warnings.append("DERIVED: this value was computed by the loader (e.g. a grand total summed from leaves where the report printed none).")
    if row.get("data_status") and row["data_status"] != "final":
        warnings.append(f"DATA_STATUS={row['data_status']}: not a final figure.")

    return {
        "series_id": row.get("series_key"),
        "indicator": row.get("indicator"),
        "value": row.get("value"),
        "unit": row.get("unit_id"),
        # --- twin-defining qualifiers (constraint #3 / #4) ---
        "calorific_basis": row.get("calorific_basis"),
        "basis_confidence": row.get("basis_confidence"),
        "period_type": row.get("period_type"),
        "period_start": _iso(row.get("period_start")),
        "period_end": _iso(period_end),
        "ytd_cutoff_month": ytd_cutoff,
        "ref_year": row.get("ref_year"),
        "geography_scope": row.get("geography_scope"),
        "scope": row.get("scope"),
        # --- aggregation safety (constraint #5) ---
        "aggregation_role": row.get("aggregation_role"),
        "is_total": row.get("is_total"),
        "is_derived": row.get("is_derived"),
        # --- other attributes ---
        "technology": row.get("technology"),
        "regime": row.get("regime"),
        "redevance_included": row.get("redevance_included"),
        # --- quality / status flags ---
        "data_status": row.get("data_status"),
        "extraction_confidence": row.get("extraction_confidence"),
        "confidence": row.get("confidence"),
        "is_escalated": row.get("is_escalated"),
        # --- dimensions ---
        "dimensions": {
            k: row.get(k)
            for k in ("flow_id", "product_id", "sector_id", "region_id",
                      "field_id", "level_id", "producer_id")
            if row.get(k) is not None
        },
        # --- provenance (cite "Conjoncture avril 2026, p5, C-T1") ---
        "provenance": {
            "source_id": row.get("source_id"),
            "report_type": row.get("report_type"),
            "version": row.get("version"),
            "source_page": row.get("source_page"),
            "source_ref": row.get("source_ref"),
            "source_cell": row.get("source_cell"),
            "template_version": row.get("template_version"),
            "extraction_method": row.get("extraction_method"),
            "source_type": row.get("source_type"),
        },
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# scope_glossary helpers (constraint #4 / twins)
# --------------------------------------------------------------------------

def scope_glossary(attribute: Optional[str] = None) -> list[dict]:
    sql = "SELECT attribute, token, definition, never_sum_with FROM scope_glossary"
    params: list = []
    if attribute:
        sql += " WHERE attribute = ?"
        params.append(attribute)
    sql += " ORDER BY attribute, token"
    return _q(sql, params)


def _glossary_for(envelopes: list[dict]) -> list[dict]:
    """Return the glossary rows relevant to the tokens present in the results,
    so a blind LLM understands commercial_dry vs primary_broad, local vs
    incl_exports, PCI vs PCS, etc. (constraint #4: always expose the glossary)."""
    wanted: set[tuple[str, str]] = set()
    for e in envelopes:
        if e.get("scope"):
            wanted.add(("scope", e["scope"]))
        if e.get("geography_scope"):
            wanted.add(("geography_scope", e["geography_scope"]))
        if e.get("calorific_basis") and e["calorific_basis"] != "NA":
            wanted.add(("calorific_basis", e["calorific_basis"]))
    if not wanted:
        return []
    gl = scope_glossary()
    return [g for g in gl if (g["attribute"], g["token"]) in wanted]


# --------------------------------------------------------------------------
# Out-of-scope detection (constraint #4)
# --------------------------------------------------------------------------

def _deferred_match(query: str) -> Optional[dict]:
    """If a free-text query targets a defined-but-not-ingested family, return an
    explicit out-of-scope descriptor. Returns None if the query isn't clearly a
    deferred family. Checks precise phrase keywords first, then the generic
    single-word family terms a user actually types ('prices', 'refining')."""
    ql = query.lower()
    for iid, kws in _DEFERRED_KEYWORDS.items():
        if iid not in DEFERRED_INDICATORS:
            continue
        if any(kw in ql for kw in kws):
            ind = INDICATORS[iid]
            return _out_of_scope(iid, ind["canonical_name"], ind["category"])
    # generic family terms (whole-word match so 'price' fires but 'priced' inside
    # an unrelated word does not dominate; tokenized on non-alphanumerics)
    tokens = set(t for t in ql.replace("'", " ").replace("-", " ").replace("/", " ").split())
    for term, iid in _DEFERRED_FAMILY_TERMS.items():
        if term in tokens and iid in DEFERRED_INDICATORS:
            ind = INDICATORS[iid]
            return _out_of_scope(iid, ind["canonical_name"], ind["category"])
    return None


def _out_of_scope(indicator_id: str, name: str, category: str) -> dict:
    return {
        "status": "out_of_scope",
        "indicator_id": indicator_id,
        "indicator": name,
        "category": category,
        "message": (
            f"'{name}' ({category}) is a DEFINED indicator but has NO ingested "
            "series — it is OUT OF SCOPE / not ingested, which is NOT the same as "
            "'no data exists'. Do not tell the user the data does not exist."
        ),
        "guidance": (
            "Prices, trade-value/quantity, refining KPIs, exploration KPIs, "
            "petroleum-product imports/production, renewable capacity, peak power, "
            "the electricity supply balance, specific consumption and gas sales are "
            "consciously deferred. See coverage_gaps.md for the exact editions/tables "
            "and why."
        ),
        "see": "coverage_gaps.md",
    }


# --------------------------------------------------------------------------
# search_series / list_series  (semantic discovery over series_catalog)
# --------------------------------------------------------------------------

_TWIN_HINTS = {
    "calorific_basis": "PCI vs PCS are DISTINCT (PCI ≈ 0.9 × PCS); never equate.",
    "period_type": "annual vs ytd_cumulative are DISTINCT; YTD carries a cutoff month (period_end).",
    "geography_scope": "local vs incl_exports are DISTINCT (e.g. elec sales 17089 vs 17197).",
    "scope": "commercial_dry vs primary_broad (gas) and incl/excl GPL+condensat (crude) are DISTINCT; never sum/equate.",
}


def _score(row: dict, terms: list[str]) -> int:
    t1, t2, t3, t4 = _TIERS[row["series_id"]]
    score = 0
    for t in terms:
        if t in t1:
            score += 8
        elif t in t2:
            score += 4
        elif t in t3:
            score += 2
        elif t in t4:
            score += 1
    return score


# Relevance floor (S-2). Distinguishing a real-but-low-scoring query from an
# out-of-corpus concept by ABSOLUTE score alone is unreliable: an English query against
# the French catalog ("electricity sales") legitimately scores low yet still ranks the
# correct single indicator #1. The reliable discriminator is CONCENTRATION: a genuine
# concept query concentrates its top score on ONE indicator (electricity_sales,
# gas_production, redevance…); an out-of-corpus concept (transit throughput) only grazes
# incidental terms ("gas","to","volume") and spreads the same weak top score DIFFUSELY
# across several unrelated indicators (energy_balance/primary_balance/pp_consumption).
# So: flag low-relevance only when the top score is weak AND diffuse across indicators.
_RELEVANCE_FLOOR = 8          # weak top score
_DIFFUSE_INDICATORS = 2       # top score shared by > this many distinct indicators


def search_series(query: str, limit: int = 15, include_deferred_hint: bool = True) -> dict:
    """Semantic discovery over the 593-series catalog. Returns ranked series with
    their twin distinctions made explicit; flags out-of-scope when the query
    targets a deferred family, and distinguishes a confident match from a merely
    low-relevance similarity match (so absent concepts never look present)."""
    # S-3: expand recognized AR/dialect terms to canonical FR/EN tokens before
    # scoring/deferred-matching, so a dialect query reaches the same series.
    eff_query = _expand_dialect(query)
    deferred = _deferred_match(eff_query) if include_deferred_hint else None

    terms = [t for t in eff_query.lower().replace("'", " ").replace("-", " ").split() if len(t) > 1]
    scored = []
    for row in CATALOG:
        s = _score(row, terms)
        if s > 0:
            scored.append((s, row))
    scored.sort(key=lambda x: (-x[0], -int(x[1].get("n_obs") or 0)))
    top_score = scored[0][0] if scored else 0
    # concentration signal: how many DISTINCT indicators share the top score?
    top_indicators = {r["series_id"].split("|")[0] for sc, r in scored if sc == top_score} if scored else set()
    diffuse = len(top_indicators) > _DIFFUSE_INDICATORS
    hits = [_catalog_brief(r) for _, r in scored[:limit]]

    out: dict = {
        "status": "ok",
        "query": query,
        "n_matches": len(scored),
        "returned": len(hits),
        "series": hits,
        "twin_warnings": _twin_warnings(scored[:limit]),
    }

    # Deferred family recognized outright (keyword/family-term hit) → out-of-scope.
    if deferred is not None:
        out["also_note_out_of_scope"] = deferred

    if not hits and deferred is None:
        out["status"] = "empty_in_scope"
        out["message"] = (
            "No ingested series matched. This is an empty result WITHIN scope. "
            "If you expected a price, trade-value, refining, exploration, import, "
            "capacity or supply-balance figure, those families are deferred — see "
            "coverage_gaps.md (out-of-scope, not absent)."
        )
    elif hits and top_score < _RELEVANCE_FLOOR and diffuse:
        # S-2 fail-safe: the best match is weak AND diffuse — only incidental term overlap
        # spread across unrelated indicators (not a concept match). Do NOT
        # let a model treat these as "the data". Flag low relevance + scope-uncertainty,
        # and (if not already) point at the deferred families, so an out-of-corpus concept
        # (transit throughput, …) reads as possibly-out-of-scope, never as present data.
        out["status"] = "low_relevance"
        out["relevance"] = "low"
        out["top_score"] = top_score
        out["message"] = (
            "These series only WEAKLY match the query (incidental term overlap, not a "
            "concept match) — treat them as low-relevance candidates, NOT as confirmation "
            "that the queried concept exists in the store. If your query is about a concept "
            "like transit/throughput volume, pipeline flow, prices, or trade values, those "
            "families are NOT ingested (out-of-scope, not absent) — see coverage_gaps.md. "
            "Re-query with the canonical indicator name, or call list_series to confirm scope."
        )
        if deferred is None:
            out["also_note_out_of_scope"] = {
                "status": "possibly_out_of_scope",
                "message": (
                    "No strong in-scope match. The concept may be a deferred/not-ingested "
                    "family (prices, trade value/quantity, transit/throughput volume, "
                    "refining, exploration, imports, capacity). 'Not in scope' is NOT the "
                    "same as 'no data exists'."
                ),
                "see": "coverage_gaps.md",
            }
    return out


def _twin_warnings(scored: list) -> list[str]:
    """If the result set spans both sides of a twin, say so explicitly."""
    msgs = []
    for attr, hint in _TWIN_HINTS.items():
        vals = {str(r.get(attr)) for _, r in scored if r.get(attr) not in (None, "", "NA")}
        if len(vals) > 1:
            msgs.append(f"Result set spans multiple {attr} ({sorted(vals)}): {hint}")
    return msgs


def _catalog_brief(row: dict) -> dict:
    iid = row["series_id"].split("|")[0]
    return {
        "series_id": row["series_id"],
        # B-3: human-readable name from resolved FR labels (not id-laden tuple)
        "display_name": _labelled_name(row),
        "raw_display_name": row["display_name"],  # the original id tuple, kept for traceability
        "indicator": _label(iid, "fr") or row["display_name"].split(" — ")[0],
        "indicator_id": iid,
        "aggregation_role": row.get("aggregation_role"),
        "unit": row.get("unit"),
        "calorific_basis": row.get("calorific_basis"),
        "period_type": row.get("period_type"),
        "geography_scope": row.get("geography_scope") or None,
        "scope": row.get("scope") or None,
        # B-3: each dimension carries id + FR/EN label so the consumer never has to
        # quote an opaque id, but can still cite the stable id.
        "dimensions": _labelled_dimensions(row),
        "n_obs": int(row.get("n_obs") or 0),
        "years": f"{row.get('first_year')}–{row.get('last_year')}",
        "families": row.get("families"),
        "escalated": row.get("escalated") == "True",
        "confidence": row.get("confidence"),
        "definition": row.get("definition"),
    }


def list_series(indicator: Optional[str] = None, limit: int = 100) -> dict:
    """List catalogued series, optionally filtered by indicator id or name.

    On a zero-match filter, distinguishes OUT-OF-SCOPE (a deferred family — prices,
    refining, imports, …) from genuinely empty/unknown, so a query for a deferred
    family never reads as 'no data exists' (CLAUDE.md #5 / BLOCK B-2)."""
    rows = CATALOG
    if indicator:
        il = indicator.lower()
        iid = _NAME_TO_IID.get(indicator)
        rows = [
            r for r in CATALOG
            if r["series_id"].split("|")[0] == (iid or indicator)
            or il in r["display_name"].lower()
        ]
        if not rows:
            # 1) exact deferred indicator id / canonical name
            ind = (INDICATORS.get(indicator)
                   or INDICATORS.get(_NAME_TO_IID.get(indicator, "")))
            if ind and ind["n_series"] == 0:
                return _out_of_scope(ind["indicator_id"], ind["canonical_name"], ind["category"])
            # 2) a deferred FAMILY word the user typed ('prices', 'refining', …)
            deferred = _deferred_match(indicator)
            if deferred is not None:
                return deferred
            # 3) genuinely nothing matched, and not a known deferred family
            return {
                "status": "empty_in_scope",
                "indicator": indicator,
                "n": 0,
                "series": [],
                "message": (
                    f"No catalogued series matched '{indicator}'. This is an empty "
                    "result WITHIN scope (the filter matched no ingested indicator). "
                    "It is NOT a statement that the figure doesn't exist — if you meant "
                    "a price, trade-value, refining, exploration, import, capacity, "
                    "peak-power or supply-balance family, those are deferred "
                    "(out-of-scope); see coverage_gaps.md. Use search_series to discover "
                    "valid series."
                ),
            }
    return {
        "status": "ok",
        "n": len(rows),
        "returned": min(len(rows), limit),
        "series": [_catalog_brief(r) for r in rows[:limit]],
    }


# --------------------------------------------------------------------------
# get_series  (time series with full qualifiers)
# --------------------------------------------------------------------------

def get_series(
    series_id: str,
    period_type: Optional[str] = None,
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
    exclude_provisional: bool = False,
    include_low_confidence: bool = False,
    include_escalated: bool = False,
) -> dict:
    """Return the time series for a series_id, each point fully qualified.

    DEFAULT surface = v_series_clean (precedence-winning, non-low-confidence,
    non-escalated). The clean view INCLUDES provisional figures (in a recurring
    report store the latest year is always provisional); they are returned but
    each flagged with a DATA_STATUS warning. Set exclude_provisional=True to drop
    them. Low-confidence / escalated data widen to v_series and are opt-in,
    each returned with explicit warning flags (never silently)."""
    cat = CATALOG_BY_ID.get(series_id)

    # widen to v_series only when the caller opts into a flagged surface; then
    # re-apply per-flag filters so the two opt-ins are INDEPENDENT (ADVISE A-1):
    # include_low_confidence must NOT also surface escalated, and vice-versa.
    view = "v_series" if (include_low_confidence or include_escalated) else "v_series_clean"
    where = ["series_key = ?"]
    params: list = [series_id]
    if view == "v_series":
        if not include_low_confidence:
            where.append("extraction_confidence <> 'low'")
        if not include_escalated:
            where.append("is_escalated = FALSE")
    if period_type:
        where.append("period_type = ?")
        params.append(period_type)
    if start_year is not None:
        where.append("ref_year >= ?")
        params.append(start_year)
    if end_year is not None:
        where.append("ref_year <= ?")
        params.append(end_year)
    if exclude_provisional:
        where.append("data_status NOT IN ('provisional','estimated')")

    rows = _q(
        f"SELECT {_VALUE_SELECT} FROM {view} WHERE " + " AND ".join(where)
        + " ORDER BY period_type, period_start, period_end",
        params,
    )
    envelopes = [_envelope(r) for r in rows]

    if not envelopes:
        # Distinguish empty-in-scope from out-of-scope from unknown id.
        if cat is None:
            iid = series_id.split("|")[0]
            ind = INDICATORS.get(iid)
            if ind and ind["n_series"] == 0:
                return _out_of_scope(iid, ind["canonical_name"], ind["category"])
            return {
                "status": "unknown_series",
                "series_id": series_id,
                "message": "No such series_id in the catalog. Use search_series / list_series to discover valid ids.",
            }
        # A-3: if the catalogued series is itself low-confidence or escalated,
        # bridge discovery (n_obs>0) to retrieval so an empty clean-default result
        # never reads as "data missing" — name the exact opt-in to use.
        hint = ""
        if (cat.get("confidence") == "low"):
            hint = (" This series is flagged confidence=low and is therefore excluded "
                    "from the clean default; pass include_low_confidence=true to "
                    "retrieve it (the points return with a LOW_CONFIDENCE warning).")
        elif (cat.get("escalated") == "True"):
            hint = (" This series is ESCALATED (uncertain basis/scope or field identity) "
                    "and is excluded from the clean default; pass include_escalated=true "
                    "to retrieve it (the points return with an ESCALATED warning).")
        return {
            "status": "empty_in_scope",
            "series_id": series_id,
            "display_name": cat["display_name"],
            "n_obs_catalogued": int(cat.get("n_obs") or 0),
            "message": (
                "The series exists but no observations matched the filters "
                "(or all matching points were excluded as low-confidence/escalated; "
                "set include_low_confidence / include_escalated to see them). "
                "This is an empty result WITHIN scope, not missing data." + hint
            ),
        }

    return {
        "status": "ok",
        "series_id": series_id,
        "display_name": cat["display_name"] if cat else None,
        "definition": cat.get("definition") if cat else None,
        "aggregation_role": cat.get("aggregation_role") if cat else None,
        "view": view,
        "n_points": len(envelopes),
        "observations": envelopes,
        "scope_glossary": _glossary_for(envelopes),
    }


def get_observation(series_id: str, ref_year: int,
                    period_type: Optional[str] = None,
                    ytd_cutoff_month: Optional[int] = None) -> dict:
    """Point lookup: a single observation for series + period, fully qualified."""
    res = get_series(series_id, period_type=period_type,
                     start_year=ref_year, end_year=ref_year)
    if res["status"] != "ok":
        return res
    obs = res["observations"]
    if ytd_cutoff_month is not None:
        obs = [o for o in obs if o.get("ytd_cutoff_month") == ytd_cutoff_month]
    if not obs:
        return {
            "status": "empty_in_scope",
            "series_id": series_id,
            "message": f"Series exists but no observation for ref_year={ref_year}"
                       + (f", period_type={period_type}" if period_type else "")
                       + (f", cutoff month={ytd_cutoff_month}" if ytd_cutoff_month else "")
                       + ". Empty within scope.",
        }
    return {
        "status": "ok",
        "series_id": series_id,
        "n": len(obs),
        "observations": obs,
        "scope_glossary": _glossary_for(obs),
        "note": "Multiple points returned; disambiguate by period_type / ytd_cutoff_month." if len(obs) > 1 else None,
    }


# --------------------------------------------------------------------------
# describe_series / get_metadata
# --------------------------------------------------------------------------

def describe_series(series_id: str) -> dict:
    """Definition, unit, basis, period_type, scope, footnotes, provenance and
    escalation status for a series."""
    cat = CATALOG_BY_ID.get(series_id)
    if cat is None:
        iid = series_id.split("|")[0]
        ind = INDICATORS.get(iid)
        if ind and ind["n_series"] == 0:
            return _out_of_scope(iid, ind["canonical_name"], ind["category"])
        return {"status": "unknown_series", "series_id": series_id,
                "message": "No such series_id. Use search_series / list_series."}

    # one representative observation_id to resolve footnotes
    rep = _q(
        "SELECT observation_id FROM v_series WHERE series_key = ? LIMIT 1",
        [series_id],
    )
    footnotes: list[dict] = []
    if rep:
        footnotes = _q(
            """SELECT DISTINCT f.footnote_id, f.footnote_type, f.text
               FROM v_observation_footnotes f
               WHERE f.observation_id IN (
                   SELECT observation_id FROM v_series WHERE series_key = ?
               )""",
            [series_id],
        )

    return {
        "status": "ok",
        "series_id": series_id,
        "display_name": cat["display_name"],
        "definition": cat.get("definition"),
        "indicator": cat["display_name"].split(" — ")[0],
        "unit": cat.get("unit"),
        "calorific_basis": cat.get("calorific_basis"),
        "basis_confidence": cat.get("basis_confidence"),
        "period_type": cat.get("period_type"),
        "scope": cat.get("scope") or None,
        "geography_scope": cat.get("geography_scope") or None,
        "redevance_toggle": cat.get("redevance_toggle") or None,
        "aggregation_role": cat.get("aggregation_role"),
        "dimensions": {
            k: cat.get(k) for k in ("flow", "product", "sector", "region",
                                    "field", "level", "producer", "technology",
                                    "regime")
            if cat.get(k)
        },
        "n_obs": int(cat.get("n_obs") or 0),
        "years": f"{cat.get('first_year')}–{cat.get('last_year')}",
        "families": cat.get("families"),
        "confidence": cat.get("confidence"),
        "escalated": cat.get("escalated") == "True",
        "footnotes": footnotes,
        "scope_glossary": _glossary_for([{
            "scope": cat.get("scope") or None,
            "geography_scope": cat.get("geography_scope") or None,
            "calorific_basis": cat.get("calorific_basis"),
        }]),
    }


# --------------------------------------------------------------------------
# compare  (the category-error guardrail made concrete)
# --------------------------------------------------------------------------

# Attributes across which a comparison is a CATEGORY ERROR unless the caller
# explicitly forces it. Mirrors CLAUDE.md "never conflate twins".
_INCOMPATIBLE_ATTRS = ("calorific_basis", "period_type", "geography_scope", "scope", "unit")


def _resolve_roles(series_id: str, ref_year: Optional[int]) -> set[str]:
    """The aggregation_role(s) a series actually carries IN THE LIVE DB (not the
    catalog — ADVISE A-2). A few series flip role by year (e.g. a column that is a
    sole leaf in a sparse baseline year but the derived grand_total in a full
    year), so when no ref_year is pinned we treat the series as carrying ALL its
    roles for guard purposes (fail-safe: assume the riskier role is in play)."""
    sql = "SELECT DISTINCT aggregation_role FROM v_series WHERE series_key = ?"
    params: list = [series_id]
    if ref_year is not None:
        sql += " AND ref_year = ?"
        params.append(ref_year)
    rows = _q(sql, params)
    roles = {r["aggregation_role"] for r in rows if r["aggregation_role"]}
    return roles or {(CATALOG_BY_ID.get(series_id) or {}).get("aggregation_role") or "leaf"}


def _aggregation_conflict(series_ids: list[str], metas: list[dict],
                          ref_year: Optional[int]) -> Optional[dict]:
    """B-1 GATE: detect parent↔child / subtotal-as-total / dual-partition mixes
    among the compared series, so `compare` can no longer line up a grand_total
    beside its own components (the 17088 = 8839+7084+1165 double-count).

    Rule, scoped by period_type + geography_scope (mirrors C10/C11/C12): within a
    group of series that share (indicator, period_type, geography_scope) — i.e.
    members of the SAME aggregation partition — the only summation-safe mix is
    'all leaves'. A group is an aggregation conflict when, across >1 of the
    compared series, it contains:
      * a 'grand_total' alongside any other member (the total already includes them);
      * a 'subtotal' alongside any other member (subtotal vs its components/siblings);
      * both 'leaf' and 'alternative_breakdown' (two partitions of one total).
    Comparing unrelated totals from DIFFERENT indicators is fine (different group).
    """
    # group the compared series by (indicator, period_type, geography_scope)
    groups: dict[tuple, list[tuple[str, set[str]]]] = {}
    for sid, m in zip(series_ids, metas):
        indicator = sid.split("|")[0]
        key = (indicator, m.get("period_type") or "", m.get("geography_scope") or "")
        groups.setdefault(key, []).append((sid, _resolve_roles(sid, ref_year)))

    details = []
    for key, members in groups.items():
        if len(members) < 2:
            continue  # a single series can't double-count against itself here
        roles_present: set[str] = set()
        for _sid, roles in members:
            roles_present |= roles
        reason = None
        if "grand_total" in roles_present:
            reason = ("a grand_total is being compared with other members of the "
                      "same partition — the total ALREADY INCLUDES them; summing the "
                      "rows double-counts.")
        elif "subtotal" in roles_present:
            reason = ("a subtotal is being compared with members of its own group — "
                      "a subtotal is not a sibling leaf and must not be summed with "
                      "its components.")
        elif {"leaf", "alternative_breakdown"} <= roles_present:
            reason = ("two ALTERNATIVE partitions of the same total are mixed (e.g. "
                      "usage split AND pressure split) — they each sum to the same "
                      "total; combining them double-counts.")
        if reason:
            details.append({
                "indicator": key[0],
                "period_type": key[1] or None,
                "geography_scope": key[2] or None,
                "roles_present": sorted(roles_present),
                "series_ids": [sid for sid, _ in members],
                "reason": reason,
            })
    return {"groups": details} if details else None


def compare(series_ids: list[str], ref_year: Optional[int] = None,
            force: bool = False) -> dict:
    """Guardrailed comparison of two or more series.

    REFUSES (or hard-warns if force=True) when the series differ on calorific
    basis, period_type, geography_scope, scope, or unit — the twin distinctions
    that make a naive comparison a wrong answer — OR when they mix incompatible
    aggregation_roles (a grand_total/subtotal beside its own components, or two
    alternative partitions of one total), which would double-count."""
    if len(series_ids) < 2:
        return {"status": "error", "message": "compare needs at least two series_ids."}

    metas = []
    for sid in series_ids:
        cat = CATALOG_BY_ID.get(sid)
        if cat is None:
            return {"status": "unknown_series", "series_id": sid,
                    "message": "Unknown series_id; cannot compare. Use search_series."}
        metas.append(cat)

    # detect twin incompatibilities (basis/period/scope/geo/unit)
    conflicts = {}
    for attr in _INCOMPATIBLE_ATTRS:
        vals = {(m.get(attr) or "NA") for m in metas}
        if len(vals) > 1:
            conflicts[attr] = sorted(vals)

    # B-1: detect aggregation-role (parent↔child / dual-partition) mixing
    agg_conflict = _aggregation_conflict(series_ids, metas, ref_year)

    if agg_conflict and not force:
        return {
            "status": "refused_aggregation",
            "series_ids": series_ids,
            "aggregation_conflict": agg_conflict,
            "incompatible_on": {**conflicts, "aggregation_role": True} if conflicts else {"aggregation_role": True},
            "message": (
                "Comparison REFUSED (double-count guard): the series mix incompatible "
                "aggregation roles within the same partition — "
                + "; ".join(g["reason"] for g in agg_conflict["groups"])
                + " To total a partition, read the grand_total OR sum the leaves, never "
                "both. Re-issue with force=true ONLY if you have a documented reason; the "
                "result will carry a hard warning. (See aggregation_role / scope_glossary.)"
            ),
            "scope_glossary": scope_glossary(),
        }

    if conflicts and not force:
        return {
            "status": "refused_incompatible",
            "series_ids": series_ids,
            "incompatible_on": conflicts,
            "message": (
                "Comparison REFUSED: these series differ on "
                + ", ".join(conflicts)
                + ". Comparing across these is a category error (e.g. PCI vs PCS, "
                "annual vs YTD, local vs incl_exports, commercial_dry vs primary_broad). "
                "Re-issue with force=true ONLY if you have a documented reason; the "
                "result will carry a hard warning."
            ),
            "scope_glossary": scope_glossary(),
        }

    # fetch values (clean surface), aligned by ref_year/period
    series_data = []
    for sid in series_ids:
        res = get_series(sid, start_year=ref_year, end_year=ref_year) if ref_year else get_series(sid)
        pts = res.get("observations", []) if res["status"] == "ok" else []
        series_data.append({"series_id": sid,
                            "display_name": CATALOG_BY_ID[sid]["display_name"],
                            "points": pts})

    out = {
        "status": "compared_with_warning" if (conflicts or agg_conflict) else "ok",
        "series_ids": series_ids,
        "ref_year": ref_year,
        "data": series_data,
        "scope_glossary": scope_glossary() if (conflicts or agg_conflict) else _glossary_for(
            [p for sd in series_data for p in sd["points"]]),
    }
    warnings = []
    if conflicts:
        warnings.append(
            "COMPARISON ACROSS INCOMPATIBLE QUALIFIERS (" + ", ".join(conflicts)
            + ") was FORCED. The numbers are NOT directly comparable.")
        out["incompatible_on"] = conflicts
    if agg_conflict:
        warnings.append(
            "AGGREGATION-ROLE MIX was FORCED: "
            + "; ".join(g["reason"] for g in agg_conflict["groups"])
            + " Do NOT sum these rows — the total already includes its components.")
        out["aggregation_conflict"] = agg_conflict
        out["aggregation_warning"] = warnings[-1]
    if warnings:
        out["hard_warning"] = " ".join(warnings) + " Report this caveat to the user verbatim."
    return out


# --------------------------------------------------------------------------
# get_conflicts  (reconciliation_log)
# --------------------------------------------------------------------------

def get_conflicts(series_id: Optional[str] = None,
                  include_agreements: bool = False) -> dict:
    """Surface cross-edition DISAGREEMENTS retained in reconciliation_log: which
    editions disagree on a cell, the precedence-winning value, and the retained
    alternatives.

    Per CLAUDE.md #7, disagreements are RETAINED, never overwritten; the clean
    view already serves the precedence winner (Bilan > Memento > Conjoncture;
    final > provisional; later publication_date). reconciliation_log also records
    multi-source cells that AGREE (within tolerance) — those are hidden by default
    (a non-conflict); pass include_agreements=True to see them too."""
    PRECEDENCE = "Bilan > Memento > Conjoncture; final > provisional; later publication_date wins."
    where = ["1=1"]
    params: list = []
    if series_id:
        where.append("series_key = ?")
        params.append(series_id)
    if not include_agreements:
        where.append("note = 'disagreement>tol'")
    rows = _q(
        "SELECT id, series_key, ref_year, period_type, calorific_basis, metric, "
        "values_json, resolution, note FROM reconciliation_log WHERE "
        + " AND ".join(where) + " ORDER BY series_key, ref_year",
        params,
    )
    # parse the values_json into a structured per-edition breakdown
    for r in rows:
        try:
            r["values_by_source"] = json.loads(r.pop("values_json"))
        except Exception:
            r["values_by_source"] = None
    if not rows:
        return {
            "status": "no_conflicts",
            "series_id": series_id,
            "message": (
                "No cross-edition disagreements are recorded for this query. The "
                "clean view applies precedence (Bilan > Memento > Conjoncture; final "
                "> provisional; later publication wins), so the served value is the "
                "precedence winner."
            ),
            "precedence_rule": PRECEDENCE,
        }
    return {
        "status": "ok",
        "series_id": series_id,
        "n": len(rows),
        "conflicts": rows,
        "precedence_rule": PRECEDENCE,
        "note": ("Each conflict lists values_by_source (the disagreeing editions) and "
                 "the precedence-winning resolution. The clean view already serves the "
                 "winner; the alternatives are retained here, not discarded."),
    }


# --------------------------------------------------------------------------
# units / convert_units  (basis-aware)
# --------------------------------------------------------------------------

def list_units() -> dict:
    units = _q("SELECT unit_id, label_fr, label_en, quantity_kind, stock_or_flow, notes FROM unit ORDER BY quantity_kind, unit_id")
    factors = _q("SELECT from_unit, to_unit, factor, calorific_basis, scope, note FROM conversion_factor ORDER BY scope, from_unit")
    return {"status": "ok", "units": units, "conversion_factors": factors}


# --------------------------------------------------------------------------
# Carrier / scope synonym normalization (F-1)
# --------------------------------------------------------------------------
# convert_units' scope filter matched the stored carrier string EXACTLY, so a
# model disambiguating with scope="gas" or "natural_gas" got a false no_factor on
# the documented PCI->PCS conversion. This is a synonym/normalization gap (the same
# exact-string brittleness as the compare guard / label layer), not a single missing
# alias — so we normalize the class, not the one string.
#
# The synonym set is SEEDED FROM THE REAL FAILING TRAJECTORY ARGS the eval captured
# (T11: "gas", "natural_gas") — the strings models actually emit — and extended to
# the other stored carriers so the next carrier doesn't have to be rediscovered.
# Keys are pre-normalized (case-folded, separators collapsed) via _norm_scope_key();
# values are the canonical scope strings stored in conversion_factor.scope.
_SCOPE_SYNONYMS = {
    # natural gas (PCI<->PCS basis factor lives here)
    "gas": "natural gas",
    "natural gas": "natural gas",
    "gaz": "natural gas",
    "gaz naturel": "natural gas",
    "naturalgas": "natural gas",
    # crude oil
    "crude": "crude oil quality",
    "crude oil": "crude oil quality",
    "crudeoil": "crude oil quality",
    "oil": "crude oil quality",
    "petrole brut": "crude oil quality",
    "petrole": "crude oil quality",
    "crude oil quality": "crude oil quality",
    "crude oil map": "crude oil MAP",
    # electricity
    "electricity": "electricity",
    "elec": "electricity",
    "electricite": "electricity",
    "power": "electricity",
    # volume
    "volume": "volume",
    "vol": "volume",
    # petroleum products tep/t
    "product": "product tep/t",
    "products": "product tep/t",
    "petroleum product": "product tep/t",
    "petroleum products": "product tep/t",
    "produits petroliers": "product tep/t",
    "product tep/t": "product tep/t",
}


def _norm_scope_key(scope: str) -> str:
    """Normalize a free-text scope/carrier word for synonym lookup: case-fold,
    trim, strip accents, and collapse separators (_, -, multiple spaces) to a
    single space. 'Natural_Gas' / 'NATURAL-GAS' / ' gaz  ' all map to one key."""
    s = scope.strip().lower()
    # cheap accent fold for the FR carriers we map (é -> e, è -> e)
    for a, b in (("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a")):
        s = s.replace(a, b)
    # unify separators to single spaces
    for sep in ("_", "-", "/"):
        s = s.replace(sep, " ")
    s = " ".join(s.split())
    return s


def _canonical_scope(scope: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Map a caller-supplied scope to (canonical_scope, normalized_input).

    Returns (canonical, norm) when a synonym is recognized; (None, norm) when the
    input is non-empty but unrecognized (caller decides how to fail-safe); and
    (None, None) when no scope was supplied at all."""
    if not scope:
        return None, None
    norm = _norm_scope_key(scope)
    return _SCOPE_SYNONYMS.get(norm), norm


def convert_units(value: float, from_unit: str, to_unit: str,
                  scope: Optional[str] = None,
                  calorific_basis: Optional[str] = None) -> dict:
    """Basis-aware unit conversion using the stored conversion_factor table.

    PCI↔PCS is a basis change (0.9), NOT a unit change — handled explicitly and
    flagged so the consumer never silently equates the two. The real series carry
    `ktep-pci`/`ktep-pcs`; the basis factor is stored under the abstract `PCI`/`PCS`
    tokens, so those unit tokens are aliased to the basis factor (BLOCK: PCI↔PCS
    must be reachable via the units the data actually uses)."""
    if from_unit == to_unit:
        return {"status": "ok", "value": value, "from_unit": from_unit,
                "to_unit": to_unit, "factor": 1.0, "note": "identity"}

    # Alias the real `-pci`/`-pcs` unit tokens to the stored basis tokens so the
    # PCI↔PCS basis change is reachable. BOTH directions are stored explicitly
    # (F-3 ruling 2026-06-26): PCI->PCS factor 1.1111 (PCS>PCI), PCS->PCI factor 0.9.
    # map ktep-pci -> PCI, ktep-pcs -> PCS (and default scope).
    _BASIS_ALIAS = {"ktep-pci": "PCI", "ktep-pcs": "PCS"}
    q_from = _BASIS_ALIAS.get(from_unit, from_unit)
    q_to = _BASIS_ALIAS.get(to_unit, to_unit)
    aliased = (q_from != from_unit) or (q_to != to_unit)

    # F-1: normalize the caller's scope/carrier word (case/separator/synonym) to the
    # canonical stored scope, so scope="gas"/"natural_gas"/"Natural Gas" all resolve
    # the same factor as the literal "natural gas". An UNRECOGNIZED non-empty scope is
    # passed through verbatim — the lookup will simply not match and we fail safe
    # (no fabricated factor), never silently widening to the wrong carrier.
    scope_requested = scope
    canonical, scope_norm = _canonical_scope(scope)
    if canonical:
        scope = canonical
    if aliased and not scope:
        scope = "natural gas"  # the PCI<->PCS factor's stored scope

    def _lookup(a, b):
        sql = "SELECT from_unit, to_unit, factor, calorific_basis, scope, note FROM conversion_factor WHERE from_unit = ? AND to_unit = ?"
        params: list = [a, b]
        if scope:
            sql_ = sql + " AND scope = ?"
            params.append(scope)
            return _q(sql_, params)
        return _q(sql, params)

    rows = _lookup(q_from, q_to)
    inverted = False
    if not rows:
        # Defensive reciprocal fallback: both PCI/PCS directions are now stored
        # explicitly, so this should not fire for the basis alias. It remains ONLY
        # as a safety net for a carrier whose reverse row is genuinely absent, and
        # only for the basis alias — we never fabricate an unrelated factor. NOTE:
        # 1/factor is the mathematically correct reciprocal; the F-3 inversion bug
        # was a wrong STORED direction, not this reciprocal arithmetic.
        rev = _lookup(q_to, q_from)
        if rev and aliased:
            inverted = True
            rows = rev
    if not rows:
        unrecognized_scope = bool(scope_requested) and canonical is None
        msg = ("No stored conversion factor for this exact pair/scope. "
               "Call list_units to see available conversions. Conversions "
               "are NOT invented — only documented factors are applied; do "
               "not improvise a number (e.g. do not assume 0.9 yourself).")
        if unrecognized_scope:
            msg += (f" (The scope '{scope_requested}' was not recognized as a known "
                    "carrier; retry without scope, or use a known carrier such as "
                    "'natural gas', 'crude oil', 'electricity', 'volume'.)")
        return {
            "status": "no_factor",
            "from_unit": from_unit, "to_unit": to_unit,
            "scope": scope_requested,
            "message": msg,
        }
    if len(rows) > 1:
        return {
            "status": "ambiguous",
            "from_unit": from_unit, "to_unit": to_unit,
            "candidates": rows,
            "message": "Multiple factors match across scopes; pass scope= to disambiguate.",
        }
    f = rows[0]
    factor = (1.0 / f["factor"]) if inverted else f["factor"]
    result = value * factor
    warn = None
    # a basis change is flagged either by the factor's PCI->PCS tag OR because we
    # aliased a -pci/-pcs unit pair (the conversion crosses calorific bases).
    is_basis_change = ("->" in str(f.get("calorific_basis"))
                       or (aliased and q_from in ("PCI", "PCS") and q_to in ("PCI", "PCS")
                           and q_from != q_to))
    if is_basis_change:
        warn = ("This conversion CHANGES calorific basis (" + str(f.get("calorific_basis"))
                + f"; {from_unit} → {to_unit}). PCI and PCS are DISTINCT bases — the "
                "result is on a different basis from the input; do not equate them.")
    return {
        "status": "ok",
        "value": result,
        "input_value": value,
        # echo the units the caller actually asked for, not the internal alias
        "from_unit": from_unit, "to_unit": to_unit,
        "resolved_factor_key": {"from": q_from, "to": q_to, "inverted": inverted} if aliased else None,
        "factor": factor, "scope": f["scope"],
        # surface that we normalized the caller's scope word, for traceability
        "scope_requested": scope_requested if (scope_requested and canonical
                                               and scope_requested != f["scope"]) else None,
        "calorific_basis": f.get("calorific_basis"),
        "note": f.get("note"),
        "warning": warn,
    }


# --------------------------------------------------------------------------
# list_dimensions / get_scope_glossary
# --------------------------------------------------------------------------

_DIM_TABLES = {
    "flow": ("flow", "flow_id", "label_fr, label_en, parent_flow_id, is_total, aggregation_level, definition"),
    "product": ("product", "product_id", "label_fr, label_en, category, parent_product_id, is_total, definition"),
    "sector": ("sector", "sector_id", "label_fr, label_en, parent_sector_id, is_total, definition"),
    "region": ("region", "region_id", "label, region_type, composition, definition"),
    "field": ("field", "field_id", "label, produces, is_aggregate, notes"),
    "level": ("level", "level_id", "label_fr, label_en, domain"),
    "producer": ("producer", "producer_id", "label, notes"),
}


def list_dimensions(dimension: Optional[str] = None) -> dict:
    """Dimension vocabularies (flow, product, sector, region, field, level,
    producer), including parent edges and total-ness so the consumer can see the
    hierarchy that drives aggregation safety."""
    if dimension:
        if dimension not in _DIM_TABLES:
            return {"status": "error",
                    "message": f"Unknown dimension '{dimension}'. Valid: {sorted(_DIM_TABLES)}"}
        tbl, idc, cols = _DIM_TABLES[dimension]
        rows = _q(f"SELECT {idc}, {cols} FROM {tbl} ORDER BY {idc}")
        return {"status": "ok", "dimension": dimension, "n": len(rows), "values": rows}
    return {"status": "ok", "dimensions": {
        d: _q(f"SELECT count(*) AS n FROM {t[0]}")[0]["n"] for d, t in _DIM_TABLES.items()
    }, "note": "Pass dimension= (flow/product/sector/region/field/level/producer) for the vocabulary."}


def get_scope_glossary(attribute: Optional[str] = None) -> dict:
    """The scope_glossary: defines qualifier tokens (commercial_dry, primary_broad,
    local, incl_exports, PCI, PCS, power_generation, non_power, …) and the
    'do-not-sum/equate-across' rule for each."""
    return {"status": "ok", "glossary": scope_glossary(attribute)}
