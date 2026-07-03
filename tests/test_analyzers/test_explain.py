"""Tests for EXPLAIN ANALYZE plan detection."""

from pgtriage.analyzers.explain import detect_plan_issues
from pgtriage.models import Category, Severity


class TestDetectPlanIssues:
    def test_detects_seq_scan_on_large_table(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "identifiers",
            "Actual Rows": 2_200_000,
            "Plan Rows": 2_200_000,
            "Filter": "(value = 'abc'::text)",
        }}]
        findings = detect_plan_issues(plan, "SELECT * FROM identifiers WHERE value = 'abc'")
        assert len(findings) >= 1
        seq_findings = [f for f in findings if f.category == Category.SEQUENTIAL_SCAN]
        assert len(seq_findings) == 1
        assert seq_findings[0].severity == Severity.HIGH
        assert "identifiers" in seq_findings[0].detail

    def test_detects_stale_stats(self):
        plan = [{"Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "accounts",
            "Actual Rows": 500_000,
            "Plan Rows": 100,
        }}]
        findings = detect_plan_issues(plan)
        stale = [f for f in findings if f.category == Category.STALE_STATS]
        assert len(stale) == 1
        assert stale[0].evidence["estimate_ratio"] == 5000.0

    def test_detects_nested_loop_with_seq_scan(self):
        plan = [{"Plan": {
            "Node Type": "Nested Loop",
            "Actual Rows": 50_000,
            "Plan Rows": 50_000,
            "Plans": [
                {"Node Type": "Index Scan", "Relation Name": "orders", "Actual Rows": 100, "Plan Rows": 100},
                {"Node Type": "Seq Scan", "Relation Name": "line_items", "Actual Rows": 500, "Plan Rows": 500},
            ],
        }}]
        findings = detect_plan_issues(plan)
        missing = [f for f in findings if f.category == Category.MISSING_INDEX]
        assert len(missing) == 1
        assert "line_items" in missing[0].detail

    def test_ignores_small_seq_scan(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "config",
            "Actual Rows": 50,
            "Plan Rows": 50,
        }}]
        findings = detect_plan_issues(plan)
        seq_findings = [f for f in findings if f.category == Category.SEQUENTIAL_SCAN]
        assert len(seq_findings) == 0

    def test_handles_empty_plan(self):
        findings = detect_plan_issues([])
        assert len(findings) == 0

    def test_handles_no_plan_key(self):
        findings = detect_plan_issues([{}])
        assert len(findings) == 0
