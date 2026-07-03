from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os

from mcp.server.fastmcp import FastMCP

from pgaudit.analyzers.config_rules import analyze_config
from pgaudit.analyzers.explain import detect_plan_issues, run_explain_analyze
from pgaudit.analyzers.patterns import (
    analyze_dead_tuples,
    analyze_sequential_scans,
    analyze_table_bloat,
    analyze_vacuum_staleness,
)
from pgaudit.collectors.config import collect_config_settings, collect_connection_stats
from pgaudit.collectors.index_health import (
    collect_duplicate_indexes,
    collect_tables_needing_indexes,
    collect_unused_indexes,
)
from pgaudit.collectors.slow_queries import (
    collect_n_plus_one_candidates,
    collect_slow_queries,
    is_pg_stat_statements_available,
    is_pg_stat_statements_loaded,
)
from pgaudit.collectors.table_health import collect_table_sizes, collect_table_stats
from pgaudit.connection import ConnectionManager
from pgaudit.models import AuditResult, Category, Finding, Severity


@dataclass
class AppContext:
    db: ConnectionManager


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    conn_string = os.environ.get("PGAUDIT_CONNECTION_STRING", "")
    if not conn_string:
        raise RuntimeError(
            "PGAUDIT_CONNECTION_STRING environment variable is required. "
            "Example: postgres://user:pass@localhost:5432/dbname"
        )

    db = ConnectionManager(conn_string)
    try:
        yield AppContext(db=db)
    finally:
        await db.close()


mcp = FastMCP(
    "pgaudit",
    lifespan=app_lifespan,
    instructions=(
        "PostgreSQL performance auditing server. "
        "Use 'full_audit' for a comprehensive review, or individual tools "
        "like 'check_table_health', 'analyze_slow_queries', 'check_index_health', "
        "and 'check_config' for targeted analysis."
    ),
)


@mcp.tool()
async def check_table_health(
    table_name: str | None = None,
) -> dict:
    """Check table health: dead tuples, bloat, vacuum stats, scan ratios.
    Optionally filter to a specific table by name."""
    ctx = mcp.get_context()
    db = ctx.request_context.lifespan_context.db

    table_stats = await collect_table_stats(db, table_name)
    table_sizes = await collect_table_sizes(db)

    findings = []
    findings.extend(analyze_dead_tuples(table_stats))
    findings.extend(analyze_sequential_scans(table_stats))
    findings.extend(analyze_vacuum_staleness(table_stats))
    findings.extend(analyze_table_bloat(table_sizes))

    result = AuditResult.from_findings(
        findings,
        tables_analyzed=len(table_stats),
    )
    return result.model_dump()


@mcp.tool()
async def analyze_slow_queries(
    limit: int = 10,
    min_calls: int = 5,
) -> dict:
    """Find and analyze slow queries from pg_stat_statements.
    Runs EXPLAIN ANALYZE on top slow SELECT queries and detects
    performance patterns like sequential scans and missing indexes."""
    ctx = mcp.get_context()
    db = ctx.request_context.lifespan_context.db

    if not await is_pg_stat_statements_available(db):
        no_ext = Finding(
            severity=Severity.INFO,
            category=Category.CONFIG_ISSUE,
            detail=(
                "pg_stat_statements extension is not installed. "
                "This extension is required to identify slow queries. "
                "Install it with: CREATE EXTENSION pg_stat_statements; "
                "and add 'pg_stat_statements' to shared_preload_libraries in postgresql.conf."
            ),
            suggested_fix="CREATE EXTENSION pg_stat_statements;",
        )
        return AuditResult.from_findings([no_ext]).model_dump()

    if not await is_pg_stat_statements_loaded(db):
        not_loaded = Finding(
            severity=Severity.MEDIUM,
            category=Category.CONFIG_ISSUE,
            detail=(
                "pg_stat_statements extension is installed but not loaded. "
                "Slow query analysis is unavailable without it. "
                "Add 'pg_stat_statements' to shared_preload_libraries in postgresql.conf "
                "and restart PostgreSQL."
            ),
            suggested_fix=(
                "Add to postgresql.conf: shared_preload_libraries = 'pg_stat_statements'\n"
                "Then restart PostgreSQL."
            ),
        )
        return AuditResult.from_findings([not_loaded]).model_dump()

    slow_queries = await collect_slow_queries(db, limit, min_calls)
    findings = []

    for sq in slow_queries:
        query_text = sq.get("query", "")
        plan = await run_explain_analyze(db, query_text)
        if plan:
            plan_findings = detect_plan_issues(plan, query_text)
            for f in plan_findings:
                f.evidence["mean_exec_time_ms"] = sq.get("mean_exec_time_ms")
                f.evidence["total_exec_time_ms"] = sq.get("total_exec_time_ms")
                f.evidence["calls"] = sq.get("calls")
                f.evidence["cache_hit_pct"] = sq.get("cache_hit_pct")
            findings.extend(plan_findings)

    n_plus_one = await collect_n_plus_one_candidates(db)
    for npo in n_plus_one:
        findings.append(Finding(
            severity=Severity.MEDIUM,
            category=Category.N_PLUS_ONE,
            query=npo.get("query"),
            detail=(
                f"Query called {npo['calls']:,} times with {npo['mean_exec_time_ms']}ms avg. "
                f"Total time: {npo['total_exec_time_ms']:,}ms. "
                f"High call count with low individual latency suggests an N+1 pattern."
            ),
            estimated_impact=(
                f"Batching could reduce {npo['calls']:,} calls to a single query"
            ),
            evidence={
                "calls": npo["calls"],
                "mean_exec_time_ms": npo["mean_exec_time_ms"],
                "total_exec_time_ms": npo["total_exec_time_ms"],
            },
        ))

    result = AuditResult.from_findings(
        findings,
        queries_analyzed=len(slow_queries),
    )
    return result.model_dump()


@mcp.tool()
async def check_index_health(
    schema_name: str = "public",
) -> dict:
    """Find unused indexes, duplicate indexes, and missing index
    opportunities based on sequential scan patterns."""
    ctx = mcp.get_context()
    db = ctx.request_context.lifespan_context.db

    unused = await collect_unused_indexes(db, schema_name)
    duplicates = await collect_duplicate_indexes(db, schema_name)
    needs_indexes = await collect_tables_needing_indexes(db)

    findings = []
    total_indexes = len(unused)
    max_unused_detail = 10

    sorted_unused = sorted(unused, key=lambda x: x.get("index_size_bytes", 0), reverse=True)

    for idx in sorted_unused[:max_unused_detail]:
        findings.append(Finding(
            severity=Severity.LOW,
            category=Category.UNUSED_INDEX,
            table=idx["table_name"],
            index=idx["index_name"],
            detail=(
                f"Index '{idx['index_name']}' on '{idx['table_name']}' has 0 scans "
                f"since last stats reset. Size: {idx['index_size']}."
            ),
            suggested_fix=f"DROP INDEX CONCURRENTLY {idx['index_name']};",
            safe_to_apply=True,
            evidence={
                "index_definition": idx["index_definition"],
                "index_size": idx["index_size"],
                "index_size_bytes": idx["index_size_bytes"],
            },
        ))

    if len(sorted_unused) > max_unused_detail:
        remaining = len(sorted_unused) - max_unused_detail
        remaining_size = sum(x.get("index_size_bytes", 0) for x in sorted_unused[max_unused_detail:])
        remaining_names = [x["index_name"] for x in sorted_unused[max_unused_detail:]]
        findings.append(Finding(
            severity=Severity.LOW,
            category=Category.UNUSED_INDEX,
            detail=(
                f"{remaining} more unused indexes not shown (total wasted: "
                f"{remaining_size // 1024} kB). Run check_index_health for the full list."
            ),
            evidence={"remaining_indexes": remaining_names},
        ))

    for dup in duplicates:
        findings.append(Finding(
            severity=Severity.MEDIUM,
            category=Category.DUPLICATE_INDEX,
            table=str(dup["table_name"]),
            detail=(
                f"Duplicate indexes on '{dup['table_name']}': "
                f"'{dup['index_1']}' ({dup['index_1_size']}) and "
                f"'{dup['index_2']}' ({dup['index_2_size']}). "
                f"Same column definition. One can be dropped."
            ),
            suggested_fix=(
                f"-- Keep the one with more scans, drop the other:\n"
                f"-- {dup['index_1_def']}\n"
                f"-- {dup['index_2_def']}"
            ),
            evidence={
                "index_1_def": dup["index_1_def"],
                "index_2_def": dup["index_2_def"],
            },
        ))

    for tbl in needs_indexes:
        findings.append(Finding(
            severity=Severity.MEDIUM,
            category=Category.MISSING_INDEX,
            table=tbl["table_name"],
            detail=(
                f"Table '{tbl['table_name']}' has {tbl['n_live_tup']:,} rows "
                f"with {tbl['seq_scan']:,} seq scans vs {tbl['idx_scan']:,} idx scans. "
                f"Avg {tbl['avg_rows_per_seq_scan']:,} rows per seq scan. "
                f"Likely missing an index on frequently queried columns."
            ),
            suggested_fix=(
                f"Identify common WHERE clauses on {tbl['table_name']} and add: "
                f"CREATE INDEX CONCURRENTLY ON {tbl['table_name']} (...);"
            ),
            evidence={
                "live_rows": tbl["n_live_tup"],
                "seq_scans": tbl["seq_scan"],
                "idx_scans": tbl["idx_scan"],
                "avg_rows_per_seq_scan": tbl["avg_rows_per_seq_scan"],
            },
        ))

    result = AuditResult.from_findings(
        findings,
        indexes_analyzed=total_indexes + len(duplicates),
    )
    return result.model_dump()


@mcp.tool()
async def check_config() -> dict:
    """Review PostgreSQL configuration settings and flag
    suboptimal values for shared_buffers, work_mem,
    max_connections, and autovacuum parameters."""
    ctx = mcp.get_context()
    db = ctx.request_context.lifespan_context.db

    settings = await collect_config_settings(db)
    conn_stats = await collect_connection_stats(db)

    findings = analyze_config(settings, conn_stats)

    settings_dict = {s["name"]: s["setting"] for s in settings}
    result_data = AuditResult.from_findings(findings).model_dump()
    result_data["settings"] = settings_dict
    if conn_stats:
        result_data["connections"] = conn_stats
    return result_data


@mcp.tool()
async def full_audit(
    slow_query_limit: int = 10,
) -> dict:
    """Run a comprehensive performance audit: table health,
    slow queries, index health, and configuration review.
    Returns a unified report with all findings sorted by severity."""
    ctx = mcp.get_context()
    db = ctx.request_context.lifespan_context.db

    all_findings = []
    sections = {}

    table_result = await check_table_health()
    sections["table_health"] = table_result
    all_findings.extend(
        Finding(**f) for f in table_result.get("findings", [])
    )

    query_result = await analyze_slow_queries(limit=slow_query_limit)
    sections["slow_queries"] = query_result
    all_findings.extend(
        Finding(**f) for f in query_result.get("findings", [])
    )

    index_result = await check_index_health()
    sections["index_health"] = index_result
    all_findings.extend(
        Finding(**f) for f in index_result.get("findings", [])
    )

    config_result = await check_config()
    sections["config"] = config_result
    all_findings.extend(
        Finding(**f) for f in config_result.get("findings", [])
    )

    tables_analyzed = table_result.get("summary", {}).get("tables_analyzed", 0)
    queries_analyzed = query_result.get("summary", {}).get("queries_analyzed", 0)
    indexes_analyzed = index_result.get("summary", {}).get("indexes_analyzed", 0)

    result = AuditResult.from_findings(
        all_findings,
        tables_analyzed=tables_analyzed,
        queries_analyzed=queries_analyzed,
        indexes_analyzed=indexes_analyzed,
    )
    result_data = result.model_dump()
    result_data["sections"] = sections
    return result_data


@mcp.resource("pgaudit://status")
async def get_status() -> str:
    """Connection status, database version, loaded extensions."""
    ctx = mcp.get_context()
    db = ctx.request_context.lifespan_context.db

    version = await db.get_server_version()
    has_pgss = await db.has_extension("pg_stat_statements")

    extensions = await db.fetch_all(
        "SELECT extname, extversion FROM pg_extension ORDER BY extname"
    )
    ext_list = ", ".join(f"{e['extname']} ({e['extversion']})" for e in extensions)

    return (
        f"PostgreSQL version: {version}\n"
        f"pg_stat_statements: {'installed' if has_pgss else 'NOT installed'}\n"
        f"Extensions: {ext_list}\n"
    )


@mcp.resource("pgaudit://tables")
async def get_tables() -> str:
    """List all tables with sizes and approximate row counts."""
    ctx = mcp.get_context()
    db = ctx.request_context.lifespan_context.db

    rows = await db.fetch_all("""
        SELECT
            n.nspname AS schema,
            c.relname AS table_name,
            pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
            c.reltuples::bigint AS approx_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY pg_total_relation_size(c.oid) DESC
    """)

    lines = ["schema | table | size | ~rows", "---|---|---|---"]
    for r in rows:
        lines.append(
            f"{r['schema']} | {r['table_name']} | {r['total_size']} | {r['approx_rows']:,}"
        )
    return "\n".join(lines)
