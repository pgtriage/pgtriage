"""Tests for conservative duplicate-index candidate selection."""

from datetime import UTC, datetime, timedelta

from pgtriage.analyzers.index_health import (
    MIN_INDEX_STATS_AGE_DAYS,
    analyze_duplicate_indexes,
)

NOW = datetime(2026, 10, 23, tzinfo=UTC)
MATURE_RESET = NOW - timedelta(days=MIN_INDEX_STATS_AGE_DAYS + 1)


def _index(
    name: str,
    scans: int,
    *,
    schema: str = "public",
    **overrides,
):
    value = {
        "schema_name": schema,
        "index_name": name,
        "index_relkind": "i",
        "index_definition": f"CREATE INDEX {name} ON orders (customer_id)",
        "index_size": "2 GB",
        "index_size_bytes": 2_000_000_000,
        "idx_scan": scans,
        "is_primary": False,
        "is_unique": False,
        "is_exclusion": False,
        "is_constraint_backed": False,
        "is_replica_identity": False,
        "is_clustered": False,
        "is_valid": True,
        "is_ready": True,
        "is_live": True,
    }
    value.update(overrides)
    return value


def _group(indexes, **overrides):
    value = {
        "schema_name": "public",
        "table_name": "orders",
        "statistics_reset_at": MATURE_RESET,
        "equivalence": {
            "access_method": "btree",
            "indkey": "1",
            "indoption": "0",
            "indclass": "1978",
            "indcollation": "0",
            "indnkeyatts": 1,
            "index_expression": "",
            "index_predicate": "",
        },
        "indexes": indexes,
        "in_recovery": False,
        "visible_standbys": 0,
        "replication_context_available": True,
    }
    value.update(overrides)
    return value


def test_selects_all_colder_candidates_from_three_index_group():
    findings = analyze_duplicate_indexes([
        _group([
            _index("idx_orders_customer", 18_420),
            _index("idx_orders_customer_copy", 12),
            _index("idx_orders_customer_old", 1),
        ]),
    ], now=NOW)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.safe_to_apply is False
    assert finding.evidence["keep_index"]["name"] == "idx_orders_customer"
    assert [
        candidate["name"] for candidate in finding.evidence["drop_candidates"]
    ] == ["idx_orders_customer_copy", "idx_orders_customer_old"]
    assert finding.suggested_fix.count("DROP INDEX CONCURRENTLY") == 3
    assert '"public"."idx_orders_customer_copy"' in finding.suggested_fix
    assert len(finding.evidence["indexes"]) == 3
    assert finding.evidence["index_1_def"].startswith("CREATE INDEX")
    assert finding.evidence["index_2_def"].startswith("CREATE INDEX")


def test_tied_scan_counts_remain_review_only():
    finding = analyze_duplicate_indexes([
        _group([
            _index("idx_a", 10),
            _index("idx_b", 10),
        ]),
    ], now=NOW)[0]

    assert finding.evidence["keep_index"] is None
    assert finding.evidence["drop_candidates"] == []
    assert "tied" in finding.evidence["review_only_reason"]
    assert "DROP INDEX CONCURRENTLY" not in finding.suggested_fix


def test_all_zero_scan_counts_remain_review_only():
    finding = analyze_duplicate_indexes([
        _group([_index("idx_a", 0), _index("idx_b", 0)]),
    ], now=NOW)[0]

    assert "zero" in finding.evidence["review_only_reason"]
    assert finding.evidence["drop_candidates"] == []


def test_recent_or_missing_statistics_remain_review_only():
    recent = _group(
        [_index("idx_a", 20), _index("idx_b", 1)],
        statistics_reset_at=NOW - timedelta(days=2),
    )
    missing = _group(
        [_index("idx_c", 20), _index("idx_d", 1)],
        statistics_reset_at=None,
    )

    findings = analyze_duplicate_indexes([recent, missing], now=NOW)

    assert "younger" in findings[0].evidence["review_only_reason"]
    assert "unavailable" in findings[1].evidence["review_only_reason"]
    assert all(not finding.evidence["drop_candidates"] for finding in findings)


def test_protected_indexes_are_never_nominated():
    finding = analyze_duplicate_indexes([
        _group([
            _index("idx_keeper", 100),
            _index("idx_constraint", 2, is_constraint_backed=True),
            _index("idx_candidate", 1),
        ]),
    ], now=NOW)[0]

    assert [
        candidate["name"] for candidate in finding.evidence["drop_candidates"]
    ] == ["idx_candidate"]
    protected = next(
        index
        for index in finding.evidence["indexes"]
        if index["name"] == "idx_constraint"
    )
    assert protected["protected_reasons"] == ["constraint-backed"]


def test_every_protected_index_type_is_excluded_from_candidates():
    protected_variants = (
        {"is_primary": True},
        {"is_unique": True},
        {"is_exclusion": True},
        {"is_constraint_backed": True},
        {"is_replica_identity": True},
        {"is_clustered": True},
        {"is_valid": False},
        {"is_ready": False},
        {"is_live": False},
        {"index_relkind": "I"},
    )

    for position, protection in enumerate(protected_variants):
        protected_name = f"idx_protected_{position}"
        finding = analyze_duplicate_indexes([
            _group([
                _index("idx_keeper", 100),
                _index(protected_name, 1, **protection),
            ]),
        ], now=NOW)[0]

        assert finding.evidence["drop_candidates"] == []
        assert protected_name not in finding.suggested_fix


def test_exactly_seven_day_old_statistics_are_mature():
    finding = analyze_duplicate_indexes([
        _group(
            [_index("idx_a", 100), _index("idx_b", 1)],
            statistics_reset_at=NOW - timedelta(days=MIN_INDEX_STATS_AGE_DAYS),
        ),
    ], now=NOW)[0]

    assert finding.evidence["review_only_reason"] is None
    assert [
        candidate["name"] for candidate in finding.evidence["drop_candidates"]
    ] == ["idx_b"]


def test_quoted_identifiers_generate_safe_schema_qualified_sql():
    finding = analyze_duplicate_indexes([
        _group([
            _index("Keeper", 100, schema='Odd"Schema'),
            _index('Drop"Me', 1, schema='Odd"Schema'),
        ]),
    ], now=NOW)[0]

    assert (
        'DROP INDEX CONCURRENTLY "Odd""Schema"."Drop""Me";'
        in finding.suggested_fix
    )


def test_visible_standbys_add_explicit_replica_warning():
    finding = analyze_duplicate_indexes([
        _group(
            [_index("idx_a", 100), _index("idx_b", 1)],
            visible_standbys=2,
        ),
    ], now=NOW)[0]

    assert any(
        "standby scan counts are not visible" in advisory
        for advisory in finding.evidence["advisories"]
    )
