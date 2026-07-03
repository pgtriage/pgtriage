"""Tests for EXPLAIN ANALYZE safety validation and plan detection."""

from pgtriage.analyzers.explain import detect_plan_issues, is_safe_to_explain
from pgtriage.models import Category, Severity


class TestIsSafeToExplain:
    def test_allows_simple_select(self):
        assert is_safe_to_explain("SELECT * FROM users") is True

    def test_allows_select_with_where(self):
        assert is_safe_to_explain("SELECT id FROM accounts WHERE status = 'active'") is True

    def test_allows_select_with_join(self):
        assert is_safe_to_explain("SELECT a.id FROM accounts a JOIN users u ON a.user_id = u.id") is True

    def test_allows_select_with_leading_whitespace(self):
        assert is_safe_to_explain("  SELECT 1") is True

    def test_rejects_insert(self):
        assert is_safe_to_explain("INSERT INTO users (name) VALUES ('test')") is False

    def test_rejects_update(self):
        assert is_safe_to_explain("UPDATE users SET name = 'test'") is False

    def test_rejects_delete(self):
        assert is_safe_to_explain("DELETE FROM users") is False

    def test_rejects_drop(self):
        assert is_safe_to_explain("DROP TABLE users") is False

    def test_rejects_truncate(self):
        assert is_safe_to_explain("TRUNCATE users") is False

    def test_rejects_create(self):
        assert is_safe_to_explain("CREATE TABLE test (id int)") is False

    def test_rejects_stacked_queries(self):
        assert is_safe_to_explain("SELECT 1; DROP TABLE users") is False

    def test_rejects_select_into(self):
        assert is_safe_to_explain("SELECT * INTO new_table FROM users") is False

    def test_rejects_select_for_update(self):
        assert is_safe_to_explain("SELECT * FROM users FOR UPDATE") is False

    def test_rejects_select_for_share(self):
        assert is_safe_to_explain("SELECT * FROM users FOR SHARE") is False

    def test_rejects_select_for_no_key_update(self):
        assert is_safe_to_explain("SELECT * FROM users FOR NO KEY UPDATE") is False

    def test_rejects_empty_string(self):
        assert is_safe_to_explain("") is False

    def test_rejects_whitespace_only(self):
        assert is_safe_to_explain("   ") is False

    def test_rejects_none_like(self):
        assert is_safe_to_explain("") is False

    def test_case_insensitive_rejection(self):
        assert is_safe_to_explain("select * INTO backup from users") is False
        assert is_safe_to_explain("SELECT * for UPDATE") is False


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
