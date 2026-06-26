# ONEM Tunisia Energy MCP

**A qualifier-aware [MCP](https://modelcontextprotocol.io) server over Tunisian energy time-series
(2010–2026), derived from public ONEM reports.**

> ⚠️ **Unofficial & independent.** This is an independent, community project. It is **not affiliated
> with, authorized by, or endorsed by ONEM or the Tunisian Ministry of Industry, Mines and Energy.**
> Figures are extracted from public reports by an automated pipeline and may contain errors. For
> authoritative data, consult ONEM directly. Always carry the qualifiers (basis / period / scope) this
> tool exposes.

## Demo

An LLM answering Tunisian-energy questions through the server, keeping bases and periods straight:

https://github.com/user-attachments/assets/caa7a43a-c46d-4028-9c23-f085383070a8

## What it is

An MCP server an LLM client (e.g. Claude Desktop) can query for Tunisian energy statistics — production,
consumption, trade, balances, royalties — across **Bilan**, **Memento (Chiffres clés)**, and
**Conjoncture** report families. The data lives in a long-format **DuckDB** store; the server exposes it
through tools that an LLM selects from, opened **read-only**.

## Why it's different — *qualifier-aware*

Energy statistics are full of look-alike numbers that mean different things. This server is built so a
model **cannot quietly conflate them**:

- **Never conflates twins** — PCI vs PCS calorific basis (PCI ≈ 0.9 × PCS), **annual vs year-to-date**
  (YTD carries a cutoff month), **local vs incl-exports** electricity sales, commercial-dry vs
  primary-broad gas, crude incl/excl GPL+condensat. The `compare` tool **refuses** to line up
  incompatible series.
- **Distinguishes _out-of-scope_ from _no-data_** — if a family was never ingested (prices, transit
  volumes, …), the server says **"not in scope / not ingested"** — it never implies the data doesn't
  exist.
- **Surfaces methodological footnotes** — e.g. the STEG↔State royalty-regularization caveat that
  explains an apparent contradiction — and tags **aggregation roles** so totals aren't double-counted.
- **Every value carries its qualifiers** (basis, period, scope, geography, data-status, provenance).
  No bare numbers.

## Data source & attribution

Data derived and **restructured** from public reports published by **ONEM — Observatoire National de
l'Energie et des Mines, Ministère de l'Industrie, des Mines et de l'Énergie, Tunisia**
([energiemines.gov.tn](https://www.energiemines.gov.tn)). The underlying figures are ONEM's; this project
re-structures them into a queryable time-series store. PDFs are **not re-hosted** — fetch them from ONEM
with the included downloader (see *Reproducibility*).

## Coverage & known gaps (stated up front, not buried)

Honesty about limits is a feature here. See **[`coverage_gaps.md`](coverage_gaps.md)** for the full map.
In short:

- **Deferred families (out of scope, not "no data"):** prices, trade values/quantities (incl. pipeline
  transit volumes), refining KPIs, exploration KPIs, product imports, capacity. The tools report these
  as out-of-scope.
- **Deferred editions:** Memento 2018–2023, some older Bilan editions, Conjoncture FR 2018–most-2019,
  COVID bulletins.
- **Low-confidence cells:** some older-edition extractions are flagged and excluded from the clean
  default surface.
- **Escalated / unresolved items:** gas basis/scope (Bilan primary-broad vs commercial-dry, OQ-R1) and
  the *Barka* vs *Maâmoura-Baraka* field identity (OQ-F2) are flagged uncertain and isolated, awaiting
  ONEM confirmation.

## Install & run

Requirements: Python 3.12+, `duckdb`, and the `mcp` Python SDK.

```bash
git clone https://github.com/tanitdata/onem-tunisia-mcp.git
cd onem-tunisia-mcp
pip install duckdb mcp
python mcp_server.py      # serves over stdio
```

The repository ships the built **`energy.duckdb`**, so the server works immediately — no build step
needed to start querying. The server speaks MCP over **stdio**.

> In every config below, replace `/absolute/path/to/onem-tunisia-mcp` with the real path to your clone.
> `PYTHONIOENCODING=utf-8` matters on Windows (the data has accented French labels).

### Claude Code (CLI)

```bash
claude mcp add onem-energy \
  --env PYTHONIOENCODING=utf-8 \
  -- python /absolute/path/to/onem-tunisia-mcp/mcp_server.py
```

Or add it to a project-local `.mcp.json` (shipped in this repo as an example) and run `claude` from the
project directory. List/manage with `claude mcp list` and `claude mcp remove onem-energy`.

### Claude Desktop

Edit `claude_desktop_config.json` (**Settings → Developer → Edit Config**), then fully restart the app:

```json
{
  "mcpServers": {
    "onem-energy": {
      "command": "python",
      "args": ["/absolute/path/to/onem-tunisia-mcp/mcp_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

### Cursor

`Settings → MCP → Add new MCP server` (writes `~/.cursor/mcp.json`), or add the block directly:

```json
{
  "mcpServers": {
    "onem-energy": {
      "command": "python",
      "args": ["/absolute/path/to/onem-tunisia-mcp/mcp_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

### VS Code (GitHub Copilot / Continue) and other MCP clients

Any MCP-capable client uses the same stdio launch. In VS Code, add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "onem-energy": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/onem-tunisia-mcp/mcp_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

The universal recipe for any client: **command** `python`, **args** `[".../mcp_server.py"]`,
**transport** `stdio`, **env** `PYTHONIOENCODING=utf-8`.

## Reproducibility — how the store is built

You don't have to trust the shipped DB; rebuild it from the source reports:

```bash
python acquire.py     # fetch the PDF corpus from ONEM (polite, idempotent, sha256-verified)
python backfill.py    # build energy.duckdb from the corpus
python validate.py    # run the validation gate
python report.py      # validation report + reconciliation log + coverage
```

Provenance lives in [`manifest.csv`](manifest.csv) (source URLs + hashes) and the methodology docs;
the live series inventory is in [`series_catalog.md`](series_catalog.md). Cross-edition disagreements
are **retained, not silently fixed** — see [`new_conflicts.md`](new_conflicts.md).

## Evaluation

The server ships with a three-layer eval (see [`eval/`](eval/)):

- **Layer 1 — retrieval fidelity** (deterministic): values + qualifiers survive the tool round-trip.
- **Layer 3 — adversarial guards** (deterministic): twin-conflation and double-count attempts are
  refused; out-of-scope is honest.
- **Layer 2 — behavioral** (model-in-the-loop): an LLM answers realistic questions over the tools.

Layers 1 and 3 are the hard, credential-free regression gate; Layer 2 is the richer, stochastic layer
on top. Results and findings are in [`eval/`](eval/).

## License

**MIT** — see [`LICENSE`](LICENSE). The license covers the code and the derived data structures; the
underlying figures are ONEM's, restructured here. This project is independent and unofficial (see the
disclaimer at the top).
