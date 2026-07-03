"""Collect PostgreSQL configuration and connection stats."""

from pgaudit.connection import ConnectionManager

CONFIG_SETTINGS_QUERY = """
SELECT name, setting, unit, short_desc, context, boot_val, reset_val
FROM pg_settings
WHERE name IN (
    'max_connections',
    'shared_buffers',
    'effective_cache_size',
    'work_mem',
    'maintenance_work_mem',
    'random_page_cost',
    'seq_page_cost',
    'default_statistics_target',
    'checkpoint_completion_target',
    'wal_buffers',
    'min_wal_size',
    'max_wal_size',
    'autovacuum',
    'autovacuum_max_workers',
    'autovacuum_vacuum_threshold',
    'autovacuum_vacuum_scale_factor',
    'autovacuum_analyze_threshold',
    'autovacuum_analyze_scale_factor',
    'autovacuum_vacuum_cost_delay',
    'autovacuum_vacuum_cost_limit',
    'log_min_duration_statement',
    'track_activity_query_size'
)
ORDER BY name
"""

CONNECTION_STATS_QUERY = """
SELECT
    (SELECT count(*) FROM pg_stat_activity) AS total_connections,
    (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS max_connections,
    (SELECT count(*) FROM pg_stat_activity WHERE state = 'active') AS active_queries,
    (SELECT count(*) FROM pg_stat_activity WHERE state = 'idle') AS idle_connections,
    (SELECT count(*) FROM pg_stat_activity
     WHERE state = 'active'
       AND now() - query_start > interval '30 seconds') AS long_running_queries
"""

DATABASE_INFO_QUERY = """
SELECT
    pg_size_pretty(pg_database_size(current_database())) AS database_size,
    current_database() AS database_name,
    (SELECT setting FROM pg_settings WHERE name = 'server_version') AS pg_version
"""


async def collect_config_settings(db: ConnectionManager) -> list[dict]:
    return await db.fetch_all(CONFIG_SETTINGS_QUERY)


async def collect_connection_stats(db: ConnectionManager) -> dict | None:
    return await db.fetch_one(CONNECTION_STATS_QUERY)


async def collect_database_info(db: ConnectionManager) -> dict | None:
    return await db.fetch_one(DATABASE_INFO_QUERY)
