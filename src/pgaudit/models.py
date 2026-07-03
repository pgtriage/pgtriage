"""Data models for audit findings and reports."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Category(str, Enum):
    MISSING_INDEX = "missing_index"
    UNUSED_INDEX = "unused_index"
    DUPLICATE_INDEX = "duplicate_index"
    TABLE_BLOAT = "table_bloat"
    DEAD_TUPLES = "dead_tuples"
    STALE_STATS = "stale_stats"
    SEQUENTIAL_SCAN = "sequential_scan"
    TYPE_MISMATCH = "type_mismatch"
    N_PLUS_ONE = "n_plus_one"
    CONFIG_ISSUE = "config_issue"
    LONG_RUNNING_QUERY = "long_running_query"
    CONNECTION_PRESSURE = "connection_pressure"
    AUTOVACUUM_LAG = "autovacuum_lag"
    TOAST_BLOAT = "toast_bloat"


class Finding(BaseModel):
    severity: Severity
    category: Category
    table: str | None = None
    index: str | None = None
    query: str | None = None
    detail: str
    estimated_impact: str | None = None
    suggested_fix: str | None = None
    safe_to_apply: bool = True
    requires_downtime: bool = False
    evidence: dict[str, Any] = Field(default_factory=dict)


class AuditSummary(BaseModel):
    total_findings: int = 0
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    info: int = 0
    tables_analyzed: int = 0
    queries_analyzed: int = 0
    indexes_analyzed: int = 0


class AuditResult(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    summary: AuditSummary = Field(default_factory=AuditSummary)

    @classmethod
    def from_findings(
        cls,
        findings: list[Finding],
        tables_analyzed: int = 0,
        queries_analyzed: int = 0,
        indexes_analyzed: int = 0,
    ) -> "AuditResult":
        severity_counts = {}
        for f in findings:
            key = f.severity.value
            severity_counts[key] = severity_counts.get(key, 0) + 1

        summary = AuditSummary(
            total_findings=len(findings),
            critical=severity_counts.get("critical", 0),
            high=severity_counts.get("high", 0),
            medium=severity_counts.get("medium", 0),
            low=severity_counts.get("low", 0),
            info=severity_counts.get("info", 0),
            tables_analyzed=tables_analyzed,
            queries_analyzed=queries_analyzed,
            indexes_analyzed=indexes_analyzed,
        )

        sorted_findings = sorted(
            findings,
            key=lambda f: list(Severity).index(f.severity),
        )

        return cls(findings=sorted_findings, summary=summary)
