"""Collect index health metrics from PostgreSQL system views."""

from pgtriage.connection import ConnectionManager

UNUSED_INDEXES_QUERY = """
SELECT
    s.schemaname,
    s.relname AS table_name,
    s.indexrelname AS index_name,
    s.idx_scan AS scans_since_reset,
    pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
    pg_relation_size(s.indexrelid) AS index_size_bytes,
    pg_get_indexdef(i.indexrelid) AS index_definition,
    i.indisunique AS is_unique,
    i.indisprimary AS is_primary
FROM pg_stat_user_indexes s
JOIN pg_index i ON s.indexrelid = i.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisunique
  AND NOT i.indisprimary
  AND s.schemaname = %s
ORDER BY pg_relation_size(s.indexrelid) DESC
"""

DUPLICATE_INDEXES_QUERY = """
SELECT
    a.indrelid::regclass AS table_name,
    a.indexrelid::regclass AS index_1,
    b.indexrelid::regclass AS index_2,
    pg_get_indexdef(a.indexrelid) AS index_1_def,
    pg_get_indexdef(b.indexrelid) AS index_2_def,
    pg_size_pretty(pg_relation_size(a.indexrelid)) AS index_1_size,
    pg_size_pretty(pg_relation_size(b.indexrelid)) AS index_2_size,
    pg_relation_size(a.indexrelid) AS index_1_size_bytes,
    pg_relation_size(b.indexrelid) AS index_2_size_bytes
FROM pg_index a
JOIN pg_index b ON a.indrelid = b.indrelid
    AND a.indexrelid < b.indexrelid
    AND a.indkey::text = b.indkey::text
JOIN pg_namespace n ON n.oid = (
    SELECT relnamespace FROM pg_class WHERE oid = a.indrelid
)
WHERE n.nspname = %s
ORDER BY pg_relation_size(a.indexrelid) + pg_relation_size(b.indexrelid) DESC
"""

TABLES_NEEDING_INDEXES_QUERY = """
SELECT
    schemaname,
    relname AS table_name,
    seq_scan,
    idx_scan,
    n_live_tup,
    seq_tup_read,
    CASE WHEN seq_scan > 0
         THEN seq_tup_read / seq_scan
         ELSE 0 END AS avg_rows_per_seq_scan
FROM pg_stat_user_tables
WHERE n_live_tup > 100000
  AND seq_scan > idx_scan
  AND seq_scan > 100
ORDER BY seq_tup_read DESC
LIMIT 20
"""


async def collect_unused_indexes(
    db: ConnectionManager,
    schema_name: str = "public",
) -> list[dict]:
    return await db.fetch_all(UNUSED_INDEXES_QUERY, (schema_name,))


async def collect_duplicate_indexes(
    db: ConnectionManager,
    schema_name: str = "public",
) -> list[dict]:
    return await db.fetch_all(DUPLICATE_INDEXES_QUERY, (schema_name,))


async def collect_tables_needing_indexes(
    db: ConnectionManager,
) -> list[dict]:
    return await db.fetch_all(TABLES_NEEDING_INDEXES_QUERY)
