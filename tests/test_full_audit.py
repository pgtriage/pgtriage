from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from pgtriage import server
from pgtriage.audit_runs import AuditRunStore

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


def _install_audit_context(monkeypatch, mock_db):
    store = AuditRunStore(":memory:")
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                db=mock_db,
                audit_runs=store,
                database_identity_hash="database-a",
            )
        )
    )
    monkeypatch.setattr(server.mcp, "get_context", lambda: context)
    return store


@pytest.mark.asyncio
async def test_full_audit_passes_schema_to_scoped_tools(monkeypatch, mock_db):
    store = _install_audit_context(monkeypatch, mock_db)
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
    store.close()


@pytest.mark.asyncio
async def test_full_audit_defaults_to_all_user_schemas(monkeypatch, mock_db):
    store = _install_audit_context(monkeypatch, mock_db)
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
    store.close()


@pytest.mark.asyncio
async def test_repeated_audit_run_id_returns_saved_result_without_reexecution(
    monkeypatch,
    mock_db,
):
    store = _install_audit_context(monkeypatch, mock_db)
    table_health = AsyncMock(return_value=EMPTY_RESULT)
    slow_queries = AsyncMock(return_value=EMPTY_RESULT)
    index_health = AsyncMock(return_value=EMPTY_RESULT)
    config = AsyncMock(return_value=EMPTY_RESULT)
    monkeypatch.setattr(server, "check_table_health", table_health)
    monkeypatch.setattr(server, "analyze_slow_queries", slow_queries)
    monkeypatch.setattr(server, "check_index_health", index_health)
    monkeypatch.setattr(server, "check_config", config)
    audit_run_id = str(uuid4())

    first = await server.full_audit(audit_run_id=audit_run_id)
    second = await server.full_audit(audit_run_id=audit_run_id)

    assert first == second
    assert first["provenance"]["audit_run_id"] == audit_run_id
    assert first["provenance"]["status"] == "COMPLETED"
    table_health.assert_awaited_once()
    slow_queries.assert_awaited_once()
    index_health.assert_awaited_once()
    config.assert_awaited_once()
    store.close()


@pytest.mark.asyncio
async def test_get_audit_run_recovers_completed_result(monkeypatch, mock_db):
    store = _install_audit_context(monkeypatch, mock_db)
    monkeypatch.setattr(
        server,
        "_execute_full_audit",
        AsyncMock(return_value={**EMPTY_RESULT, "sections": {}}),
    )
    audit_run_id = str(uuid4())
    await server.full_audit(audit_run_id=audit_run_id)

    recovered = await server.get_audit_run(audit_run_id)

    assert recovered["status"] == "COMPLETED"
    assert recovered["result"]["provenance"]["audit_run_id"] == audit_run_id
    store.close()


@pytest.mark.asyncio
async def test_get_audit_run_reports_not_found(monkeypatch, mock_db):
    store = _install_audit_context(monkeypatch, mock_db)

    recovered = await server.get_audit_run(str(uuid4()))

    assert recovered["status"] == "NOT_FOUND"
    assert recovered["result"] is None
    store.close()


@pytest.mark.asyncio
async def test_get_audit_run_hides_result_from_different_database(
    monkeypatch,
    mock_db,
):
    store = _install_audit_context(monkeypatch, mock_db)
    audit_run_id = str(uuid4())
    record, _ = store.claim(
        audit_run_id=audit_run_id,
        database_identity_hash="database-b",
        schema_name=None,
        slow_query_limit=10,
        postgres_server_version="PostgreSQL 15.4",
        statistics_reset_at=None,
    )
    store.complete(record.audit_run_id, {**EMPTY_RESULT, "sections": {}})

    recovered = await server.get_audit_run(audit_run_id)

    assert recovered == {
        "audit_run_id": audit_run_id,
        "status": "CONFLICT",
        "provenance": None,
        "result": None,
        "failure": None,
    }
    store.close()


@pytest.mark.asyncio
async def test_full_audit_persists_bounded_failure(monkeypatch, mock_db):
    store = _install_audit_context(monkeypatch, mock_db)
    monkeypatch.setattr(
        server,
        "_execute_full_audit",
        AsyncMock(side_effect=RuntimeError("contains internal detail")),
    )
    audit_run_id = str(uuid4())

    with pytest.raises(RuntimeError, match="contains internal detail"):
        await server.full_audit(audit_run_id=audit_run_id)

    recovered = await server.get_audit_run(audit_run_id)
    assert recovered["status"] == "FAILED"
    assert recovered["failure"]["code"] == "RUNTIMEERROR"
    assert "internal detail" not in recovered["failure"]["reason"]
    store.close()
