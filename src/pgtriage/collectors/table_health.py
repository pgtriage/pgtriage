"""Collect table health metrics from PostgreSQL system views."""

from pgtriage.connection import ConnectionManager

TABLE_STATS_QUERY = """
SELECT
    schemaname,
    relname AS table_name,
    n_live_tup,
    n_dead_tup,
    CASE WHEN n_live_tup + n_dead_tup > 0
         THEN round(n_dead_tup::numeric / (n_live_tup + n_dead_tup) * 100, 2)
         ELSE 0 END AS dead_tuple_pct,
    last_vacuum,
    last_autovacuum,
    last_analyze,
    last_autoanalyze,
    vacuum_count,
    autovacuum_count,
    seq_scan,
    idx_scan,
    CASE WHEN seq_scan + idx_scan > 0
         THEN round(seq_scan::numeric / (seq_scan + idx_scan) * 100, 2)
         ELSE 0 END AS seq_scan_pct,
    n_tup_ins,
    n_tup_upd,
    n_tup_del
FROM pg_stat_user_tables
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  AND (%s::text IS NULL OR schemaname = %s)
  AND (%s::text IS NULL OR relname = %s)
ORDER BY n_dead_tup DESC
"""

TABLE_SIZES_QUERY = """
SELECT
    c.relname AS table_name,
    pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
    pg_total_relation_size(c.oid) AS total_size_bytes,
    pg_size_pretty(pg_relation_size(c.oid)) AS table_size,
    pg_relation_size(c.oid) AS table_size_bytes,
    pg_size_pretty(pg_indexes_size(c.oid)) AS indexes_size,
    pg_indexes_size(c.oid) AS indexes_size_bytes,
    pg_size_pretty(
        pg_total_relation_size(c.oid)
        - pg_relation_size(c.oid)
        - pg_indexes_size(c.oid)
    ) AS toast_size,
    (pg_total_relation_size(c.oid)
     - pg_relation_size(c.oid)
     - pg_indexes_size(c.oid)) AS toast_size_bytes
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND (%s::text IS NULL OR n.nspname = %s)
ORDER BY pg_total_relation_size(c.oid) DESC
"""


async def collect_table_stats(
    db: ConnectionManager,
    table_name: str | None = None,
    schema_name: str | None = None,
) -> list[dict]:
    return await db.fetch_all(
        TABLE_STATS_QUERY,
        (schema_name, schema_name, table_name, table_name),
    )


async def collect_table_sizes(
    db: ConnectionManager,
    schema_name: str | None = None,
) -> list[dict]:
    return await db.fetch_all(TABLE_SIZES_QUERY, (schema_name, schema_name))
