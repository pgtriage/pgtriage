import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from pgtriage.analyzers.config_rules import analyze_config
from pgtriage.analyzers.explain import (
    detect_plan_issues,
    plan_references_schema,
    run_explain_analyze,
)
from pgtriage.analyzers.index_health import analyze_duplicate_indexes
from pgtriage.analyzers.patterns import (
    analyze_dead_tuples,
    analyze_sequential_scans,
    analyze_table_bloat,
    analyze_vacuum_staleness,
)
from pgtriage.audit_runs import (
    AuditRunConflictError,
    AuditRunStatus,
    AuditRunStore,
    database_target_hash,
)
from pgtriage.collectors.config import collect_config_settings, collect_connection_stats
from pgtriage.collectors.index_health import (
    collect_duplicate_indexes,
    collect_index_metadata,
    collect_tables_needing_indexes,
    collect_unused_indexes,
)
from pgtriage.collectors.schemas import validate_schema_name
from pgtriage.collectors.slow_queries import (
    collect_n_plus_one_candidates,
    collect_slow_queries,
    is_pg_stat_statements_available,
    is_pg_stat_statements_loaded,
)
from pgtriage.collectors.table_health import collect_table_sizes, collect_table_stats
from pgtriage.connection import ConnectionManager
from pgtriage.models import AuditResult, Category, Finding, Severity

READ_ONLY_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
)
READ_ONLY_DIAGNOSTIC = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
)


@dataclass
class AppContext:
    db: ConnectionManager
    audit_runs: AuditRunStore
    database_identity_hash: str


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    conn_string = os.environ.get("PGTRIAGE_CONNECTION_STRING", "")
    if not conn_string:
        conn_string = os.environ.get("PGAUDIT_CONNECTION_STRING", "")
        if conn_string:
            print(
                "WARNING: PGAUDIT_CONNECTION_STRING is deprecated, use "
                "PGTRIAGE_CONNECTION_STRING instead. Support for the old name "
                "will be removed in a future release.",
                file=sys.stderr,
            )
    if not conn_string:
        raise RuntimeError(
            "PGTRIAGE_CONNECTION_STRING environment variable is required. "
            "Example: postgres://user:pass@localhost:5432/dbname"
        )

    db = ConnectionManager(conn_string)
    audit_runs = AuditRunStore(
        retention_hours=_positive_env_int(
            "PGTRIAGE_AUDIT_RETENTION_HOURS",
            24,
        ),
        tombstone_hours=_positive_env_int(
            "PGTRIAGE_AUDIT_TOMBSTONE_HOURS",
            24 * 7,
        ),
    )
    try:
        yield AppContext(
            db=db,
            audit_runs=audit_runs,
            database_identity_hash=database_target_hash(conn_string),
        )
    finally:
        await db.close()
        audit_runs.close()


mcp = FastMCP(
    "pgtriage",
    lifespan=app_lifespan,
    instructions=(
        "PostgreSQL performance auditing server. "
        "Use 'full_audit' for a comprehensive review, or individual tools "
        "like 'check_table_health', 'analyze_slow_queries', 'check_index_health', "
        "and 'check_config' for targeted analysis."
    ),
)


@mcp.tool(annotations=READ_ONLY_IDEMPOTENT)
async def check_table_health(
    table_name: str | None = None,
    schema_name: str | None = None,
) -> dict:
    """Check table health: dead tuples, bloat, vacuum stats, scan ratios.
    Optionally filter to a specific user schema and/or table by name."""
    ctx = mcp.get_context()
    db = ctx.request_context.lifespan_context.db

    await validate_schema_name(db, schema_name)
    table_stats = await collect_table_stats(db, table_name, schema_name)
    table_sizes = await collect_table_sizes(db, schema_name)

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


@mcp.tool(annotations=READ_ONLY_DIAGNOSTIC)
async def analyze_slow_queries(
    limit: int = 10,
    min_calls: int = 5,
    schema_name: str | None = None,
) -> dict:
    """Find and analyze slow queries from pg_stat_statements.
    Runs EXPLAIN ANALYZE on top slow SELECT queries and detects
    performance patterns like sequential scans and missing indexes."""
    ctx = mcp.get_context()
    db = ctx.request_context.lifespan_context.db
    await validate_schema_name(db, schema_name)

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
    try:
        index_metadata = await collect_index_metadata(db, schema_name)
    except psycopg.Error:
        # Catalog metadata enriches plan findings but must never make the
        # existing slow-query audit unavailable on a restricted database role.
        index_metadata = []
    findings = []
    plans_by_query: dict[str, list[dict] | None] = {}
    queries_in_scope = 0

    for sq in slow_queries:
        query_text = sq.get("query", "")
        plan = await run_explain_analyze(db, query_text)
        plans_by_query[query_text] = plan
        if schema_name and (
            not plan or not plan_references_schema(plan, schema_name)
        ):
            continue
        queries_in_scope += 1
        if plan:
            plan_findings = detect_plan_issues(
                plan,
                query_text,
                index_metadata=index_metadata,
                schema_name=schema_name,
            )
            for f in plan_findings:
                f.evidence["mean_exec_time_ms"] = sq.get("mean_exec_time_ms")
                f.evidence["total_exec_time_ms"] = sq.get("total_exec_time_ms")
                f.evidence["calls"] = sq.get("calls")
                f.evidence["cache_hit_pct"] = sq.get("cache_hit_pct")
            findings.extend(plan_findings)

    n_plus_one = await collect_n_plus_one_candidates(db)
    for npo in n_plus_one:
        query_text = npo.get("query", "")
        if schema_name:
            if query_text not in plans_by_query:
                plans_by_query[query_text] = await run_explain_analyze(
                    db,
                    query_text,
                )
            plan = plans_by_query[query_text]
            if not plan or not plan_references_schema(plan, schema_name):
                continue
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
        queries_analyzed=(
            queries_in_scope if schema_name else len(slow_queries)
        ),
    )
    return result.model_dump()


@mcp.tool(annotations=READ_ONLY_IDEMPOTENT)
async def check_index_health(
    schema_name: str | None = None,
) -> dict:
    """Find unused indexes, duplicate indexes, and missing index
    opportunities based on sequential scan patterns."""
    ctx = mcp.get_context()
    db = ctx.request_context.lifespan_context.db
    await validate_schema_name(db, schema_name)

    unused = await collect_unused_indexes(db, schema_name)
    duplicates = await collect_duplicate_indexes(db, schema_name)
    needs_indexes = await collect_tables_needing_indexes(db, schema_name)

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
            suggested_fix=(
                "Review the index definition, constraints, statistics-reset time, "
                "replica usage, and representative query plans before considering: "
                f"DROP INDEX CONCURRENTLY {idx['index_name']};"
            ),
            safe_to_apply=False,
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

    findings.extend(analyze_duplicate_indexes(duplicates))

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


@mcp.tool(annotations=READ_ONLY_IDEMPOTENT)
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


@mcp.tool(annotations=READ_ONLY_DIAGNOSTIC)
async def full_audit(
    slow_query_limit: int = 10,
    schema_name: str | None = None,
    audit_run_id: str | None = None,
) -> dict:
    """Run a comprehensive performance audit: table health,
    slow queries, index health, and configuration review.
    Returns a unified report with all findings sorted by severity.

    Optionally scope table, slow-query, and index findings to one user schema.
    Configuration findings remain database-wide. Omitting schema_name audits
    all non-system schemas.
    """
    ctx = mcp.get_context()
    app = ctx.request_context.lifespan_context
    db = app.db
    store = app.audit_runs

    postgres_server_version = await db.get_server_version()
    try:
        statistics_reset_at = await db.get_statistics_reset_at()
    except psycopg.Error:
        statistics_reset_at = None

    try:
        audit_run, created = store.claim(
            audit_run_id=audit_run_id,
            database_identity_hash=app.database_identity_hash,
            schema_name=schema_name,
            slow_query_limit=slow_query_limit,
            postgres_server_version=postgres_server_version,
            statistics_reset_at=statistics_reset_at,
        )
    except AuditRunConflictError as error:
        raise ValueError(str(error)) from error

    if not created:
        if audit_run.status is AuditRunStatus.COMPLETED and audit_run.result:
            return audit_run.result
        if audit_run.status is AuditRunStatus.STARTED:
            raise RuntimeError(
                f"Audit run '{audit_run.audit_run_id}' is already in progress. "
                "Use get_audit_run to reconcile it."
            )
        if audit_run.status is AuditRunStatus.EXPIRED:
            raise RuntimeError(
                f"Audit run '{audit_run.audit_run_id}' has expired. "
                "An explicit new run ID is required."
            )
        raise RuntimeError(
            f"Audit run '{audit_run.audit_run_id}' previously failed. "
            "Use get_audit_run to inspect its bounded failure state."
        )

    try:
        result_data = await _execute_full_audit(
            slow_query_limit=slow_query_limit,
            schema_name=schema_name,
        )
        completed = store.complete(audit_run.audit_run_id, result_data)
        if completed.result is None:
            raise RuntimeError("Completed audit run is missing its persisted result.")
        return completed.result
    except Exception as error:
        store.fail(
            audit_run.audit_run_id,
            code=type(error).__name__.upper()[:100],
            reason=(
                "Audit execution failed before a recoverable result was recorded."
            ),
        )
        raise


async def _execute_full_audit(
    *,
    slow_query_limit: int,
    schema_name: str | None,
) -> dict:
    all_findings = []
    sections = {}

    table_result = await check_table_health(schema_name=schema_name)
    sections["table_health"] = table_result
    all_findings.extend(
        Finding(**f) for f in table_result.get("findings", [])
    )

    query_result = await analyze_slow_queries(
        limit=slow_query_limit,
        schema_name=schema_name,
    )
    sections["slow_queries"] = query_result
    all_findings.extend(
        Finding(**f) for f in query_result.get("findings", [])
    )

    index_result = await check_index_health(schema_name=schema_name)
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


@mcp.tool(annotations=READ_ONLY_IDEMPOTENT)
async def get_audit_run(audit_run_id: str) -> dict:
    """Retrieve durable status and evidence for a known full-audit operation.

    This tool never starts or repeats database diagnostic work.
    """
    ctx = mcp.get_context()
    app = ctx.request_context.lifespan_context
    record = app.audit_runs.get(audit_run_id)
    if record is None:
        return {
            "audit_run_id": audit_run_id,
            "status": "NOT_FOUND",
            "provenance": None,
            "result": None,
            "failure": None,
        }
    if record.database_identity_hash != app.database_identity_hash:
        return {
            "audit_run_id": record.audit_run_id,
            "status": "CONFLICT",
            "provenance": None,
            "result": None,
            "failure": None,
        }
    return record.lookup_payload()


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


@mcp.resource("pgtriage://status")
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


@mcp.resource("pgtriage://tables")
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
