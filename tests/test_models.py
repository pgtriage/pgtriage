"""Tests for data models."""

from pgaudit.models import AuditResult, Category, Finding, Severity


class TestAuditResult:
    def test_from_findings_counts_severities(self):
        findings = [
            Finding(severity=Severity.CRITICAL, category=Category.DEAD_TUPLES, detail="crit"),
            Finding(severity=Severity.HIGH, category=Category.SEQUENTIAL_SCAN, detail="high"),
            Finding(severity=Severity.HIGH, category=Category.MISSING_INDEX, detail="high2"),
            Finding(severity=Severity.MEDIUM, category=Category.UNUSED_INDEX, detail="med"),
            Finding(severity=Severity.LOW, category=Category.CONFIG_ISSUE, detail="low"),
        ]
        result = AuditResult.from_findings(findings, tables_analyzed=10)
        assert result.summary.total_findings == 5
        assert result.summary.critical == 1
        assert result.summary.high == 2
        assert result.summary.medium == 1
        assert result.summary.low == 1
        assert result.summary.tables_analyzed == 10

    def test_findings_sorted_by_severity(self):
        findings = [
            Finding(severity=Severity.LOW, category=Category.CONFIG_ISSUE, detail="low"),
            Finding(severity=Severity.CRITICAL, category=Category.DEAD_TUPLES, detail="crit"),
            Finding(severity=Severity.MEDIUM, category=Category.UNUSED_INDEX, detail="med"),
        ]
        result = AuditResult.from_findings(findings)
        assert result.findings[0].severity == Severity.CRITICAL
        assert result.findings[1].severity == Severity.MEDIUM
        assert result.findings[2].severity == Severity.LOW

    def test_empty_findings(self):
        result = AuditResult.from_findings([])
        assert result.summary.total_findings == 0
        assert len(result.findings) == 0

    def test_model_dump_serializable(self):
        findings = [Finding(severity=Severity.HIGH, category=Category.DEAD_TUPLES, detail="test")]
        result = AuditResult.from_findings(findings)
        dumped = result.model_dump()
        assert isinstance(dumped, dict)
        assert isinstance(dumped["findings"], list)
        assert dumped["findings"][0]["severity"] == "high"
