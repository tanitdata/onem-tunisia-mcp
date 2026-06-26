"""mcp_server.py — read-only MCP server over the ONEM Tunisia energy store.

Read `CLAUDE.md` first; its conventions override anything here that conflicts.

This is the thin FastMCP wrapper. All logic lives in `onem_store.py` (a pure,
read-only data layer that is unit-testable without an MCP runtime). The tool and
parameter DESCRIPTIONS below are part of the product: the consuming LLM selects
tools from them, so they are written to make twin distinctions and the
out-of-scope-vs-no-data difference explicit.

Run (stdio): python mcp_server.py
"""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

import onem_store as store

SERVER_INSTRUCTIONS = """\
ONEM Tunisia energy time-series store (read-only). Data comes from recurring ONEM
reports (Bilan / Memento / Conjoncture), stored long-format in DuckDB.

CRITICAL RULES for any answer you build from these tools:
1. Every value carries qualifiers: period_type, period_start/end, calorific_basis,
   geography_scope, scope, aggregation_role, data_status and provenance. Quote a
   number WITH its basis/period/scope, never bare.
2. Never conflate TWINS — they are different series:
   • calorific_basis PCI vs PCS (PCI ≈ 0.9 × PCS)
   • period_type annual vs ytd_cumulative (YTD carries a cutoff month)
   • geography_scope local vs incl_exports (e.g. elec sales 17089 vs 17197)
   • scope commercial_dry vs primary_broad (gas); crude incl vs excl GPL+condensat
   Use `compare` for any cross-series comparison — it refuses incompatible pairs.
3. To total a partition, read the grand_total (aggregation_role='grand_total') or
   sum the leaves — NEVER sum a total with its components, and never mix a
   partition with an alternative_breakdown.
4. "Not in scope" ≠ "no data". If a tool returns status 'out_of_scope', the family
   (prices, trade values, refining, exploration, imports, capacity, …) is
   consciously NOT ingested — say so; never tell the user the data doesn't exist.
   See coverage_gaps.md.
"""

mcp = FastMCP("onem-energy", instructions=SERVER_INSTRUCTIONS)


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------
@mcp.tool()
def search_series(query: str, limit: int = 15) -> dict:
    """Semantic discovery over the ONEM series catalog (593 series). Free-text
    search by indicator, product, field, sector, region, basis, period type, etc.

    Returns ranked series with their full qualifier signature (unit, calorific
    basis, period_type, geography_scope, scope, aggregation_role) and explicit
    TWIN warnings when the result set spans both sides of a distinction (PCI/PCS,
    annual/YTD, local/incl_exports, commercial_dry/primary_broad). If the query
    targets a DEFERRED family (prices, trade values, refining, exploration,
    imports, capacity, …), the response includes an out-of-scope note — that is
    different from 'no data'. Use this first to find a series_id, then call
    get_series / describe_series.

    Args:
        query: natural-language description of the wanted series.
        limit: max series to return (default 15).
    """
    return store.search_series(query, limit=limit)


@mcp.tool()
def list_series(indicator: Optional[str] = None, limit: int = 100) -> dict:
    """List catalogued series, optionally filtered to one indicator.

    Args:
        indicator: indicator id (e.g. 'gas_production') or its name; omit for all.
                   A deferred indicator returns an out-of-scope response.
        limit: max series to return.
    """
    return store.list_series(indicator=indicator, limit=limit)


# ----------------------------------------------------------------------------
# Values
# ----------------------------------------------------------------------------
@mcp.tool()
def get_series(
    series_id: str,
    period_type: Optional[str] = None,
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
    exclude_provisional: bool = False,
    include_low_confidence: bool = False,
    include_escalated: bool = False,
) -> dict:
    """Return the full time series for a series_id, every point carrying its
    qualifiers (basis, period_type, period_start/end, ytd_cutoff_month, scope,
    geography_scope, aggregation_role, data_status) and provenance, plus the
    relevant scope_glossary entries.

    Defaults to the CLEAN surface (precedence-winning, non-low-confidence,
    non-escalated). Provisional points ARE included by default (the latest report
    year is always provisional) but flagged; set exclude_provisional=True to drop
    them. Low-confidence and escalated data are OPT-IN and returned with loud
    warnings — never silently mixed in.

    Args:
        series_id: stable id from search_series / list_series.
        period_type: filter to 'annual' / 'ytd_cumulative' / 'monthly' / 'point_in_time'.
        start_year / end_year: inclusive ref_year bounds.
        exclude_provisional: drop provisional/estimated points.
        include_low_confidence: also return low-confidence extractions (flagged).
        include_escalated: also return escalated (OQ-R1/F2) items (flagged provisional).
    """
    return store.get_series(
        series_id, period_type=period_type, start_year=start_year, end_year=end_year,
        exclude_provisional=exclude_provisional,
        include_low_confidence=include_low_confidence,
        include_escalated=include_escalated,
    )


@mcp.tool()
def get_observation(
    series_id: str,
    ref_year: int,
    period_type: Optional[str] = None,
    ytd_cutoff_month: Optional[int] = None,
) -> dict:
    """Point lookup: the observation(s) for one series in one year, fully
    qualified. If several points match (e.g. both annual and YTD), all are
    returned with a note to disambiguate by period_type / ytd_cutoff_month.

    Args:
        series_id: stable series id.
        ref_year: the reference year.
        period_type: optional filter ('annual' / 'ytd_cumulative' / …).
        ytd_cutoff_month: for YTD series, the cutoff month (e.g. 4 = à fin avril).
    """
    return store.get_observation(series_id, ref_year, period_type=period_type,
                                 ytd_cutoff_month=ytd_cutoff_month)


# ----------------------------------------------------------------------------
# Metadata
# ----------------------------------------------------------------------------
@mcp.tool()
def describe_series(series_id: str) -> dict:
    """Full metadata for a series: definition, unit, calorific basis, period_type,
    scope, geography_scope, aggregation_role, dimensions, source families, year
    span, escalation status, verbatim footnotes, and the relevant scope_glossary
    entries. Use before quoting a value to know exactly what it means.

    Args:
        series_id: stable series id.
    """
    return store.describe_series(series_id)


# alias kept because the brief names both describe_series and get_metadata
@mcp.tool()
def get_metadata(series_id: str) -> dict:
    """Alias of describe_series: definition, unit, basis, period_type, scope,
    footnotes, provenance and escalation status for a series."""
    return store.describe_series(series_id)


# ----------------------------------------------------------------------------
# Guardrailed comparison
# ----------------------------------------------------------------------------
@mcp.tool()
def compare(series_ids: list[str], ref_year: Optional[int] = None,
            force: bool = False) -> dict:
    """Guardrailed comparison of two or more series. REFUSES (status
    'refused_incompatible') when the series differ on calorific_basis,
    period_type, geography_scope, scope, or unit — comparing across those is a
    category error (PCI vs PCS, annual vs YTD, local vs incl_exports,
    commercial_dry vs primary_broad). When compatible, returns the aligned values
    with qualifiers. Pass force=True ONLY with a documented reason — the result
    then carries a hard warning that the numbers are not directly comparable.

    Args:
        series_ids: two or more stable series ids.
        ref_year: optional year to align the comparison on.
        force: override the incompatibility guard (returns a hard-warned result).
    """
    return store.compare(series_ids, ref_year=ref_year, force=force)


# ----------------------------------------------------------------------------
# Cross-edition conflicts
# ----------------------------------------------------------------------------
@mcp.tool()
def get_conflicts(series_id: Optional[str] = None,
                  include_agreements: bool = False) -> dict:
    """Surface cross-edition disagreements (reconciliation_log): which editions
    disagree on a cell (values_by_source), the precedence-winning value, and the
    retained alternatives. Disagreements are retained, never overwritten; the clean
    view already serves the precedence winner (Bilan > Memento > Conjoncture; final >
    provisional; later publication wins).

    Args:
        series_id: optional; omit to list all recorded disagreements.
        include_agreements: also include multi-source cells that AGREE within
            tolerance (hidden by default — those are not conflicts).
    """
    return store.get_conflicts(series_id=series_id, include_agreements=include_agreements)


# ----------------------------------------------------------------------------
# Units & conversions
# ----------------------------------------------------------------------------
@mcp.tool()
def list_units() -> dict:
    """List the unit vocabulary and every stored conversion factor (GWh↔ktep
    0.086, PCI→PCS 0.9, bbl↔m³, product tep/t, …). Conversions are basis-aware."""
    return store.list_units()


@mcp.tool()
def convert_units(value: float, from_unit: str, to_unit: str,
                  scope: Optional[str] = None,
                  calorific_basis: Optional[str] = None) -> dict:
    """Convert a value between units using only DOCUMENTED factors (never
    invented). PCI→PCS is treated as a basis change (0.9) and flagged: the result
    is on a DIFFERENT calorific basis and must not be equated with the input.

    Args:
        value: numeric value to convert.
        from_unit / to_unit: unit ids (see list_units).
        scope: disambiguator when a pair has scope-specific factors (e.g. 'electricity', 'natural gas', 'volume').
        calorific_basis: optional basis context.
    """
    return store.convert_units(value, from_unit, to_unit, scope=scope,
                               calorific_basis=calorific_basis)


# ----------------------------------------------------------------------------
# Dimensions & glossary
# ----------------------------------------------------------------------------
@mcp.tool()
def list_dimensions(dimension: Optional[str] = None) -> dict:
    """Dimension vocabularies (flow, product, sector, region, field, level,
    producer) with parent edges and total-ness. Omit dimension for counts; pass
    one (e.g. 'product') for its full vocabulary.

    Args:
        dimension: one of flow/product/sector/region/field/level/producer.
    """
    return store.list_dimensions(dimension=dimension)


@mcp.tool()
def get_scope_glossary(attribute: Optional[str] = None) -> dict:
    """The scope_glossary: defines every qualifier token (commercial_dry,
    primary_broad, local, incl_exports, PCI, PCS, power_generation, non_power,
    incl/excl GPL+condensat, redevance incl/excl) and the 'do-not-sum/equate-with'
    rule for each. Consult this to explain or disambiguate a scope/basis token.

    Args:
        attribute: optional filter ('scope' / 'geography_scope' / 'calorific_basis' / 'redevance_toggle').
    """
    return store.get_scope_glossary(attribute=attribute)


if __name__ == "__main__":
    mcp.run()
