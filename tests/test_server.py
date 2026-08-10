import pytest

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
