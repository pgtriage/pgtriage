"""Tests for index catalog metadata collection."""

from unittest.mock import AsyncMock

import psycopg
import pytest

from pgtriage.collectors.index_health import (
    DUPLICATE_INDEXES_QUERY,
    INDEX_METADATA_QUERY,
    REPLICATION_CONTEXT_QUERY,
    TABLES_NEEDING_INDEXES_QUERY,
    UNUSED_INDEXES_QUERY,
    collect_duplicate_indexes,
    collect_index_metadata,
    collect_tables_needing_indexes,
    collect_unused_indexes,
)


@pytest.mark.asyncio
async def test_collect_index_metadata_uses_catalog_snapshot(mock_db):
    expected = [{
        "schema_name": "public",
        "table_name": "identifiers",
        "column_name": "account_id",
        "column_type": "uuid",
        "index_position": 1,
        "is_key_column": True,
        "index_name": "idx_identifiers_account_id",
        "index_definition": "CREATE INDEX ...",
        "index_expression": None,
        "index_predicate": None,
        "table_live_rows": 200_001,
    }]
    mock_db._responses["FROM pg_index indexes"] = expected

    result = await collect_index_metadata(mock_db)

    assert result == expected
    assert mock_db._calls == [
        ("fetch_all", INDEX_METADATA_QUERY, (None, None)),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collector", "query"),
    [
        (collect_unused_indexes, UNUSED_INDEXES_QUERY),
        (collect_tables_needing_indexes, TABLES_NEEDING_INDEXES_QUERY),
        (collect_index_metadata, INDEX_METADATA_QUERY),
    ],
)
async def test_index_collectors_bind_schema_filter(mock_db, collector, query):
    await collector(mock_db, "accounts")

    assert mock_db._calls == [
        ("fetch_all", query, ("accounts", "accounts")),
    ]


def _duplicate_row(name, *, access_method="btree", expression="", **overrides):
    value = {
        "table_oid": 42,
        "schema_name": "accounts",
        "table_name": "orders",
        "index_schema_name": "accounts",
        "index_name": name,
        "index_relkind": "i",
        "access_method": access_method,
        "indkey": "1",
        "indoption": "0",
        "indclass": "1978",
        "indcollation": "0",
        "indnkeyatts": 1,
        "index_expression": expression,
        "index_predicate": "",
        "index_definition": f"CREATE INDEX {name} ON accounts.orders (customer_id)",
        "index_size": "16 kB",
        "index_size_bytes": 16_384,
        "idx_scan": 1,
        "is_primary": False,
        "is_unique": False,
        "is_exclusion": False,
        "is_replica_identity": False,
        "is_clustered": False,
        "is_valid": True,
        "is_ready": True,
        "is_live": True,
        "is_constraint_backed": False,
        "statistics_reset_at": None,
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_duplicate_collector_groups_only_complete_structural_matches(mock_db):
    mock_db._responses["WITH index_data AS"] = [
        _duplicate_row("idx_a", expression="lower((email)::text)"),
        _duplicate_row("idx_b", expression="lower((email)::text)"),
        _duplicate_row("idx_hash", access_method="hash", expression="lower((email)::text)"),
    ]
    mock_db._responses["pg_is_in_recovery"] = {
        "in_recovery": False,
        "visible_standbys": 1,
    }

    result = await collect_duplicate_indexes(mock_db, "accounts")

    assert len(result) == 1
    assert [index["index_name"] for index in result[0]["indexes"]] == [
        "idx_a",
        "idx_b",
    ]
    assert result[0]["visible_standbys"] == 1
    assert mock_db._calls == [
        ("fetch_all", DUPLICATE_INDEXES_QUERY, ("accounts", "accounts")),
        ("fetch_one", REPLICATION_CONTEXT_QUERY, ()),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "different_value"),
    [
        ("access_method", "hash"),
        ("indkey", "1 2"),
        ("indoption", "1"),
        ("indclass", "3126"),
        ("indcollation", "100"),
        ("indnkeyatts", 2),
        ("index_expression", "upper((email)::text)"),
        ("index_predicate", "(active = true)"),
    ],
)
async def test_duplicate_collector_rejects_partial_signature_matches(
    mock_db,
    field,
    different_value,
):
    first = _duplicate_row("idx_a", expression="lower((email)::text)")
    second = _duplicate_row("idx_b", expression="lower((email)::text)")
    second[field] = different_value
    mock_db._responses["WITH index_data AS"] = [first, second]
    mock_db._responses["pg_is_in_recovery"] = {
        "in_recovery": False,
        "visible_standbys": 0,
    }

    result = await collect_duplicate_indexes(mock_db)

    assert result == []


@pytest.mark.asyncio
async def test_replication_permission_failure_does_not_fail_collection(mock_db):
    mock_db._responses["WITH index_data AS"] = [
        _duplicate_row("idx_a"),
        _duplicate_row("idx_b"),
    ]
    mock_db.fetch_one = AsyncMock(side_effect=psycopg.OperationalError())

    result = await collect_duplicate_indexes(mock_db)

    assert len(result) == 1
    assert result[0]["replication_context_available"] is False
    assert result[0]["visible_standbys"] is None


def test_duplicate_query_uses_complete_equivalence_and_protection_data():
    for fragment in (
        "indexes.indkey::text",
        "indexes.indoption::text",
        "indexes.indclass::text",
        "indexes.indcollation::text",
        "indexes.indnkeyatts",
        "pg_get_expr(indexes.indexprs, indexes.indrelid, false)",
        "pg_get_expr(indexes.indpred, indexes.indrelid, false)",
        "access_method.amname",
        "indexes.indisclustered",
        "indexes.indisreplident",
        "indexes.indislive",
        "constraints.conindid",
        "database_stats.stats_reset",
    ):
        assert fragment in DUPLICATE_INDEXES_QUERY


def test_index_metadata_query_includes_expression_and_validity_data():
    assert "pg_get_expr(indexes.indexprs" in INDEX_METADATA_QUERY
    assert "pg_get_expr(indexes.indpred" in INDEX_METADATA_QUERY
    assert "indexes.indisvalid" in INDEX_METADATA_QUERY
    assert "indexes.indisready" in INDEX_METADATA_QUERY
    assert "indexes.indnkeyatts" in INDEX_METADATA_QUERY
    assert "format_type(attr.atttypid" in INDEX_METADATA_QUERY
    assert "stats.n_live_tup" in INDEX_METADATA_QUERY
    assert "GREATEST" in INDEX_METADATA_QUERY
    assert "tbl.reltuples" in INDEX_METADATA_QUERY
    assert "%s::text IS NULL OR n.nspname = %s" in INDEX_METADATA_QUERY
