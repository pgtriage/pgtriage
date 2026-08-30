import pytest

from pgtriage.collectors.schemas import SCHEMA_EXISTS_QUERY, validate_schema_name


@pytest.mark.asyncio
async def test_omitted_schema_needs_no_catalog_lookup(mock_db):
    await validate_schema_name(mock_db, None)

    assert mock_db._calls == []


@pytest.mark.asyncio
async def test_valid_schema_uses_bound_catalog_lookup(mock_db):
    mock_db._responses["FROM pg_namespace"] = {"valid": True}

    await validate_schema_name(mock_db, "accounts")

    assert mock_db._calls == [
        ("fetch_one", SCHEMA_EXISTS_QUERY, ("accounts",)),
    ]


@pytest.mark.asyncio
async def test_unknown_schema_is_rejected(mock_db):
    mock_db._responses["FROM pg_namespace"] = {"valid": False}

    with pytest.raises(ValueError, match="does not exist"):
        await validate_schema_name(mock_db, "missing")


@pytest.mark.asyncio
async def test_empty_schema_is_rejected_without_query(mock_db):
    with pytest.raises(ValueError, match="must not be empty"):
        await validate_schema_name(mock_db, "")

    assert mock_db._calls == []


def test_schema_validation_excludes_postgres_system_namespaces():
    assert "nspname <> 'information_schema'" in SCHEMA_EXISTS_QUERY
    assert "left(nspname, 3) <> 'pg_'" in SCHEMA_EXISTS_QUERY
