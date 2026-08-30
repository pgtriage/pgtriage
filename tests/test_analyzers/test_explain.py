"""Tests for EXPLAIN ANALYZE safety validation and plan detection."""

import pytest

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

    # CTE write smuggling — the sharpest edge case
    def test_rejects_cte_with_delete(self):
        assert is_safe_to_explain(
            "WITH x AS (DELETE FROM users RETURNING *) SELECT * FROM x"
        ) is False

    def test_rejects_cte_with_insert(self):
        assert is_safe_to_explain(
            "WITH x AS (INSERT INTO logs (msg) VALUES ('hi') RETURNING *) SELECT * FROM x"
        ) is False

    def test_rejects_cte_with_update(self):
        assert is_safe_to_explain(
            "WITH x AS (UPDATE users SET active = false RETURNING *) SELECT * FROM x"
        ) is False

    # DML hidden in executable SQL
    def test_rejects_delete_in_subquery_text(self):
        assert is_safe_to_explain(
            "SELECT * FROM (DELETE FROM users RETURNING *) x"
        ) is False

    def test_rejects_truncate_anywhere(self):
        assert is_safe_to_explain(
            "SELECT 1; TRUNCATE users"
        ) is False

    def test_allows_dangerous_keywords_inside_string_literals(self):
        assert is_safe_to_explain(
            "SELECT * FROM users WHERE name = 'ALTER TABLE test'"
        ) is True

    def test_allows_semicolon_inside_string_literal(self):
        assert is_safe_to_explain("SELECT 'hello; DROP TABLE users'") is True

    def test_allows_doubled_quote_inside_string_literal(self):
        assert is_safe_to_explain("SELECT 'it''s a DROP test'") is True

    def test_allows_backslash_escape_inside_e_string(self):
        assert is_safe_to_explain(
            r"SELECT E'it\'s safe; DROP TABLE users'"
        ) is True

    def test_does_not_treat_backslash_as_escape_in_standard_string(self):
        assert is_safe_to_explain(
            r"SELECT 'safe\'; DROP TABLE users; SELECT 'x'"
        ) is False

    def test_allows_dangerous_keywords_inside_comments(self):
        assert is_safe_to_explain(
            "SELECT 1 /* DELETE FROM users; */"
        ) is True

    def test_allows_dangerous_keywords_inside_dollar_quoted_literal(self):
        assert is_safe_to_explain(
            "SELECT $body$UPDATE users SET active = false$body$"
        ) is True

    def test_allows_leading_comment_before_select(self):
        assert is_safe_to_explain(
            "/* read-only diagnostic */ SELECT 1"
        ) is True

    def test_rejects_unterminated_string_literal(self):
        assert is_safe_to_explain("SELECT 'unterminated") is False

    def test_rejects_unterminated_block_comment(self):
        assert is_safe_to_explain("SELECT 1 /* unterminated") is False

    # Read-only subqueries should be allowed
    def test_allows_read_only_subquery(self):
        assert is_safe_to_explain(
            "SELECT * FROM users WHERE id IN (SELECT id FROM accounts)"
        ) is True

    def test_allows_select_with_subquery(self):
        assert is_safe_to_explain(
            "SELECT u.*, (SELECT count(*) FROM orders o WHERE o.user_id = u.id) FROM users u"
        ) is True


class TestDetectPlanIssues:
    @staticmethod
    def _uuid_index_metadata(**overrides):
        metadata = {
            "schema_name": "public",
            "table_name": "identifiers",
            "column_name": "account_id",
            "column_type": "uuid",
            "index_position": 1,
            "is_key_column": True,
            "index_name": "idx_identifiers_account_id",
            "index_method": "btree",
            "index_definition": (
                "CREATE INDEX idx_identifiers_account_id "
                "ON public.identifiers USING btree (account_id)"
            ),
            "index_expression": None,
            "index_predicate": None,
            "table_live_rows": 2_200_000,
        }
        metadata.update(overrides)
        return metadata

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

    def test_detects_cast_on_indexed_column_that_forces_large_seq_scan(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Alias": "i",
            "Actual Rows": 1,
            "Rows Removed by Filter": 2_199_999,
            "Actual Loops": 1,
            "Plan Rows": 1,
            "Filter": "((account_id)::text = 'abc'::text)",
        }}]

        findings = detect_plan_issues(
            plan,
            "SELECT * FROM identifiers WHERE account_id::text = 'abc'",
            index_metadata=[self._uuid_index_metadata()],
        )

        mismatches = [f for f in findings if f.category == Category.TYPE_MISMATCH]
        assert len(mismatches) == 1
        finding = mismatches[0]
        assert finding.severity == Severity.HIGH
        assert finding.safe_to_apply is False
        assert finding.evidence["column"] == "account_id"
        assert finding.evidence["source_type"] == "uuid"
        assert finding.evidence["cast_target_type"] == "text"
        assert finding.evidence["rows_examined"] == 2_200_000
        assert finding.evidence["index_names"] == ["idx_identifiers_account_id"]
        assert "binding or casting the compared value as uuid" in finding.suggested_fix
        assert not [f for f in findings if f.category == Category.SEQUENTIAL_SCAN]

    def test_detects_cast_function_syntax_and_qualified_column(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Alias": "i",
            "Actual Rows": 2,
            "Rows Removed by Filter": 149_998,
            "Plan Rows": 2,
            "Filter": "(CAST(i.account_id AS character varying) = 'abc'::varchar)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata(table_live_rows=150_000)],
        )

        mismatches = [f for f in findings if f.category == Category.TYPE_MISMATCH]
        assert len(mismatches) == 1
        assert mismatches[0].severity == Severity.MEDIUM
        assert mismatches[0].evidence["cast_target_type"] == "varchar"

    def test_ignores_cast_on_parameter_instead_of_indexed_column(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Actual Rows": 1,
            "Rows Removed by Filter": 2_199_999,
            "Plan Rows": 1,
            "Filter": "(account_id = ($1)::uuid)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata()],
        )

        assert not [f for f in findings if f.category == Category.TYPE_MISMATCH]
        assert [f for f in findings if f.category == Category.SEQUENTIAL_SCAN]

    def test_ignores_cast_on_unindexed_column(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Actual Rows": 1,
            "Rows Removed by Filter": 2_199_999,
            "Plan Rows": 1,
            "Filter": "((external_id)::text = 'abc'::text)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata()],
        )

        assert not [f for f in findings if f.category == Category.TYPE_MISMATCH]

    def test_ignores_same_type_cast(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Actual Rows": 1,
            "Rows Removed by Filter": 2_199_999,
            "Plan Rows": 1,
            "Filter": "((account_id)::uuid = '00000000-0000-0000-0000-000000000000'::uuid)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata()],
        )

        assert not [f for f in findings if f.category == Category.TYPE_MISMATCH]

    def test_ignores_cast_when_scan_is_small(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Actual Rows": 1,
            "Rows Removed by Filter": 999,
            "Plan Rows": 1,
            "Filter": "((account_id)::text = 'abc'::text)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata(table_live_rows=1_000)],
        )

        assert not [f for f in findings if f.category == Category.TYPE_MISMATCH]

    def test_ignores_cast_when_predicate_returns_large_share_of_table(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Actual Rows": 1_100_000,
            "Rows Removed by Filter": 1_100_000,
            "Plan Rows": 1_100_000,
            "Filter": "((account_id)::text <> '')",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata()],
        )

        assert not [f for f in findings if f.category == Category.TYPE_MISMATCH]
        assert [f for f in findings if f.category == Category.SEQUENTIAL_SCAN]

    def test_ignores_index_method_that_cannot_support_scalar_equality(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Actual Rows": 1,
            "Rows Removed by Filter": 2_199_999,
            "Plan Rows": 1,
            "Filter": "((account_id)::text = 'abc'::text)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata(index_method="gin")],
        )

        assert not [f for f in findings if f.category == Category.TYPE_MISMATCH]

    def test_normalizes_type_modifiers_before_comparing_types(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Actual Rows": 1,
            "Rows Removed by Filter": 2_199_999,
            "Plan Rows": 1,
            "Filter": "(CAST(account_id AS varchar(255)) = 'abc'::varchar)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata()],
        )

        mismatches = [f for f in findings if f.category == Category.TYPE_MISMATCH]
        assert len(mismatches) == 1
        assert mismatches[0].evidence["cast_target_type"] == "varchar"

    @pytest.mark.parametrize(
        "metadata_override",
        [
            {"index_position": 2},
            {"is_key_column": False},
            {"index_predicate": "(active = true)"},
        ],
    )
    def test_ignores_index_that_cannot_support_plain_lookup(self, metadata_override):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Actual Rows": 1,
            "Rows Removed by Filter": 2_199_999,
            "Plan Rows": 1,
            "Filter": "((account_id)::text = 'abc'::text)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata(**metadata_override)],
        )

        assert not [f for f in findings if f.category == Category.TYPE_MISMATCH]

    def test_uses_catalog_row_count_when_plan_has_no_runtime_counters(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Plan Rows": 1,
            "Filter": "((account_id)::text = 'abc'::text)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata(table_live_rows=2_200_000)],
        )

        mismatches = [f for f in findings if f.category == Category.TYPE_MISMATCH]
        assert len(mismatches) == 1
        assert mismatches[0].evidence["rows_examined"] == 2_200_000

    def test_ignores_cast_when_matching_expression_index_exists(self):
        expression_index = self._uuid_index_metadata(
            column_name=None,
            column_type=None,
            index_name="idx_identifiers_account_id_text",
            index_definition=(
                "CREATE INDEX idx_identifiers_account_id_text "
                "ON public.identifiers USING btree (((account_id)::text))"
            ),
            index_expression="(account_id)::text",
        )
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Actual Rows": 1,
            "Rows Removed by Filter": 2_199_999,
            "Plan Rows": 1,
            "Filter": "((account_id)::text = 'abc'::text)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata(), expression_index],
        )

        assert not [f for f in findings if f.category == Category.TYPE_MISMATCH]

    def test_ignores_cast_qualified_for_different_relation(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Schema": "public",
            "Relation Name": "identifiers",
            "Alias": "i",
            "Actual Rows": 1,
            "Rows Removed by Filter": 2_199_999,
            "Plan Rows": 1,
            "Filter": "((other.account_id)::text = 'abc'::text)",
        }}]

        findings = detect_plan_issues(
            plan,
            index_metadata=[self._uuid_index_metadata()],
        )

        assert not [f for f in findings if f.category == Category.TYPE_MISMATCH]

    def test_rows_removed_by_filter_drives_generic_seq_scan_detection(self):
        plan = [{"Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "identifiers",
            "Actual Rows": 1,
            "Rows Removed by Filter": 200_000,
            "Actual Loops": 2,
            "Plan Rows": 1,
            "Filter": "(value = 'abc'::text)",
        }}]

        findings = detect_plan_issues(plan)

        seq_scan = [f for f in findings if f.category == Category.SEQUENTIAL_SCAN]
        assert len(seq_scan) == 1
        assert seq_scan[0].evidence["rows_examined"] == 400_002
