"""Durable identity and reconciliation state for full audit operations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg.conninfo import conninfo_to_dict

from pgtriage import __version__

DETECTOR_CONTRACT_VERSION = "1"
DEFAULT_RETENTION_HOURS = 24
DEFAULT_TOMBSTONE_HOURS = 24 * 7


class AuditRunStatus(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class AuditRunConflictError(ValueError):
    """Raised when an operation ID is reused with a different binding."""


@dataclass(frozen=True)
class AuditRunRecord:
    audit_run_id: str
    binding_hash: str
    database_identity_hash: str
    schema_name: str | None
    slow_query_limit: int
    status: AuditRunStatus
    started_at: str
    completed_at: str | None
    package_version: str
    detector_contract_version: str
    postgres_server_version: str | None
    statistics_reset_at: str | None
    evidence_hash: str | None
    result: dict[str, Any] | None
    failure_code: str | None
    failure_reason: str | None
    expires_at: float
    tombstone_expires_at: float

    def provenance(self) -> dict[str, Any]:
        return {
            "audit_run_id": self.audit_run_id,
            "status": self.status.value,
            "package_version": self.package_version,
            "detector_contract_version": self.detector_contract_version,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "schema_name": self.schema_name,
            "postgres_server_version": self.postgres_server_version,
            "statistics_reset_at": self.statistics_reset_at,
            "evidence_hash": self.evidence_hash,
        }

    def lookup_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "audit_run_id": self.audit_run_id,
            "status": self.status.value,
            "provenance": self.provenance(),
            "result": self.result if self.status is AuditRunStatus.COMPLETED else None,
        }
        if self.status is AuditRunStatus.FAILED:
            payload["failure"] = {
                "code": self.failure_code,
                "reason": self.failure_reason,
            }
        else:
            payload["failure"] = None
        return payload


def canonical_json(value: Any) -> str:
    """Return the documented canonical JSON representation used for hashes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def database_target_hash(connection_string: str) -> str:
    """Hash non-secret connection target fields without retaining the DSN."""
    parsed = conninfo_to_dict(connection_string)
    target = {
        "service": parsed.get("service"),
        "host": parsed.get("host"),
        "hostaddr": parsed.get("hostaddr"),
        "port": parsed.get("port"),
        "dbname": parsed.get("dbname"),
        "user": parsed.get("user"),
    }
    return canonical_hash(target)


def operation_binding_hash(
    *,
    database_identity_hash: str,
    schema_name: str | None,
    slow_query_limit: int,
) -> str:
    return canonical_hash(
        {
            "database_identity_hash": database_identity_hash,
            "schema_name": schema_name,
            "slow_query_limit": slow_query_limit,
            "detector_contract_version": DETECTOR_CONTRACT_VERSION,
        }
    )


def normalize_audit_run_id(value: str | None) -> str:
    if value is None:
        return str(uuid4())
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("audit_run_id must be a valid UUID") from error


def default_store_path() -> Path | str:
    configured = os.environ.get("PGTRIAGE_AUDIT_STORE_PATH")
    if configured:
        return configured
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return root / "pgtriage" / "audit_runs.sqlite3"


class AuditRunStore:
    """Permission-restricted SQLite registry for audit reconciliation."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        retention_hours: int = DEFAULT_RETENTION_HOURS,
        tombstone_hours: int = DEFAULT_TOMBSTONE_HOURS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if retention_hours <= 0 or tombstone_hours <= 0:
            raise ValueError("Audit run retention values must be positive.")
        self.path = path if path is not None else default_store_path()
        self.retention_seconds = retention_hours * 3600
        self.tombstone_seconds = tombstone_hours * 3600
        self._clock = clock
        self._lock = threading.RLock()
        self._prepare_path()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_runs (
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
            CREATE INDEX IF NOT EXISTS idx_audit_runs_status
                ON audit_runs(status);
            CREATE INDEX IF NOT EXISTS idx_audit_runs_expiry
                ON audit_runs(expires_at);
            """
        )
        self._db.commit()
        self._restrict_file_permissions()

    def claim(
        self,
        *,
        audit_run_id: str | None,
        database_identity_hash: str,
        schema_name: str | None,
        slow_query_limit: int,
        postgres_server_version: str | None,
        statistics_reset_at: str | None,
    ) -> tuple[AuditRunRecord, bool]:
        normalized_id = normalize_audit_run_id(audit_run_id)
        binding_hash = operation_binding_hash(
            database_identity_hash=database_identity_hash,
            schema_name=schema_name,
            slow_query_limit=slow_query_limit,
        )
        now = self._clock()
        started_at = _utc_iso(now)
        expires_at = now + self.retention_seconds
        tombstone_expires_at = expires_at + self.tombstone_seconds

        with self._lock, self._db:
            self._cleanup_locked(now)
            inserted = self._db.execute(
                """
                INSERT OR IGNORE INTO audit_runs (
                    audit_run_id, binding_hash, database_identity_hash,
                    schema_name, slow_query_limit, status, started_at,
                    package_version, detector_contract_version,
                    postgres_server_version, statistics_reset_at,
                    expires_at, tombstone_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_id,
                    binding_hash,
                    database_identity_hash,
                    schema_name,
                    slow_query_limit,
                    AuditRunStatus.STARTED.value,
                    started_at,
                    __version__,
                    DETECTOR_CONTRACT_VERSION,
                    postgres_server_version,
                    statistics_reset_at,
                    expires_at,
                    tombstone_expires_at,
                ),
            )
            row = self._db.execute(
                "SELECT * FROM audit_runs WHERE audit_run_id = ?",
                (normalized_id,),
            ).fetchone()
            assert row is not None
            record = self._record(row)
            if record.binding_hash != binding_hash:
                raise AuditRunConflictError(
                    "audit_run_id is already bound to a different database, "
                    "schema, or request"
                )
            return record, inserted.rowcount == 1

    def complete(self, audit_run_id: str, result: dict[str, Any]) -> AuditRunRecord:
        evidence = {
            "findings": result.get("findings", []),
            "summary": result.get("summary", {}),
            "sections": result.get("sections", {}),
        }
        evidence_hash = canonical_hash(evidence)
        now = self._clock()
        completed_at = _utc_iso(now)
        expires_at = now + self.retention_seconds
        tombstone_expires_at = expires_at + self.tombstone_seconds

        with self._lock, self._db:
            current = self._required_record(audit_run_id)
            if current.status is not AuditRunStatus.STARTED:
                raise RuntimeError(
                    f"Cannot complete audit run in state {current.status.value}."
                )
            provenance = {
                **current.provenance(),
                "status": AuditRunStatus.COMPLETED.value,
                "completed_at": completed_at,
                "evidence_hash": evidence_hash,
            }
            stored_result = {**result, "provenance": provenance}
            self._db.execute(
                """
                UPDATE audit_runs
                SET status = ?, completed_at = ?, evidence_hash = ?,
                    result_json = ?, expires_at = ?, tombstone_expires_at = ?
                WHERE audit_run_id = ? AND status = ?
                """,
                (
                    AuditRunStatus.COMPLETED.value,
                    completed_at,
                    evidence_hash,
                    canonical_json(stored_result),
                    expires_at,
                    tombstone_expires_at,
                    current.audit_run_id,
                    AuditRunStatus.STARTED.value,
                ),
            )
            return self._required_record(current.audit_run_id)

    def fail(
        self,
        audit_run_id: str,
        *,
        code: str,
        reason: str,
    ) -> AuditRunRecord:
        now = self._clock()
        with self._lock, self._db:
            current = self._required_record(audit_run_id)
            if current.status is not AuditRunStatus.STARTED:
                return current
            self._db.execute(
                """
                UPDATE audit_runs
                SET status = ?, completed_at = ?, failure_code = ?,
                    failure_reason = ?, expires_at = ?, tombstone_expires_at = ?
                WHERE audit_run_id = ? AND status = ?
                """,
                (
                    AuditRunStatus.FAILED.value,
                    _utc_iso(now),
                    code[:100],
                    reason[:500],
                    now + self.retention_seconds,
                    now + self.retention_seconds + self.tombstone_seconds,
                    current.audit_run_id,
                    AuditRunStatus.STARTED.value,
                ),
            )
            return self._required_record(current.audit_run_id)

    def get(self, audit_run_id: str) -> AuditRunRecord | None:
        normalized_id = normalize_audit_run_id(audit_run_id)
        with self._lock, self._db:
            self._cleanup_locked(self._clock())
            row = self._db.execute(
                "SELECT * FROM audit_runs WHERE audit_run_id = ?",
                (normalized_id,),
            ).fetchone()
            return self._record(row) if row is not None else None

    def delete(self, audit_run_id: str) -> bool:
        normalized_id = normalize_audit_run_id(audit_run_id)
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM audit_runs WHERE audit_run_id = ?",
                (normalized_id,),
            )
            return cursor.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _required_record(self, audit_run_id: str) -> AuditRunRecord:
        row = self._db.execute(
            "SELECT * FROM audit_runs WHERE audit_run_id = ?",
            (normalize_audit_run_id(audit_run_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown audit run: {audit_run_id}")
        return self._record(row)

    def _cleanup_locked(self, now: float) -> None:
        self._db.execute(
            """
            UPDATE audit_runs
            SET status = ?, result_json = NULL, evidence_hash = NULL,
                failure_code = NULL, failure_reason = NULL
            WHERE status IN (?, ?, ?) AND expires_at <= ?
            """,
            (
                AuditRunStatus.EXPIRED.value,
                AuditRunStatus.STARTED.value,
                AuditRunStatus.COMPLETED.value,
                AuditRunStatus.FAILED.value,
                now,
            ),
        )
        self._db.execute(
            "DELETE FROM audit_runs WHERE status = ? AND tombstone_expires_at <= ?",
            (AuditRunStatus.EXPIRED.value, now),
        )

    def _record(self, row: sqlite3.Row) -> AuditRunRecord:
        result_json = row["result_json"]
        return AuditRunRecord(
            audit_run_id=row["audit_run_id"],
            binding_hash=row["binding_hash"],
            database_identity_hash=row["database_identity_hash"],
            schema_name=row["schema_name"],
            slow_query_limit=row["slow_query_limit"],
            status=AuditRunStatus(row["status"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            package_version=row["package_version"],
            detector_contract_version=row["detector_contract_version"],
            postgres_server_version=row["postgres_server_version"],
            statistics_reset_at=row["statistics_reset_at"],
            evidence_hash=row["evidence_hash"],
            result=json.loads(result_json) if result_json else None,
            failure_code=row["failure_code"],
            failure_reason=row["failure_reason"],
            expires_at=row["expires_at"],
            tombstone_expires_at=row["tombstone_expires_at"],
        )

    def _prepare_path(self) -> None:
        if str(self.path) == ":memory:":
            return
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass

    def _restrict_file_permissions(self) -> None:
        if str(self.path) == ":memory:":
            return
        try:
            Path(self.path).chmod(0o600)
        except OSError:
            pass


def _utc_iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"Value of type {type(value).__name__} is not JSON serializable")
