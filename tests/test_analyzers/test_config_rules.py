"""Tests for configuration rule analysis."""

from pgtriage.analyzers.config_rules import analyze_config
from pgtriage.models import Category, Severity


def _make_settings(**overrides):
    defaults = {
        "shared_buffers": ("16384", "8kB"),
        "work_mem": ("4096", "kB"),
        "autovacuum_vacuum_scale_factor": ("0.2", None),
        "random_page_cost": ("4", None),
        "log_min_duration_statement": ("-1", None),
    }
    defaults.update(overrides)
    return [
        {"name": name, "setting": val[0], "unit": val[1]}
        for name, val in defaults.items()
    ]


class TestAnalyzeConfig:
    def test_flags_small_shared_buffers(self):
        settings = _make_settings(shared_buffers=("1024", "8kB"))
        findings = analyze_config(settings, None)
        sb_findings = [f for f in findings if "shared_buffers" in f.detail]
        assert len(sb_findings) == 1
        assert sb_findings[0].severity == Severity.HIGH

    def test_flags_default_work_mem(self):
        settings = _make_settings()
        findings = analyze_config(settings, None)
        wm_findings = [f for f in findings if "work_mem" in f.detail]
        assert len(wm_findings) == 1
        assert wm_findings[0].severity == Severity.LOW

    def test_flags_high_autovacuum_scale_factor(self):
        settings = _make_settings()
        findings = analyze_config(settings, None)
        av_findings = [f for f in findings if "autovacuum_vacuum_scale_factor" in f.detail]
        assert len(av_findings) == 1

    def test_flags_high_random_page_cost(self):
        settings = _make_settings()
        findings = analyze_config(settings, None)
        rpc_findings = [f for f in findings if "random_page_cost" in f.detail]
        assert len(rpc_findings) == 1

    def test_flags_connection_pressure(self):
        settings = _make_settings()
        conn_stats = {"total_connections": 90, "max_connections": 100, "active_queries": 10, "idle_connections": 80, "long_running_queries": 0}
        findings = analyze_config(settings, conn_stats)
        cp_findings = [f for f in findings if f.category == Category.CONNECTION_PRESSURE]
        assert len(cp_findings) == 1
        assert cp_findings[0].severity == Severity.HIGH

    def test_flags_long_running_queries(self):
        settings = _make_settings()
        conn_stats = {"total_connections": 10, "max_connections": 100, "active_queries": 5, "idle_connections": 5, "long_running_queries": 3}
        findings = analyze_config(settings, conn_stats)
        lr_findings = [f for f in findings if f.category == Category.LONG_RUNNING_QUERY]
        assert len(lr_findings) == 1

    def test_healthy_config_minimal_findings(self):
        settings = _make_settings(
            shared_buffers=("32768", "8kB"),
            work_mem=("16384", "kB"),
            autovacuum_vacuum_scale_factor=("0.05", None),
            random_page_cost=("1.1", None),
            log_min_duration_statement=("1000", None),
        )
        conn_stats = {"total_connections": 10, "max_connections": 100, "active_queries": 2, "idle_connections": 8, "long_running_queries": 0}
        findings = analyze_config(settings, conn_stats)
        assert len(findings) == 0
