from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pgtriage import server
from pgtriage.server import app_lifespan


@pytest.mark.asyncio
async def test_lifespan_uses_pgtriage_connection_string(monkeypatch, capsys):
    dsn = "postgresql://reader:new@localhost/db"
    monkeypatch.setenv("PGTRIAGE_CONNECTION_STRING", dsn)
    monkeypatch.delenv("PGAUDIT_CONNECTION_STRING", raising=False)

    async with app_lifespan(None) as context:
        assert context.db._conn_string == dsn

    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_lifespan_supports_deprecated_connection_string(monkeypatch, capsys):
    dsn = "postgresql://reader:old@localhost/db"
    monkeypatch.delenv("PGTRIAGE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("PGAUDIT_CONNECTION_STRING", dsn)

    async with app_lifespan(None) as context:
        assert context.db._conn_string == dsn

    warning = capsys.readouterr().err
    assert "PGAUDIT_CONNECTION_STRING is deprecated" in warning
    assert "PGTRIAGE_CONNECTION_STRING" in warning


@pytest.mark.asyncio
async def test_lifespan_prefers_pgtriage_connection_string(monkeypatch, capsys):
    new_dsn = "postgresql://reader:new@localhost/db"
    monkeypatch.setenv("PGTRIAGE_CONNECTION_STRING", new_dsn)
    monkeypatch.setenv(
        "PGAUDIT_CONNECTION_STRING",
        "postgresql://reader:old@localhost/db",
    )

    async with app_lifespan(None) as context:
        assert context.db._conn_string == new_dsn

    assert capsys.readouterr().err == ""


@pytest.mark.asyncio
async def test_lifespan_requires_connection_string(monkeypatch):
    monkeypatch.delenv("PGTRIAGE_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("PGAUDIT_CONNECTION_STRING", raising=False)

    with pytest.raises(RuntimeError, match="PGTRIAGE_CONNECTION_STRING"):
        async with app_lifespan(None):
            pass


@pytest.mark.asyncio
async def test_mcp_tools_publish_accurate_safety_annotations():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    assert set(tools) == {
        "check_table_health",
        "analyze_slow_queries",
        "check_index_health",
        "check_config",
        "full_audit",
    }

    for tool in tools.values():
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False

    for name in ("check_table_health", "check_index_health", "check_config"):
        assert tools[name].annotations.idempotentHint is True

    for name in ("analyze_slow_queries", "full_audit"):
        assert tools[name].annotations.idempotentHint is False


def _context_with_db(db):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(db=db),
        ),
    )


def _sequential_scan_plan(schema_name: str, table_name: str):
    return [{"Plan": {
        "Node Type": "Seq Scan",
        "Schema": schema_name,
        "Relation Name": table_name,
        "Actual Rows": 200_001,
        "Plan Rows": 200_001,
    }}]


@pytest.mark.asyncio
async def test_slow_query_schema_scope_uses_execution_plans(monkeypatch, mock_db):
    monkeypatch.setattr(server.mcp, "get_context", lambda: _context_with_db(mock_db))
    monkeypatch.setattr(server, "validate_schema_name", AsyncMock())
    monkeypatch.setattr(
        server,
        "is_pg_stat_statements_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        server,
        "is_pg_stat_statements_loaded",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        server,
        "collect_slow_queries",
        AsyncMock(return_value=[
            {"query": "SELECT * FROM customers", "calls": 10},
            {"query": "SELECT * FROM entries", "calls": 10},
        ]),
    )
    monkeypatch.setattr(server, "collect_index_metadata", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        server,
        "run_explain_analyze",
        AsyncMock(side_effect=[
            _sequential_scan_plan("accounts", "customers"),
            _sequential_scan_plan("ledger", "entries"),
        ]),
    )
    monkeypatch.setattr(
        server,
        "collect_n_plus_one_candidates",
        AsyncMock(return_value=[]),
    )

    result = await server.analyze_slow_queries(schema_name="accounts")

    assert result["summary"]["queries_analyzed"] == 1
    assert [finding["table"] for finding in result["findings"]] == ["customers"]
    server.collect_index_metadata.assert_awaited_once_with(mock_db, "accounts")


@pytest.mark.asyncio
async def test_scoped_n_plus_one_excludes_other_schemas(monkeypatch, mock_db):
    monkeypatch.setattr(server.mcp, "get_context", lambda: _context_with_db(mock_db))
    monkeypatch.setattr(server, "validate_schema_name", AsyncMock())
    monkeypatch.setattr(
        server,
        "is_pg_stat_statements_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        server,
        "is_pg_stat_statements_loaded",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(server, "collect_slow_queries", AsyncMock(return_value=[]))
    monkeypatch.setattr(server, "collect_index_metadata", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        server,
        "collect_n_plus_one_candidates",
        AsyncMock(return_value=[
            {
                "query": "SELECT * FROM customers WHERE id = $1",
                "calls": 2_000,
                "mean_exec_time_ms": 1,
                "total_exec_time_ms": 2_000,
            },
            {
                "query": "SELECT * FROM entries WHERE id = $1",
                "calls": 3_000,
                "mean_exec_time_ms": 1,
                "total_exec_time_ms": 3_000,
            },
        ]),
    )
    monkeypatch.setattr(
        server,
        "run_explain_analyze",
        AsyncMock(side_effect=[
            _sequential_scan_plan("accounts", "customers"),
            _sequential_scan_plan("ledger", "entries"),
        ]),
    )

    result = await server.analyze_slow_queries(schema_name="accounts")

    assert len(result["findings"]) == 1
    assert result["findings"][0]["category"] == "n_plus_one"
    assert result["findings"][0]["query"].startswith("SELECT * FROM customers")
