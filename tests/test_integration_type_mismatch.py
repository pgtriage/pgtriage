"""Integration test for casts that suppress an existing PostgreSQL index."""

import asyncio
import os

import psycopg
import pytest

from pgtriage.analyzers.explain import (
    _run_prepared_generic_explain,
    detect_plan_issues,
    run_explain_analyze,
)
from pgtriage.collectors.index_health import collect_index_metadata
from pgtriage.connection import ConnectionManager
from pgtriage.models import Category

pytestmark = pytest.mark.integration

DSN = os.environ.get("PGTRIAGE_CONNECTION_STRING", "")
SCHEMA = "pgtriage_cast_test"


@pytest.fixture(scope="module")
def cast_mismatch_db():
    if not DSN:
        pytest.skip("PGTRIAGE_CONNECTION_STRING not set")

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.execute(f"""
            CREATE TABLE {SCHEMA}.identifiers (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                account_id uuid NOT NULL,
                payload text NOT NULL
            )
        """)
        conn.execute(f"""
            INSERT INTO {SCHEMA}.identifiers (account_id, payload)
            SELECT md5(value::text)::uuid, repeat(md5(value::text), 2)
            FROM generate_series(1, 200001) AS value
        """)
        conn.execute(f"""
            CREATE INDEX idx_cast_test_account_id
            ON {SCHEMA}.identifiers (account_id)
        """)
        conn.execute(f"ANALYZE {SCHEMA}.identifiers")

    yield DSN

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def test_detects_real_cast_suppressed_index(cast_mismatch_db):
    target = "14ee22eaba297944c96afdbe5b16c65b"
    query = (
        f"SELECT * FROM {SCHEMA}.identifiers "
        f"WHERE account_id::text = '{target}'"
    )
    with psycopg.connect(cast_mismatch_db, autocommit=True) as conn:
        plan = conn.execute(
            f"EXPLAIN (ANALYZE, FORMAT JSON) {query}"
        ).fetchone()[0]

    async def metadata():
        db = ConnectionManager(cast_mismatch_db)
        try:
            return await collect_index_metadata(db)
        finally:
            await db.close()

    index_metadata = asyncio.run(metadata())
    findings = detect_plan_issues(
        plan,
        query,
        index_metadata=index_metadata,
    )

    mismatches = [
        finding for finding in findings
        if finding.category == Category.TYPE_MISMATCH
    ]
    assert len(mismatches) == 1
    assert mismatches[0].table == "identifiers"
    assert mismatches[0].evidence["source_type"] == "uuid"
    assert mismatches[0].evidence["cast_target_type"] == "text"
    assert mismatches[0].evidence["rows_examined"] >= 200001
    assert mismatches[0].evidence["index_names"] == ["idx_cast_test_account_id"]


def test_detects_cast_from_normalized_pg_stat_statements_query(cast_mismatch_db):
    query = (
        f"SELECT * FROM {SCHEMA}.identifiers "
        "WHERE account_id::text = $1"
    )

    async def analyze():
        db = ConnectionManager(cast_mismatch_db)
        try:
            plan = await run_explain_analyze(db, query)
            metadata = await collect_index_metadata(db)
            return plan, metadata
        finally:
            await db.close()

    plan, index_metadata = asyncio.run(analyze())
    assert plan is not None

    findings = detect_plan_issues(
        plan,
        query,
        index_metadata=index_metadata,
    )
    mismatches = [
        finding for finding in findings
        if finding.category == Category.TYPE_MISMATCH
    ]
    assert len(mismatches) == 1
    assert mismatches[0].evidence["source_type"] == "uuid"
    assert mismatches[0].evidence["cast_target_type"] == "text"
    assert mismatches[0].evidence["rows_examined"] >= 200001


def test_prepared_statement_fallback_returns_generic_plan(cast_mismatch_db):
    query = (
        f"SELECT * FROM {SCHEMA}.identifiers "
        "WHERE account_id::text = $1"
    )

    async def explain_with_fallback():
        db = ConnectionManager(cast_mismatch_db)
        try:
            return await _run_prepared_generic_explain(db, query, 1)
        finally:
            await db.close()

    plan = asyncio.run(explain_with_fallback())
    assert plan is not None
    plan_text = str(plan)
    assert "Seq Scan" in plan_text
    assert "account_id" in plan_text
