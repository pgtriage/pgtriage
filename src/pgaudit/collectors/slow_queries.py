"""Collect slow query data from pg_stat_statements."""

import psycopg

from pgaudit.connection import ConnectionManager

SLOW_QUERIES_QUERY = """
SELECT
    queryid,
    query,
    calls,
    round(total_exec_time::numeric, 2) AS total_exec_time_ms,
    round(mean_exec_time::numeric, 2) AS mean_exec_time_ms,
    round(min_exec_time::numeric, 2) AS min_exec_time_ms,
    round(max_exec_time::numeric, 2) AS max_exec_time_ms,
    round(stddev_exec_time::numeric, 2) AS stddev_exec_time_ms,
    rows,
    shared_blks_hit,
    shared_blks_read,
    CASE WHEN shared_blks_hit + shared_blks_read > 0
         THEN round(
              shared_blks_hit::numeric /
              (shared_blks_hit + shared_blks_read) * 100, 2
         ) ELSE 0 END AS cache_hit_pct,
    temp_blks_read,
    temp_blks_written
FROM pg_stat_statements
WHERE calls >= %s
  AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY total_exec_time DESC
LIMIT %s
"""

N_PLUS_ONE_QUERY = """
SELECT
    queryid,
    query,
    calls,
    round(mean_exec_time::numeric, 2) AS mean_exec_time_ms,
    round(total_exec_time::numeric, 2) AS total_exec_time_ms,
    rows
FROM pg_stat_statements
WHERE calls > 1000
  AND mean_exec_time < 5
  AND dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY calls DESC
LIMIT 20
"""


async def is_pg_stat_statements_available(db: ConnectionManager) -> bool:
    return await db.has_extension("pg_stat_statements")


async def collect_slow_queries(
    db: ConnectionManager,
    limit: int = 10,
    min_calls: int = 5,
) -> list[dict]:
    try:
        return await db.fetch_all(SLOW_QUERIES_QUERY, (min_calls, limit))
    except psycopg.errors.ObjectNotInPrerequisiteState:
        return []


async def is_pg_stat_statements_loaded(db: ConnectionManager) -> bool:
    """Check if pg_stat_statements is both installed AND loaded in shared_preload_libraries."""
    try:
        await db.fetch_one("SELECT 1 FROM pg_stat_statements LIMIT 1")
        return True
    except (psycopg.errors.ObjectNotInPrerequisiteState, psycopg.errors.UndefinedTable):
        return False


async def collect_n_plus_one_candidates(db: ConnectionManager) -> list[dict]:
    return await db.fetch_all(N_PLUS_ONE_QUERY)
