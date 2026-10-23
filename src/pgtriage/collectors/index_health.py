"""Collect index health metrics from PostgreSQL system views."""

from collections import defaultdict
from typing import Any

import psycopg

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
  AND (%s::text IS NULL OR s.schemaname = %s)
ORDER BY pg_relation_size(s.indexrelid) DESC
"""

DUPLICATE_INDEXES_QUERY = """
WITH index_data AS (
    SELECT
        indexes.indrelid AS table_oid,
        table_ns.nspname AS schema_name,
        tbl.relname AS table_name,
        index_ns.nspname AS index_schema_name,
        idx.relname AS index_name,
        idx.relkind AS index_relkind,
        access_method.amname AS access_method,
        indexes.indkey::text AS indkey,
        indexes.indoption::text AS indoption,
        indexes.indclass::text AS indclass,
        indexes.indcollation::text AS indcollation,
        indexes.indnkeyatts,
        COALESCE(pg_get_expr(indexes.indexprs, indexes.indrelid, false), '') AS index_expression,
        COALESCE(pg_get_expr(indexes.indpred, indexes.indrelid, false), '') AS index_predicate,
        pg_get_indexdef(indexes.indexrelid) AS index_definition,
        pg_size_pretty(pg_relation_size(indexes.indexrelid)) AS index_size,
        pg_relation_size(indexes.indexrelid) AS index_size_bytes,
        COALESCE(stats.idx_scan, 0) AS idx_scan,
        indexes.indisprimary AS is_primary,
        indexes.indisunique AS is_unique,
        indexes.indisexclusion AS is_exclusion,
        indexes.indisreplident AS is_replica_identity,
        indexes.indisclustered AS is_clustered,
        indexes.indisvalid AS is_valid,
        indexes.indisready AS is_ready,
        indexes.indislive AS is_live,
        EXISTS (
            SELECT 1
            FROM pg_constraint constraints
            WHERE constraints.conindid = indexes.indexrelid
        ) AS is_constraint_backed,
        database_stats.stats_reset AS statistics_reset_at
    FROM pg_index indexes
    JOIN pg_class tbl ON tbl.oid = indexes.indrelid
    JOIN pg_namespace table_ns ON table_ns.oid = tbl.relnamespace
    JOIN pg_class idx ON idx.oid = indexes.indexrelid
    JOIN pg_namespace index_ns ON index_ns.oid = idx.relnamespace
    JOIN pg_am access_method ON access_method.oid = idx.relam
    LEFT JOIN pg_stat_user_indexes stats ON stats.indexrelid = indexes.indexrelid
    LEFT JOIN pg_stat_database database_stats
        ON database_stats.datname = current_database()
    WHERE table_ns.nspname NOT IN ('pg_catalog', 'information_schema')
      AND (%s::text IS NULL OR table_ns.nspname = %s)
), duplicate_data AS (
    SELECT
        index_data.*,
        COUNT(*) OVER (
            PARTITION BY
                table_oid,
                access_method,
                indkey,
                indoption,
                indclass,
                indcollation,
                indnkeyatts,
                index_expression,
                index_predicate
        ) AS equivalent_count
    FROM index_data
)
SELECT *
FROM duplicate_data
WHERE equivalent_count > 1
ORDER BY schema_name, table_name, index_name
"""

REPLICATION_CONTEXT_QUERY = """
SELECT
    pg_is_in_recovery() AS in_recovery,
    CASE
        WHEN pg_is_in_recovery() THEN NULL
        ELSE (SELECT count(*) FROM pg_stat_replication)
    END AS visible_standbys
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
  AND (%s::text IS NULL OR schemaname = %s)
ORDER BY seq_tup_read DESC
LIMIT 20
"""

INDEX_METADATA_QUERY = """
SELECT
    n.nspname AS schema_name,
    tbl.relname AS table_name,
    attr.attname AS column_name,
    format_type(attr.atttypid, attr.atttypmod) AS column_type,
    keys.position AS index_position,
    keys.position <= indexes.indnkeyatts AS is_key_column,
    idx.relname AS index_name,
    access_method.amname AS index_method,
    pg_get_indexdef(indexes.indexrelid) AS index_definition,
    pg_get_expr(indexes.indexprs, indexes.indrelid) AS index_expression,
    pg_get_expr(indexes.indpred, indexes.indrelid) AS index_predicate,
    GREATEST(
        COALESCE(stats.n_live_tup, 0),
        COALESCE(tbl.reltuples::bigint, 0)
    ) AS table_live_rows
FROM pg_index indexes
JOIN pg_class tbl ON tbl.oid = indexes.indrelid
JOIN pg_namespace n ON n.oid = tbl.relnamespace
JOIN pg_class idx ON idx.oid = indexes.indexrelid
JOIN pg_am access_method ON access_method.oid = idx.relam
JOIN LATERAL unnest(indexes.indkey) WITH ORDINALITY AS keys(attnum, position)
    ON true
LEFT JOIN pg_attribute attr
    ON attr.attrelid = tbl.oid
   AND attr.attnum = keys.attnum
   AND keys.attnum > 0
LEFT JOIN pg_stat_user_tables stats
    ON stats.schemaname = n.nspname
   AND stats.relname = tbl.relname
WHERE indexes.indisvalid
  AND indexes.indisready
  AND tbl.relkind IN ('r', 'p', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND (%s::text IS NULL OR n.nspname = %s)
ORDER BY n.nspname, tbl.relname, idx.relname, keys.position
"""


async def collect_unused_indexes(
    db: ConnectionManager,
    schema_name: str | None = None,
) -> list[dict]:
    return await db.fetch_all(UNUSED_INDEXES_QUERY, (schema_name, schema_name))


async def collect_duplicate_indexes(
    db: ConnectionManager,
    schema_name: str | None = None,
) -> list[dict]:
    rows = await db.fetch_all(DUPLICATE_INDEXES_QUERY, (schema_name, schema_name))
    replication_context: dict[str, Any] = {
        "in_recovery": None,
        "visible_standbys": None,
        "replication_context_available": False,
    }
    try:
        context = await db.fetch_one(REPLICATION_CONTEXT_QUERY)
        if context is not None:
            replication_context = {
                "in_recovery": context.get("in_recovery"),
                "visible_standbys": context.get("visible_standbys"),
                "replication_context_available": True,
            }
    except psycopg.Error:
        pass

    signature_fields = (
        "table_oid",
        "access_method",
        "indkey",
        "indoption",
        "indclass",
        "indcollation",
        "indnkeyatts",
        "index_expression",
        "index_predicate",
    )
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row.get(field) for field in signature_fields)].append(row)

    duplicate_groups = []
    for signature, indexes in grouped.items():
        if len(indexes) < 2:
            continue
        first = indexes[0]
        duplicate_groups.append({
            "schema_name": first["schema_name"],
            "table_name": first["table_name"],
            "statistics_reset_at": first.get("statistics_reset_at"),
            "equivalence": dict(zip(signature_fields[1:], signature[1:], strict=True)),
            "indexes": [
                {
                    "schema_name": index["index_schema_name"],
                    "index_name": index["index_name"],
                    "index_relkind": index["index_relkind"],
                    "index_definition": index["index_definition"],
                    "index_size": index["index_size"],
                    "index_size_bytes": index["index_size_bytes"],
                    "idx_scan": index["idx_scan"],
                    "is_primary": index["is_primary"],
                    "is_unique": index["is_unique"],
                    "is_exclusion": index["is_exclusion"],
                    "is_constraint_backed": index["is_constraint_backed"],
                    "is_replica_identity": index["is_replica_identity"],
                    "is_clustered": index["is_clustered"],
                    "is_valid": index["is_valid"],
                    "is_ready": index["is_ready"],
                    "is_live": index["is_live"],
                }
                for index in indexes
            ],
            **replication_context,
        })

    return duplicate_groups


async def collect_tables_needing_indexes(
    db: ConnectionManager,
    schema_name: str | None = None,
) -> list[dict]:
    return await db.fetch_all(
        TABLES_NEEDING_INDEXES_QUERY,
        (schema_name, schema_name),
    )


async def collect_index_metadata(
    db: ConnectionManager,
    schema_name: str | None = None,
) -> list[dict]:
    """Collect indexed columns, source types, and expression-index definitions.

    The plan analyzer uses this catalog snapshot to distinguish a cast on an
    indexed column from harmless casts on literals, parameters, or unindexed
    columns. Expression indexes are included so an already-supported cast does
    not produce a duplicate recommendation.
    """
    return await db.fetch_all(INDEX_METADATA_QUERY, (schema_name, schema_name))
