"""Tests for deterministic pattern detection analyzers."""

from pgaudit.analyzers.patterns import (
    analyze_dead_tuples,
    analyze_sequential_scans,
    analyze_table_bloat,
    analyze_vacuum_staleness,
)
from pgaudit.models import Category, Severity


class TestAnalyzeDeadTuples:
    def test_flags_high_dead_tuple_ratio(self):
        stats = [{"table_name": "orders", "n_live_tup": 100_000, "n_dead_tup": 25_000, "dead_tuple_pct": 20.0, "autovacuum_count": 3}]
        findings = analyze_dead_tuples(stats)
        assert len(findings) == 1
        assert findings[0].severity == Severity.MEDIUM
        assert findings[0].category == Category.DEAD_TUPLES
        assert findings[0].table == "orders"

    def test_high_above_20_percent(self):
        stats = [{"table_name": "orders", "n_live_tup": 100_000, "n_dead_tup": 30_000, "dead_tuple_pct": 23.0, "autovacuum_count": 1}]
        findings = analyze_dead_tuples(stats)
        assert findings[0].severity == Severity.HIGH

    def test_critical_above_30_percent(self):
        stats = [{"table_name": "logs", "n_live_tup": 50_000, "n_dead_tup": 30_000, "dead_tuple_pct": 37.5, "autovacuum_count": 0}]
        findings = analyze_dead_tuples(stats)
        assert findings[0].severity == Severity.CRITICAL

    def test_ignores_small_dead_counts(self):
        stats = [{"table_name": "config", "n_live_tup": 100, "n_dead_tup": 50, "dead_tuple_pct": 33.0, "autovacuum_count": 0}]
        findings = analyze_dead_tuples(stats)
        assert len(findings) == 0

    def test_ignores_healthy_tables(self):
        stats = [{"table_name": "users", "n_live_tup": 1_000_000, "n_dead_tup": 500, "dead_tuple_pct": 0.05, "autovacuum_count": 10}]
        findings = analyze_dead_tuples(stats)
        assert len(findings) == 0


class TestAnalyzeSequentialScans:
    def test_flags_large_table_with_high_seq_ratio(self):
        stats = [{"table_name": "events", "n_live_tup": 500_000, "seq_scan_pct": 95.0, "seq_scan": 5000, "idx_scan": 200}]
        findings = analyze_sequential_scans(stats)
        assert len(findings) == 1
        assert findings[0].category == Category.SEQUENTIAL_SCAN

    def test_critical_for_million_row_tables(self):
        stats = [{"table_name": "transactions", "n_live_tup": 5_000_000, "seq_scan_pct": 90.0, "seq_scan": 10_000, "idx_scan": 500}]
        findings = analyze_sequential_scans(stats)
        assert findings[0].severity == Severity.HIGH

    def test_ignores_small_tables(self):
        stats = [{"table_name": "settings", "n_live_tup": 50, "seq_scan_pct": 100.0, "seq_scan": 1000, "idx_scan": 0}]
        findings = analyze_sequential_scans(stats)
        assert len(findings) == 0

    def test_ignores_low_seq_scan_ratio(self):
        stats = [{"table_name": "users", "n_live_tup": 200_000, "seq_scan_pct": 20.0, "seq_scan": 100, "idx_scan": 400}]
        findings = analyze_sequential_scans(stats)
        assert len(findings) == 0


class TestAnalyzeTableBloat:
    def test_flags_toast_bloat(self):
        sizes = [{"table_name": "documents", "table_size": "50 MB", "table_size_bytes": 50_000_000, "toast_size": "40 MB", "toast_size_bytes": 40_000_000, "total_size": "100 MB"}]
        findings = analyze_table_bloat(sizes)
        assert len(findings) == 1
        assert findings[0].category == Category.TOAST_BLOAT

    def test_ignores_small_tables(self):
        sizes = [{"table_name": "tiny", "table_size": "8 kB", "table_size_bytes": 8192, "toast_size": "16 kB", "toast_size_bytes": 16384, "total_size": "32 kB"}]
        findings = analyze_table_bloat(sizes)
        assert len(findings) == 0

    def test_ignores_low_toast_ratio(self):
        sizes = [{"table_name": "data", "table_size": "100 MB", "table_size_bytes": 100_000_000, "toast_size": "10 MB", "toast_size_bytes": 10_000_000, "total_size": "120 MB"}]
        findings = analyze_table_bloat(sizes)
        assert len(findings) == 0


class TestAnalyzeVacuumStaleness:
    def test_flags_never_vacuumed_with_dead_tuples(self):
        stats = [{"table_name": "outbox", "n_dead_tup": 50_000, "last_autovacuum": None, "last_vacuum": None}]
        findings = analyze_vacuum_staleness(stats)
        assert len(findings) == 1
        assert findings[0].category == Category.AUTOVACUUM_LAG

    def test_ignores_low_dead_tuples(self):
        stats = [{"table_name": "config", "n_dead_tup": 10, "last_autovacuum": None, "last_vacuum": None}]
        findings = analyze_vacuum_staleness(stats)
        assert len(findings) == 0
