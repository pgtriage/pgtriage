"""Integration test against a sabotaged pgbench database.

Requires a running PostgreSQL with PGTRIAGE_CONNECTION_STRING set.
Skip with: pytest -m "not integration"

This test:
  1. Creates pgbench tables (pgbench -i)
  2. Sabotages the database (junk indexes, disabled autovacuum, duplicates)
  3. Generates some query traffic
  4. Runs each pgtriage tool and asserts it finds the planted issues
"""

import asyncio
import os
import subprocess

import psycopg
import pytest

from pgtriage.analyzers.config_rules import analyze_config
from pgtriage.analyzers.patterns import (
    analyze_dead_tuples,
    analyze_sequential_scans,
    analyze_table_bloat,
    analyze_vacuum_staleness,
)
from pgtriage.collectors.config import collect_config_settings, collect_connection_stats
from pgtriage.collectors.index_health import (
    collect_duplicate_indexes,
    collect_unused_indexes,
)
from pgtriage.collectors.table_health import collect_table_sizes, collect_table_stats
from pgtriage.connection import ConnectionManager
from pgtriage.models import AuditResult, Category

pytestmark = pytest.mark.integration

DSN = os.environ.get("PGTRIAGE_CONNECTION_STRING", "")
SKIP_REASON = "PGTRIAGE_CONNECTION_STRING not set"


def _run_sql(dsn: str, sql: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql)


def _run_sql_fetch(dsn: str, sql: str) -> list:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(sql).fetchall()


@pytest.fixture(scope="module")
def sabotaged_db():
    """Set up a pgbench database with planted performance issues."""
    if not DSN:
        pytest.skip(SKIP_REASON)

    try:
        parsed = psycopg.conninfo.conninfo_to_dict(DSN)
    except Exception:
        pytest.skip(f"Invalid connection string: {DSN}")

    host = parsed.get("host", "localhost")
    port = parsed.get("port", "5432")
    dbname = parsed.get("dbname", "pgtriage_test")
    user = parsed.get("user", "pgtriage")

    env = os.environ.copy()
    env["PGPASSWORD"] = parsed.get("password", "")

    result = subprocess.run(
        ["pgbench", "-i", "-s", "10", "-h", host, "-p", str(port), "-U", user, dbname],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        pytest.skip(f"pgbench init failed: {result.stderr}")

    _run_sql(DSN, """
        CREATE INDEX IF NOT EXISTS idx_junk_accounts_1
        ON pgbench_accounts (aid, bid);
    """)
    _run_sql(DSN, """
        CREATE INDEX IF NOT EXISTS idx_junk_accounts_2
        ON pgbench_accounts (aid, bid);
    """)
    _run_sql(DSN, """
        CREATE INDEX IF NOT EXISTS idx_junk_accounts_unused
        ON pgbench_accounts (filler);
    """)

    _run_sql(DSN, """
        ALTER TABLE pgbench_history SET (autovacuum_enabled = off);
    """)

    subprocess.run(
        ["pgbench", "-c", "4", "-T", "5", "-h", host, "-p", str(port), "-U", user, dbname],
        capture_output=True,
        text=True,
        env=env,
    )

    yield DSN

    _run_sql(DSN, "DROP INDEX IF EXISTS idx_junk_accounts_1;")
    _run_sql(DSN, "DROP INDEX IF EXISTS idx_junk_accounts_2;")
    _run_sql(DSN, "DROP INDEX IF EXISTS idx_junk_accounts_unused;")
    _run_sql(DSN, "ALTER TABLE pgbench_history SET (autovacuum_enabled = on);")
    _run_sql(DSN, "DROP TABLE IF EXISTS pgbench_accounts, pgbench_branches, pgbench_history, pgbench_tellers CASCADE;")


@pytest.fixture
def db(sabotaged_db):
    """Provide an async ConnectionManager."""
    cm = ConnectionManager(sabotaged_db)
    yield cm
    asyncio.get_event_loop().run_until_complete(cm.close())


class TestTableHealth:
    def test_finds_tables(self, db):
        async def run():
            await db.connect()
            stats = await collect_table_stats(db)
            return stats
        stats = asyncio.get_event_loop().run_until_complete(run())
        table_names = [t["table_name"] for t in stats]
        assert "pgbench_accounts" in table_names
        assert "pgbench_history" in table_names

    def test_collects_sizes(self, db):
        async def run():
            await db.connect()
            sizes = await collect_table_sizes(db)
            return sizes
        sizes = asyncio.get_event_loop().run_until_complete(run())
        assert len(sizes) > 0
        account_sizes = [s for s in sizes if s["table_name"] == "pgbench_accounts"]
        assert len(account_sizes) == 1
        assert account_sizes[0]["total_size_bytes"] > 0


class TestIndexHealth:
    def test_finds_duplicate_indexes(self, db):
        async def run():
            await db.connect()
            dupes = await collect_duplicate_indexes(db)
            return dupes
        dupes = asyncio.get_event_loop().run_until_complete(run())
        dupe_names = []
        for d in dupes:
            dupe_names.extend([str(d["index_1"]), str(d["index_2"])])
        assert any("idx_junk_accounts" in n for n in dupe_names), \
            f"Expected to find junk duplicate indexes, got: {dupe_names}"

    def test_finds_unused_indexes(self, db):
        async def run():
            await db.connect()
            unused = await collect_unused_indexes(db)
            return unused
        unused = asyncio.get_event_loop().run_until_complete(run())
        unused_names = [u["index_name"] for u in unused]
        assert "idx_junk_accounts_unused" in unused_names, \
            f"Expected idx_junk_accounts_unused in unused list, got: {unused_names}"


class TestConfig:
    def test_collects_settings(self, db):
        async def run():
            await db.connect()
            settings = await collect_config_settings(db)
            return settings
        settings = asyncio.get_event_loop().run_until_complete(run())
        setting_names = [s["name"] for s in settings]
        assert "shared_buffers" in setting_names
        assert "max_connections" in setting_names
        assert "autovacuum_vacuum_scale_factor" in setting_names

    def test_analyzes_config(self, db):
        async def run():
            await db.connect()
            settings = await collect_config_settings(db)
            conn_stats = await collect_connection_stats(db)
            return analyze_config(settings, conn_stats)
        findings = asyncio.get_event_loop().run_until_complete(run())
        assert len(findings) > 0


class TestFullAudit:
    def test_end_to_end(self, db):
        async def run():
            await db.connect()
            stats = await collect_table_stats(db)
            sizes = await collect_table_sizes(db)
            unused = await collect_unused_indexes(db)
            dupes = await collect_duplicate_indexes(db)
            settings = await collect_config_settings(db)
            conn_stats = await collect_connection_stats(db)

            from pgtriage.models import Finding, Severity
            findings = []
            findings.extend(analyze_dead_tuples(stats))
            findings.extend(analyze_sequential_scans(stats))
            findings.extend(analyze_vacuum_staleness(stats))
            findings.extend(analyze_table_bloat(sizes))
            findings.extend(analyze_config(settings, conn_stats))

            for idx in unused[:10]:
                findings.append(Finding(
                    severity=Severity.LOW,
                    category=Category.UNUSED_INDEX,
                    table=idx["table_name"],
                    index=idx["index_name"],
                    detail=f"Unused: {idx['index_name']}",
                ))
            for d in dupes:
                findings.append(Finding(
                    severity=Severity.MEDIUM,
                    category=Category.DUPLICATE_INDEX,
                    table=str(d["table_name"]),
                    detail=f"Duplicate: {d['index_1']} / {d['index_2']}",
                ))

            result = AuditResult.from_findings(
                findings,
                tables_analyzed=len(stats),
                indexes_analyzed=len(unused) + len(dupes),
            )
            return result
        result = asyncio.get_event_loop().run_until_complete(run())

        assert result.summary.total_findings > 0
        categories = {f.category for f in result.findings}
        assert Category.DUPLICATE_INDEX in categories, "Should find planted duplicate indexes"
        assert Category.UNUSED_INDEX in categories, "Should find planted unused indexes"

        output = result.model_dump()
        import json
        output_size = len(json.dumps(output))
        assert output_size < 50_000, f"Output too large: {output_size} chars"
