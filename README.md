# pgaudit

MCP server for PostgreSQL performance auditing. Connect it to Claude Code (or any MCP client) and say "audit my database" to get actionable performance findings with exact fixes.

## How it works

```
Claude Code (AI interpretation)
       |  MCP (stdio)
       v
pgaudit (data collection + pattern detection)
       |  psycopg3 (read-only)
       v
PostgreSQL database
```

pgaudit connects to your PostgreSQL database and exposes performance auditing tools via the Model Context Protocol. It collects metrics from PostgreSQL system views, runs deterministic pattern detection, and returns structured findings. The MCP client provides the AI layer, interpreting results and explaining fixes in plain English.

No API keys required. No AI costs. The intelligence comes from your MCP client.

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
pip install pgaudit
```

### Configure Claude Code

Add to your MCP settings (`.claude/settings.json` or project settings):

```json
{
  "mcpServers": {
    "pgaudit": {
      "command": "python",
      "args": ["-m", "pgaudit"],
      "env": {
        "PGAUDIT_CONNECTION_STRING": "postgres://user:pass@localhost:5432/dbname"
      }
    }
  }
}
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
| `pgaudit://status` | Connection status, PostgreSQL version, loaded extensions |
| `pgaudit://tables` | All tables with sizes and approximate row counts |

## Requirements

- Python 3.11+
- PostgreSQL 12+
- `pg_stat_statements` extension (recommended for slow query analysis, not required for other tools)
- Database user with read access to `pg_stat_*` views

## Safety

- All connections enforce `SET default_transaction_read_only = true`
- `EXPLAIN ANALYZE` only runs on `SELECT` queries (validated before execution)
- Connection strings are never exposed in tool outputs
- Single read-only connection, no write operations

## Development

```bash
git clone https://github.com/manas-maheshwari/pgaudit.git
cd pgaudit
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

MIT
