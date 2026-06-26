"""
eval/harness.py — shared, READ-ONLY plumbing for the ONEM MCP eval suite.

Two surfaces, both read-only:
  • call_tool(name, args)  -> dict   : drive a real MCP tool through the live FastMCP
                                        dispatch (mcp_server.mcp.call_tool), the same
                                        path a stdio client hits. Returns the parsed
                                        JSON envelope the consumer receives.
  • db()                   -> conn   : a read_only DuckDB connection for GROUND TRUTH
                                        (Layer 1 grades the tool round-trip against the
                                        clean views — never against the source PDFs).

Nothing here writes to energy.duckdb or mutates the server. CLAUDE.md #6: readers open
read-only. The suite modifies nothing.

This module is import-only; it does not run anything at import time beyond what is needed
to reach the server module (which itself opens the DB read-only).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

# repo root is the parent of eval/
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

DB_PATH = os.path.join(_REPO, "energy.duckdb")

# The 12 tools the server exposes (used by validators / coverage checks).
TOOLS = [
    "search_series", "list_series", "describe_series", "get_series", "get_observation",
    "compare", "get_conflicts", "convert_units", "get_metadata", "get_scope_glossary",
    "list_dimensions", "list_units",
]

# Full qualifier envelope every served value point must carry (CLAUDE.md #4).
REQUIRED_QUALIFIERS = [
    "period_type", "calorific_basis", "data_status", "aggregation_role",
]
# Provenance/identity fields that must also be reachable on a point or its series wrapper.
PROVENANCE_FIELDS = ["unit", "value"]


# --------------------------------------------------------------------------------------
# MCP dispatch (the consumer's view)
# --------------------------------------------------------------------------------------
_srv = None


def _server():
    global _srv
    if _srv is None:
        import mcp_server as m  # opens the DB read-only on import
        _srv = m
    return _srv


def call_tool(name: str, args: dict) -> dict:
    """Return the parsed JSON dict a consuming LLM would receive from `name`.

    Drives the real FastMCP server (`mcp.call_tool`) — same dispatch a stdio client uses.
    FastMCP returns (content_list, structured_dict) on recent versions, or a bare
    content_list on older ones; prefer the structured dict, else parse the first
    TextContent's JSON. Raises RuntimeError if the result can't be parsed.
    """
    srv = _server()
    result = asyncio.run(srv.mcp.call_tool(name, args))
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        structured = result[1]
        if "result" in structured and len(structured) == 1:
            return structured["result"]
        return structured
    content = result[0] if isinstance(result, tuple) else result
    for item in content:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    raise RuntimeError(f"Could not parse tool result for {name}: {result!r}")


# --------------------------------------------------------------------------------------
# Ground truth (the database, read-only)
# --------------------------------------------------------------------------------------
_db = None


def db():
    """A process-wide read-only DuckDB connection for ground-truth lookups."""
    global _db
    if _db is None:
        import duckdb
        _db = duckdb.connect(DB_PATH, read_only=True)
    return _db


def db_one(sql: str, params: list | None = None):
    cur = db().execute(sql, params) if params else db().execute(sql)
    return cur.fetchone()


def db_all(sql: str, params: list | None = None):
    cur = db().execute(sql, params) if params else db().execute(sql)
    return cur.fetchall()


# --------------------------------------------------------------------------------------
# Small helpers shared by validators
# --------------------------------------------------------------------------------------
def iter_points(resp: dict):
    """Yield the value-point dicts from a get_series / get_observation envelope,
    regardless of which key the server used (observations / points / data)."""
    for key in ("observations", "points", "data"):
        v = resp.get(key)
        if isinstance(v, list):
            yield from (p for p in v if isinstance(p, dict))
            return
    # get_observation may return a single point object
    if resp.get("value") is not None and "series_id" in resp:
        yield resp


def missing_qualifiers(point: dict) -> list[str]:
    """Return the REQUIRED_QUALIFIERS absent from a value point (a bare-number check)."""
    return [q for q in REQUIRED_QUALIFIERS if q not in point or point.get(q) in (None, "")]


def approx(a, b, tol_frac=0.01, tol_abs=1.0) -> bool:
    """True if a≈b within 1% or 1.0 absolute (values carry rounding across editions)."""
    if a is None or b is None:
        return False
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= max(tol_abs, tol_frac * max(abs(a), abs(b)))
