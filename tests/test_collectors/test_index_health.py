"""Tests for index catalog metadata collection."""

import pytest

from pgtriage.collectors.index_health import (
    INDEX_METADATA_QUERY,
    collect_index_metadata,
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
    assert mock_db._calls == [("fetch_all", INDEX_METADATA_QUERY, ())]


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
