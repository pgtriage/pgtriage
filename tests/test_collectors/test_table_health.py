import pytest

from pgtriage.collectors.table_health import (
    TABLE_SIZES_QUERY,
    TABLE_STATS_QUERY,
    collect_table_sizes,
    collect_table_stats,
)


@pytest.mark.asyncio
async def test_table_stats_bind_schema_and_table_filters(mock_db):
    await collect_table_stats(mock_db, "customers", "accounts")

    assert mock_db._calls == [
        (
            "fetch_all",
            TABLE_STATS_QUERY,
            ("accounts", "accounts", "customers", "customers"),
        ),
    ]


@pytest.mark.asyncio
async def test_table_stats_default_to_all_user_schemas(mock_db):
    await collect_table_stats(mock_db)

    assert mock_db._calls == [
        ("fetch_all", TABLE_STATS_QUERY, (None, None, None, None)),
    ]


@pytest.mark.asyncio
async def test_table_sizes_bind_schema_filter(mock_db):
    await collect_table_sizes(mock_db, "accounts")

    assert mock_db._calls == [
        ("fetch_all", TABLE_SIZES_QUERY, ("accounts", "accounts")),
    ]


def test_table_filters_precede_order_by():
    assert TABLE_STATS_QUERY.index("relname = %s") < TABLE_STATS_QUERY.index(
        "ORDER BY"
    )
