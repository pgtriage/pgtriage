import json
import sqlite3
import stat
from uuid import uuid4

import pytest

from pgtriage.audit_runs import (
    AuditRunConflictError,
    AuditRunGenerationError,
    AuditRunStatus,
    AuditRunStore,
    canonical_hash,
    canonical_json,
    database_target_hash,
)


def _claim(store, audit_run_id, **overrides):
    arguments = {
        "audit_run_id": audit_run_id,
        "database_identity_hash": "database-a",
        "schema_name": "public",
        "slow_query_limit": 10,
        "postgres_server_version": "PostgreSQL 16.4",
        "statistics_reset_at": None,
    }
    arguments.update(overrides)
    return store.claim(**arguments)


def _result():
    return {
        "findings": [
            {
                "severity": "low",
                "category": "unused_index",
                "detail": "Example evidence",
            }
        ],
        "summary": {
            "total_findings": 1,
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 1,
            "info": 0,
            "tables_analyzed": 1,
            "queries_analyzed": 0,
            "indexes_analyzed": 1,
        },
        "sections": {"index_health": {"status": "checked"}},
    }


def _complete(store, record, result=None):
    assert record.claim_generation is not None
    return store.complete(
        record.audit_run_id,
        result or _result(),
        claim_generation=record.claim_generation,
    )


def test_canonical_hash_is_stable_across_object_key_order():
    left = {"summary": {"low": 1, "high": 0}, "findings": []}
    right = {"findings": [], "summary": {"high": 0, "low": 1}}

    assert canonical_json(left) == canonical_json(right)
    assert canonical_hash(left) == canonical_hash(right)


def test_canonical_hash_rejects_non_finite_numbers():
    with pytest.raises(ValueError):
        canonical_hash({"unsafe": float("nan")})


def test_database_target_hash_excludes_credentials():
    first = database_target_hash(
        "postgresql://audit:first-secret@db.internal:5432/app"
    )
    second = database_target_hash(
        "postgresql://audit:second-secret@db.internal:5432/app"
    )

    assert first == second
    assert "secret" not in first


def test_database_target_hash_distinguishes_libpq_services():
    first = database_target_hash("service=reporting-primary")
    second = database_target_hash("service=reporting-replica")

    assert first != second


def test_database_target_hash_distinguishes_database_roles():
    first = database_target_hash(
        "postgresql://audit_reader@db.internal:5432/app"
    )
    second = database_target_hash(
        "postgresql://admin_reader@db.internal:5432/app"
    )

    assert first != second


def test_completed_run_is_returned_without_creating_another_operation(tmp_path):
    store = AuditRunStore(tmp_path / "audit.sqlite3")
    audit_run_id = str(uuid4())
    claimed, created = _claim(store, audit_run_id)
    assert created is True
    assert claimed.status is AuditRunStatus.STARTED

    completed = _complete(store, claimed)
    repeated, created_again = _claim(store, audit_run_id)

    assert completed.status is AuditRunStatus.COMPLETED
    assert completed.evidence_hash
    assert completed.result["provenance"]["evidence_hash"] == completed.evidence_hash
    assert created_again is False
    assert repeated.result == completed.result
    store.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_identity_hash", "database-b"),
        ("schema_name", "private"),
        ("slow_query_limit", 20),
    ],
)
def test_operation_id_cannot_cross_a_binding(tmp_path, field, value):
    store = AuditRunStore(tmp_path / "audit.sqlite3")
    audit_run_id = str(uuid4())
    _claim(store, audit_run_id)

    with pytest.raises(AuditRunConflictError):
        _claim(store, audit_run_id, **{field: value})
    store.close()


def test_completed_result_survives_server_process_restart(tmp_path):
    path = tmp_path / "audit.sqlite3"
    audit_run_id = str(uuid4())
    first = AuditRunStore(path)
    claimed, _ = _claim(first, audit_run_id)
    _complete(first, claimed)
    first.close()

    restarted = AuditRunStore(path)
    recovered = restarted.get(audit_run_id)

    assert recovered is not None
    assert recovered.status is AuditRunStatus.COMPLETED
    assert recovered.result["summary"]["total_findings"] == 1
    restarted.close()


def test_separate_store_connections_atomically_claim_one_operation(tmp_path):
    path = tmp_path / "audit.sqlite3"
    audit_run_id = str(uuid4())
    first = AuditRunStore(path)
    second = AuditRunStore(path)

    first_record, first_created = _claim(first, audit_run_id)
    second_record, second_created = _claim(second, audit_run_id)

    assert first_created is True
    assert second_created is False
    assert second_record.audit_run_id == first_record.audit_run_id
    first.close()
    second.close()


def test_expiry_removes_sensitive_result_but_retains_tombstone(tmp_path):
    now = [1_700_000_000.0]
    store = AuditRunStore(
        tmp_path / "audit.sqlite3",
        retention_hours=1,
        tombstone_hours=1,
        clock=lambda: now[0],
    )
    audit_run_id = str(uuid4())
    claimed, _ = _claim(store, audit_run_id)
    _complete(store, claimed)

    now[0] += 3601
    expired = store.get(audit_run_id)
    assert expired is not None
    assert expired.status is AuditRunStatus.EXPIRED
    assert expired.result is None
    assert expired.evidence_hash is None

    now[0] += 3601
    assert store.get(audit_run_id) is None
    store.close()


def test_stale_started_run_expires_instead_of_remaining_in_progress_forever(
    tmp_path,
):
    now = [1_700_000_000.0]
    store = AuditRunStore(
        tmp_path / "audit.sqlite3",
        retention_hours=1,
        tombstone_hours=1,
        clock=lambda: now[0],
    )
    audit_run_id = str(uuid4())
    _claim(store, audit_run_id)

    now[0] += 3601
    expired = store.get(audit_run_id)

    assert expired is not None
    assert expired.status is AuditRunStatus.EXPIRED
    assert expired.result is None
    store.close()


def test_failed_run_records_bounded_non_sensitive_reason(tmp_path):
    store = AuditRunStore(tmp_path / "audit.sqlite3")
    audit_run_id = str(uuid4())
    claimed, _ = _claim(store, audit_run_id)
    failed = store.fail(
        audit_run_id,
        claim_generation=claimed.claim_generation,
        code="AUDIT_EXECUTION_FAILED",
        reason="Audit execution failed before a recoverable result was recorded.",
    )

    assert failed.status is AuditRunStatus.FAILED
    assert failed.failure_code == "AUDIT_EXECUTION_FAILED"
    assert failed.result is None
    store.close()


@pytest.mark.parametrize("new_schema", ["old-fixture", "new-fixture"])
def test_stale_generation_cannot_complete_or_fail_reused_id(
    tmp_path,
    new_schema,
):
    now = [0.0]
    path = tmp_path / "audit.sqlite3"
    old_worker = AuditRunStore(
        path,
        retention_hours=1,
        tombstone_hours=1,
        clock=lambda: now[0],
    )
    new_worker = AuditRunStore(
        path,
        retention_hours=1,
        tombstone_hours=1,
        clock=lambda: now[0],
    )
    audit_run_id = str(uuid4())
    old_record, created = _claim(
        old_worker,
        audit_run_id,
        schema_name="old-fixture",
    )
    assert created is True
    assert old_record.claim_generation is not None

    now[0] = 7201.0
    new_record, created = _claim(
        new_worker,
        audit_run_id,
        schema_name=new_schema,
    )
    assert created is True
    assert new_record.claim_generation is not None
    assert new_record.claim_generation != old_record.claim_generation

    with pytest.raises(AuditRunGenerationError, match="stale complete"):
        _complete(
            old_worker,
            old_record,
            {
                **_result(),
                "sections": {"origin": "old-worker-old-fixture"},
            },
        )
    with pytest.raises(AuditRunGenerationError, match="stale fail"):
        old_worker.fail(
            audit_run_id,
            claim_generation=old_record.claim_generation,
            code="OLD_WORKER_FAILED",
            reason="A stale worker must not mutate the new generation.",
        )

    current = new_worker.get(audit_run_id)
    assert current is not None
    assert current.status is AuditRunStatus.STARTED
    assert current.schema_name == new_schema
    assert current.result is None
    assert current.failure_code is None

    completed = _complete(new_worker, new_record)
    assert completed.status is AuditRunStatus.COMPLETED
    old_worker.close()
    new_worker.close()


def test_claim_generation_is_not_exposed_to_duplicate_claims_or_lookup(tmp_path):
    store = AuditRunStore(tmp_path / "audit.sqlite3")
    audit_run_id = str(uuid4())
    created_record, created = _claim(store, audit_run_id)
    duplicate_record, duplicate_created = _claim(store, audit_run_id)
    lookup_record = store.get(audit_run_id)

    assert created is True
    assert created_record.claim_generation is not None
    assert duplicate_created is False
    assert duplicate_record.claim_generation is None
    assert lookup_record is not None
    assert lookup_record.claim_generation is None
    assert "claim_generation" not in lookup_record.lookup_payload()
    assert created_record.claim_generation not in json.dumps(
        lookup_record.lookup_payload()
    )
    store.close()


def test_existing_registry_schema_is_migrated_without_exposing_generation(
    tmp_path,
):
    path = tmp_path / "audit.sqlite3"
    audit_run_id = str(uuid4())
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE audit_runs (
            audit_run_id TEXT PRIMARY KEY,
            binding_hash TEXT NOT NULL,
            database_identity_hash TEXT NOT NULL,
            schema_name TEXT,
            slow_query_limit INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            package_version TEXT NOT NULL,
            detector_contract_version TEXT NOT NULL,
            postgres_server_version TEXT,
            statistics_reset_at TEXT,
            evidence_hash TEXT,
            result_json TEXT,
            failure_code TEXT,
            failure_reason TEXT,
            expires_at REAL NOT NULL,
            tombstone_expires_at REAL NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO audit_runs (
            audit_run_id, binding_hash, database_identity_hash, schema_name,
            slow_query_limit, status, started_at, package_version,
            detector_contract_version, expires_at, tombstone_expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            audit_run_id,
            "legacy-binding",
            "legacy-database",
            "legacy-schema",
            10,
            AuditRunStatus.STARTED.value,
            "1970-01-01T00:00:00+00:00",
            "0.3.0",
            "1",
            3600.0,
            7200.0,
        ),
    )
    connection.commit()
    connection.close()

    store = AuditRunStore(path, clock=lambda: 0.0)
    migrated = store.get(audit_run_id)
    assert migrated is not None
    assert migrated.status is AuditRunStatus.STARTED
    assert migrated.claim_generation is None

    connection = sqlite3.connect(path)
    generation = connection.execute(
        "SELECT claim_generation FROM audit_runs WHERE audit_run_id = ?",
        (audit_run_id,),
    ).fetchone()[0]
    connection.close()
    assert generation
    assert generation not in json.dumps(migrated.lookup_payload())
    store.close()


def test_store_uses_restrictive_posix_permissions(tmp_path):
    path = tmp_path / "private" / "audit.sqlite3"
    store = AuditRunStore(path)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    store.close()


def test_registry_never_persists_a_connection_string(tmp_path):
    path = tmp_path / "audit.sqlite3"
    store = AuditRunStore(path)
    audit_run_id = str(uuid4())
    database_hash = database_target_hash(
        "postgresql://audit:super-secret@db.internal:5432/app"
    )
    _claim(
        store,
        audit_run_id,
        database_identity_hash=database_hash,
    )
    store.close()

    assert b"super-secret" not in path.read_bytes()
    assert b"postgresql://" not in path.read_bytes()


def test_lookup_payload_is_json_serializable(tmp_path):
    store = AuditRunStore(tmp_path / "audit.sqlite3")
    record, _ = _claim(store, str(uuid4()))

    json.dumps(record.lookup_payload())
    store.close()
