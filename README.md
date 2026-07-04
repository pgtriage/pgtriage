# pgtriage

[![CI](https://github.com/pgtriage/pgtriage/actions/workflows/ci.yml/badge.svg)](https://github.com/pgtriage/pgtriage/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pgtriage)](https://pypi.org/project/pgtriage/)
[![Python](https://img.shields.io/pypi/pyversions/pgtriage)](https://pypi.org/project/pgtriage/)
[![License](https://img.shields.io/github/license/pgtriage/pgtriage)](LICENSE)

MCP server for PostgreSQL performance auditing. Connect it to Claude Code (or any MCP client) and say "audit my database" to get actionable performance findings with exact fixes.

> Not related to the [pgAudit](https://www.pgaudit.org/) logging extension. pgtriage does performance triage, not compliance logging.

<!-- mcp-name: io.github.pgtriage/pgtriage -->

## Why I built this

Built after diagnosing implicit type casts and missing indexes on multi-million-row tables in production fintech systems. The fixes were simple (one `CREATE INDEX CONCURRENTLY` statement each), but finding them required reading query plans most engineers never look at. pgtriage automates that diagnostic process and lets any AI client explain the results.

## How it works

```
Any MCP Client (Claude Code / Cursor / Windsurf / VS Code)
       |  MCP (stdio)
       v
pgtriage (data collection + pattern detection)
       |  psycopg3 (read-only)
       v
PostgreSQL database
```

pgtriage connects to your PostgreSQL database and exposes performance auditing tools via the Model Context Protocol. It collects metrics from PostgreSQL system views, runs deterministic pattern detection, and returns structured findings. The MCP client provides the AI layer, interpreting results and explaining fixes in plain English.

No API keys required. No AI costs. No vendor lock-in. The intelligence comes from your MCP client.

## Example output

```json
{
  "severity": "high",
  "category": "connection_pressure",
  "detail": "Connection utilization at 104% (104/100). Approaching max_connections limit.",
  "suggested_fix": "Consider using a connection pooler (PgBouncer) or increasing max_connections if RAM allows.",
  "evidence": {
    "total_connections": 104,
    "max_connections": 100,
    "utilization_pct": 104.0
  }
}
```

```json
{
  "severity": "medium",
  "category": "duplicate_index",
  "table": "account",
  "detail": "Duplicate indexes on 'account': 'account_title_reverse_index' (16 kB) and 'account_group_reverse_index' (16 kB). Same column definition. One can be dropped.",
  "suggested_fix": "DROP INDEX CONCURRENTLY account_group_reverse_index;"
}
```

From a real audit: 118 tables scanned, 88 findings, prioritized by severity.

## What it finds

- **Sequential scans on large tables** with missing index suggestions
- **Dead tuple buildup** and autovacuum health issues
- **Unused and duplicate indexes** wasting disk and slowing writes
- **N+1 query patterns** from pg_stat_statements analysis
- **Stale table statistics** causing bad query plans
- **TOAST table bloat** from large JSONB/TEXT columns
- **Configuration issues** (shared_buffers, work_mem, autovacuum tuning)
- **Connection pressure** approaching max_connections
- **Long-running queries** holding locks

## Quick start

### Install

```bash
pip install pgtriage
```

### Configure Claude Code

Add to your MCP settings (`.claude/settings.json` or project settings):

```json
{
  "mcpServers": {
    "pgtriage": {
      "command": "python",
      "args": ["-m", "pgtriage"],
      "env": {
        "PGTRIAGE_CONNECTION_STRING": "postgres://user:pass@localhost:5432/dbname"
      }
    }
  }
}
```

**Recommended:** Use a dedicated read-only database role:

```sql
CREATE ROLE pgtriage_reader LOGIN PASSWORD 'secure_password';
GRANT pg_read_all_stats TO pgtriage_reader;
GRANT USAGE ON SCHEMA public TO pgtriage_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pgtriage_reader;
```

### Use

```
> audit my database

> check table health for the users table

> are there any unused indexes?

> review my PostgreSQL configuration

> find slow queries
```

## Tools

### `full_audit`
Run a comprehensive performance audit covering table health, slow queries, index health, and configuration. Returns all findings sorted by severity.

### `check_table_health`
Analyze dead tuples, autovacuum stats, sequential scan ratios, and TOAST bloat. Optionally filter to a specific table.

### `analyze_slow_queries`
Pull the slowest queries from `pg_stat_statements`, run `EXPLAIN ANALYZE` on each, and detect patterns like sequential scans, stale statistics, and N+1 queries.

### `check_index_health`
Find unused indexes (zero scans), duplicate indexes (same column definition), and tables that likely need indexes based on scan patterns.

### `check_config`
Review PostgreSQL settings (`shared_buffers`, `work_mem`, `autovacuum_vacuum_scale_factor`, `random_page_cost`, etc.) and flag suboptimal values. Checks connection utilization and long-running queries.

## Resources

| Resource | Description |
|---|---|
| `pgtriage://status` | Connection status, PostgreSQL version, loaded extensions |
| `pgtriage://tables` | All tables with sizes and approximate row counts |

## Requirements

- Python 3.11+
- PostgreSQL 12+
- `pg_stat_statements` extension (recommended for slow query analysis, not required for other tools)
- Database user with read access to `pg_stat_*` views

## Safety

pgtriage never writes to your database. Three independent layers enforce this:

1. **Session-level read-only:** `SET default_transaction_read_only = true` on every connection. PostgreSQL rejects any write attempt at the server level.
2. **Query validation:** EXPLAIN ANALYZE only runs on SELECT statements. INSERT, UPDATE, DELETE, DROP, SELECT INTO, SELECT FOR UPDATE, and stacked queries are all rejected before execution.
3. **Transaction rollback:** Every EXPLAIN ANALYZE runs inside an explicit BEGIN/ROLLBACK block with a 10-second `statement_timeout`. Even if layers 1 and 2 somehow fail, nothing is committed and long-running queries are killed.

Additionally:
- Connection strings are never exposed in tool outputs
- All database access is single-connection, no pooling

## Development

```bash
git clone https://github.com/pgtriage/pgtriage.git
cd pgtriage
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

MIT
