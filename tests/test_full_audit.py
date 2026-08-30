from unittest.mock import AsyncMock

import pytest

from pgtriage import server

EMPTY_RESULT = {
    "findings": [],
    "summary": {
        "total_findings": 0,
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "info": 0,
        "tables_analyzed": 0,
        "queries_analyzed": 0,
        "indexes_analyzed": 0,
    },
}


@pytest.mark.asyncio
async def test_full_audit_passes_schema_to_scoped_tools(monkeypatch):
    table_health = AsyncMock(return_value=EMPTY_RESULT)
    slow_queries = AsyncMock(return_value=EMPTY_RESULT)
    index_health = AsyncMock(return_value=EMPTY_RESULT)
    config = AsyncMock(return_value=EMPTY_RESULT)

    monkeypatch.setattr(server, "check_table_health", table_health)
    monkeypatch.setattr(server, "analyze_slow_queries", slow_queries)
    monkeypatch.setattr(server, "check_index_health", index_health)
    monkeypatch.setattr(server, "check_config", config)

    await server.full_audit(slow_query_limit=20, schema_name="pgtriage_demo")

    table_health.assert_awaited_once_with(schema_name="pgtriage_demo")
    slow_queries.assert_awaited_once_with(
        limit=20,
        schema_name="pgtriage_demo",
    )
    index_health.assert_awaited_once_with(schema_name="pgtriage_demo")


@pytest.mark.asyncio
async def test_full_audit_defaults_to_all_user_schemas(monkeypatch):
    table_health = AsyncMock(return_value=EMPTY_RESULT)
    monkeypatch.setattr(
        server,
        "check_table_health",
        table_health,
    )
    slow_queries = AsyncMock(return_value=EMPTY_RESULT)
    monkeypatch.setattr(
        server,
        "analyze_slow_queries",
        slow_queries,
    )
    index_health = AsyncMock(return_value=EMPTY_RESULT)
    monkeypatch.setattr(server, "check_index_health", index_health)
    monkeypatch.setattr(
        server,
        "check_config",
        AsyncMock(return_value=EMPTY_RESULT),
    )

    await server.full_audit()

    table_health.assert_awaited_once_with(schema_name=None)
    slow_queries.assert_awaited_once_with(limit=10, schema_name=None)
    index_health.assert_awaited_once_with(schema_name=None)
