# pgtriage

[![CI](https://github.com/pgtriage/pgtriage/actions/workflows/ci.yml/badge.svg)](https://github.com/pgtriage/pgtriage/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pgtriage)](https://pypi.org/project/pgtriage/)
[![Python](https://img.shields.io/pypi/pyversions/pgtriage)](https://pypi.org/project/pgtriage/)
[![License](https://img.shields.io/github/license/pgtriage/pgtriage)](LICENSE)

![pgtriage: PostgreSQL performance auditing over MCP](https://raw.githubusercontent.com/pgtriage/pgtriage/master/assets/pgtriage_hero_v5_1280x640_github.png)

Read-only MCP server for PostgreSQL performance auditing. Connect it to Claude Code (or any MCP client) and say "audit my database" to get structured findings with evidence and suggested fixes.

> Not related to the [pgAudit](https://www.pgaudit.org/) logging extension. pgtriage does performance triage, not compliance logging.

<!-- mcp-name: io.github.pgtriage/pgtriage -->

## Why I built this

Built after diagnosing implicit type casts and missing indexes on multi-million-row tables in production fintech systems. The fixes were simple (one `CREATE INDEX CONCURRENTLY` statement each), but finding them required reading query plans most engineers never look at. A real production audit surfaced 88 findings across 118 tables. pgtriage automates that diagnostic process and lets any AI client explain the results.

## How it works

![MCP clients call pgtriage, which collects read-only PostgreSQL evidence and returns structured findings](https://raw.githubusercontent.com/pgtriage/pgtriage/master/assets/pgtriage-architecture.png)

pgtriage connects to your PostgreSQL database and exposes performance auditing tools via the Model Context Protocol. It collects metrics from PostgreSQL system views, runs deterministic pattern detection, and returns structured findings. The MCP client provides the AI layer, interpreting results and explaining fixes in plain English.

No API keys required. No AI costs. No vendor lock-in. The intelligence comes from your MCP client.

## Example output

Example audit: **283 tables scanned, 147 findings** — 1 critical connection-pressure finding, 19 medium duplicate-index findings, 64 low unused-index findings, and 63 more across seven categories.

```json
{
  "severity": "critical",
  "category": "connection_pressure",
  "detail": "Ordinary client connection utilization at 99% (96/97 usable slots; 100 max, 3 reserved). Ordinary connection capacity is effectively exhausted.",
  "suggested_fix": "Consider using a connection pooler (PgBouncer) or increasing max_connections if RAM allows.",
  "evidence": {
    "total_connections": 96,
    "max_connections": 100,
    "reserved_connections": 3,
    "ordinary_connection_capacity": 97,
    "ordinary_slots_available": 1,
    "utilization_pct": 99.0
  }
}
```

```json
{
  "severity": "medium",
  "category": "duplicate_index",
  "table": "orders",
  "detail": "Structurally equivalent indexes on 'public.orders': 'public.idx_orders_customer' (2 GB, 18,420 scans), 'public.idx_orders_customer_copy' (2.1 GB, 12 scans). 'public.idx_orders_customer' has the uniquely highest recorded scan count; the remaining eligible indexes are drop candidates subject to workload review.",
  "suggested_fix": "Review every compared definition, current and periodic workload, and replica usage before considering:\nDROP INDEX CONCURRENTLY \"public\".\"idx_orders_customer_copy\";\nDROP INDEX CONCURRENTLY cannot run inside a transaction block.",
  "safe_to_apply": false,
  "evidence": {
    "structurally_equivalent": true,
    "statistics_reset_at": "2026-10-01T00:00:00+00:00",
    "minimum_statistics_age_days": 7,
    "keep_index": {
      "qualified_name": "public.idx_orders_customer",
      "scans": 18420,
      "size": "2 GB",
      "definition": "CREATE INDEX idx_orders_customer ON public.orders USING btree (customer_id)"
    },
    "drop_candidates": [
      {
        "qualified_name": "public.idx_orders_customer_copy",
        "scans": 12,
        "size": "2.1 GB",
        "definition": "CREATE INDEX idx_orders_customer_copy ON public.orders USING btree (customer_id)",
        "sql": "DROP INDEX CONCURRENTLY \"public\".\"idx_orders_customer_copy\";"
      }
    ]
  }
}
```

## What it finds

- **Sequential scans on large tables** with missing index suggestions
- **Type casts on indexed columns** that suppress index usage, verified against
  PostgreSQL catalog metadata and the observed query plan
- **Dead tuple buildup** and autovacuum health issues
- **Unused and duplicate indexes** wasting disk and slowing writes
- **N+1 query patterns** from pg_stat_statements analysis
- **Stale table statistics** causing bad query plans
- **TOAST table bloat** from large JSONB/TEXT columns
- **Configuration issues** (shared_buffers, work_mem, autovacuum tuning)
- **Connection pressure** approaching max_connections
- **Long-running queries** holding locks

See the [public roadmap](ROADMAP.md) for current priorities and non-goals.

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

> audit only the accounts schema
```

## Demo

![pgtriage auditing PostgreSQL through an MCP client](https://raw.githubusercontent.com/pgtriage/pgtriage/master/assets/pgtriage_demo_v7.gif)

## Tools

### `full_audit`
Run a comprehensive performance audit covering table health, slow queries, index health, and configuration. Pass the optional `schema_name` argument to scope table, plan, and index findings to one user schema; omit it to audit every non-system schema. Configuration findings remain database-wide. Returns all findings sorted by severity.

### `check_table_health`
Analyze dead tuples, autovacuum stats, sequential scan ratios, and TOAST bloat. Optionally filter to a specific user schema and/or table.

### `analyze_slow_queries`
Pull the slowest queries from `pg_stat_statements`, inspect their execution plans, and detect patterns like indexed-column type casts, sequential scans, stale statistics, and N+1 queries. An optional `schema_name` filter uses verbose execution-plan metadata rather than parsing SQL text. Literal queries use bounded `EXPLAIN ANALYZE`; normalized queries containing placeholders use a non-executing generic plan.

### `check_index_health`
Find unused indexes, structurally equivalent duplicate-index groups, and tables
that likely need indexes based on scan patterns. Duplicate findings preserve the
compared definitions, sizes, and scan counts. pgtriage names a review candidate
only when statistics are mature, one keeper has the uniquely highest scan count,
and the candidate is not protected by a constraint or operational role.
Optionally scope the analysis to one user schema.

### `check_config`
Review PostgreSQL settings (`shared_buffers`, `work_mem`, `autovacuum_vacuum_scale_factor`, `random_page_cost`, etc.) and flag suboptimal values. Checks connection utilization and long-running queries.

### Agent runtime integration

[pgtriage Agent Lab](https://github.com/manas-maheshwari/pgtriage-agent-lab)
is a runnable TypeScript reference client that demonstrates authorization,
bounded retries, durable workflow state, and fail-closed review around this MCP
boundary.

pgtriage publishes MCP safety annotations for every tool. All tools are marked
read-only and non-destructive; tools that can run bounded `EXPLAIN ANALYZE` are
deliberately not marked idempotent. Agent runtimes must still allowlist,
authorize, and validate every call because MCP annotations are descriptive
hints, not permissions. See
[Agent Runtime Integration](docs/agent-runtime-integration.md) for the complete
contract, schema-scoped audit behavior, and retry guidance.

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

pgtriage is designed for read-only production use and does not issue write SQL. Three independent layers protect database state:

1. **Session-level read-only:** `SET default_transaction_read_only = true` on every connection. PostgreSQL rejects any write attempt at the server level.
2. **Query validation:** EXPLAIN ANALYZE only runs on SELECT statements. INSERT, UPDATE, DELETE, DROP, SELECT INTO, SELECT FOR UPDATE, and stacked queries are all rejected before execution.
3. **Transaction rollback:** Every EXPLAIN ANALYZE runs inside an explicit BEGIN/ROLLBACK block with a 10-second `statement_timeout`. Transactional database changes are rolled back and long-running queries are canceled. Rollback cannot undo external side effects triggered by database extensions or functions, so the dedicated read-only role and session-level read-only enforcement remain essential.

Additionally:
- Connection strings are never exposed in tool outputs
- Suggested fixes are advisory and are never executed by pgtriage; findings default to `safe_to_apply: false`
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
