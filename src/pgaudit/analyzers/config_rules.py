"""Configuration recommendation rules."""

from pgaudit.models import Category, Finding, Severity


def _get_setting(settings: list[dict], name: str) -> str | None:
    for s in settings:
        if s["name"] == name:
            return s["setting"]
    return None


def _parse_memory_kb(value: str, unit: str | None) -> int:
    """Convert a pg_settings memory value to KB."""
    num = int(value)
    if unit == "8kB":
        return num * 8
    if unit == "kB":
        return num
    if unit == "MB":
        return num * 1024
    if unit == "GB":
        return num * 1024 * 1024
    return num


def _get_setting_with_unit(settings: list[dict], name: str) -> tuple[str | None, str | None]:
    for s in settings:
        if s["name"] == name:
            return s["setting"], s.get("unit")
    return None, None


def analyze_config(
    settings: list[dict],
    connection_stats: dict | None,
) -> list[Finding]:
    findings = []

    shared_buffers_val, shared_buffers_unit = _get_setting_with_unit(settings, "shared_buffers")
    if shared_buffers_val and shared_buffers_unit:
        shared_buffers_kb = _parse_memory_kb(shared_buffers_val, shared_buffers_unit)
        if shared_buffers_kb < 128 * 1024:
            findings.append(Finding(
                severity=Severity.HIGH,
                category=Category.CONFIG_ISSUE,
                detail=(
                    f"shared_buffers is {shared_buffers_kb // 1024}MB. "
                    f"For production workloads, this should typically be 25% of available RAM "
                    f"(minimum 128MB for small instances)."
                ),
                suggested_fix="ALTER SYSTEM SET shared_buffers = '256MB'; -- then restart PostgreSQL",
                requires_downtime=True,
                evidence={"shared_buffers_kb": shared_buffers_kb},
            ))

    work_mem_val, work_mem_unit = _get_setting_with_unit(settings, "work_mem")
    if work_mem_val and work_mem_unit:
        work_mem_kb = _parse_memory_kb(work_mem_val, work_mem_unit)
        if work_mem_kb <= 4 * 1024:
            findings.append(Finding(
                severity=Severity.LOW,
                category=Category.CONFIG_ISSUE,
                detail=(
                    f"work_mem is at default ({work_mem_kb // 1024}MB). "
                    f"Complex queries with sorts and hash joins may spill to disk. "
                    f"Consider increasing for workloads with complex queries."
                ),
                suggested_fix="ALTER SYSTEM SET work_mem = '16MB'; -- then SELECT pg_reload_conf();",
                evidence={"work_mem_kb": work_mem_kb},
            ))

    autovacuum_sf = _get_setting(settings, "autovacuum_vacuum_scale_factor")
    if autovacuum_sf:
        sf_val = float(autovacuum_sf)
        if sf_val > 0.1:
            findings.append(Finding(
                severity=Severity.MEDIUM,
                category=Category.CONFIG_ISSUE,
                detail=(
                    f"autovacuum_vacuum_scale_factor is {sf_val} (default 0.2). "
                    f"For large tables, this means autovacuum won't trigger until 20% of rows are dead. "
                    f"On a 10M row table, that's 2M dead rows before cleanup starts."
                ),
                suggested_fix=(
                    "For high-churn tables, set per-table: "
                    "ALTER TABLE <table> SET (autovacuum_vacuum_scale_factor = 0.01);"
                ),
                evidence={"autovacuum_vacuum_scale_factor": sf_val},
            ))

    random_page_cost = _get_setting(settings, "random_page_cost")
    if random_page_cost and float(random_page_cost) > 1.5:
        findings.append(Finding(
            severity=Severity.LOW,
            category=Category.CONFIG_ISSUE,
            detail=(
                f"random_page_cost is {random_page_cost} (default 4.0). "
                f"If your database is on SSD storage, a value of 1.1 better reflects "
                f"actual random read performance and helps the planner choose index scans."
            ),
            suggested_fix="ALTER SYSTEM SET random_page_cost = 1.1; -- then SELECT pg_reload_conf();",
            evidence={"random_page_cost": float(random_page_cost)},
        ))

    log_min_duration = _get_setting(settings, "log_min_duration_statement")
    if log_min_duration and int(log_min_duration) < 0:
        findings.append(Finding(
            severity=Severity.INFO,
            category=Category.CONFIG_ISSUE,
            detail=(
                "log_min_duration_statement is disabled (-1). "
                "Enabling it helps identify slow queries in PostgreSQL logs."
            ),
            suggested_fix=(
                "ALTER SYSTEM SET log_min_duration_statement = 1000; "
                "-- logs queries taking > 1 second"
            ),
            evidence={"log_min_duration_statement": int(log_min_duration)},
        ))

    if connection_stats:
        total = connection_stats.get("total_connections", 0)
        max_conn = connection_stats.get("max_connections", 100)
        utilization = total / max(max_conn, 1) * 100

        if utilization > 80:
            findings.append(Finding(
                severity=Severity.HIGH,
                category=Category.CONNECTION_PRESSURE,
                detail=(
                    f"Connection utilization at {utilization:.0f}% "
                    f"({total}/{max_conn}). "
                    f"Approaching max_connections limit."
                ),
                suggested_fix=(
                    "Consider using a connection pooler (PgBouncer) or "
                    "increasing max_connections if RAM allows."
                ),
                evidence={
                    "total_connections": total,
                    "max_connections": max_conn,
                    "utilization_pct": round(utilization, 1),
                },
            ))

        long_running = connection_stats.get("long_running_queries", 0)
        if long_running > 0:
            findings.append(Finding(
                severity=Severity.HIGH,
                category=Category.LONG_RUNNING_QUERY,
                detail=(
                    f"{long_running} queries running for more than 30 seconds. "
                    f"Long-running queries hold locks and prevent autovacuum."
                ),
                evidence={"long_running_queries": long_running},
            ))

    return findings
