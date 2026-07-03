"""Deterministic pattern detection on PostgreSQL metrics."""

from datetime import datetime, timezone

from pgtriage.models import Category, Finding, Severity

DEAD_TUPLE_THRESHOLD_PCT = 10.0
LARGE_TABLE_ROWS = 100_000
SEQ_SCAN_RATIO_THRESHOLD = 80.0
VACUUM_STALE_HOURS = 24
TOAST_BLOAT_RATIO = 0.5


def analyze_dead_tuples(table_stats: list[dict]) -> list[Finding]:
    findings = []
    for t in table_stats:
        dead_pct = float(t.get("dead_tuple_pct", 0))
        dead_count = t.get("n_dead_tup", 0)
        live_count = t.get("n_live_tup", 0)

        if dead_pct <= DEAD_TUPLE_THRESHOLD_PCT or dead_count < 1000:
            continue

        if dead_pct > 30:
            severity = Severity.CRITICAL
        elif dead_pct > 20:
            severity = Severity.HIGH
        else:
            severity = Severity.MEDIUM

        findings.append(Finding(
            severity=severity,
            category=Category.DEAD_TUPLES,
            table=t["table_name"],
            detail=(
                f"Table has {dead_count:,} dead tuples ({dead_pct}% of total rows). "
                f"Live rows: {live_count:,}. "
                f"Autovacuum may not be keeping up with the write volume."
            ),
            estimated_impact="Table bloat increases query times and disk usage",
            suggested_fix=(
                f"VACUUM (VERBOSE) {t['table_name']}; "
                f"-- or for severe cases: VACUUM FULL {t['table_name']}; "
                f"(requires exclusive lock)"
            ),
            safe_to_apply=True,
            requires_downtime=False,
            evidence={
                "dead_tuples": dead_count,
                "live_tuples": live_count,
                "dead_tuple_pct": dead_pct,
                "autovacuum_count": t.get("autovacuum_count", 0),
            },
        ))
    return findings


def analyze_sequential_scans(table_stats: list[dict]) -> list[Finding]:
    findings = []
    for t in table_stats:
        live_rows = t.get("n_live_tup", 0)
        seq_scan_pct = float(t.get("seq_scan_pct", 0))
        seq_scans = t.get("seq_scan", 0)
        idx_scans = t.get("idx_scan", 0)

        if live_rows < LARGE_TABLE_ROWS:
            continue
        if seq_scan_pct < SEQ_SCAN_RATIO_THRESHOLD:
            continue
        if seq_scans < 100:
            continue

        if live_rows > 1_000_000:
            severity = Severity.HIGH
        else:
            severity = Severity.MEDIUM

        findings.append(Finding(
            severity=severity,
            category=Category.SEQUENTIAL_SCAN,
            table=t["table_name"],
            detail=(
                f"Table has {live_rows:,} rows with {seq_scan_pct}% sequential scans "
                f"({seq_scans:,} seq vs {idx_scans:,} idx). "
                f"Likely missing an index on frequently queried columns."
            ),
            estimated_impact="Sequential scans on large tables cause slow queries under load",
            suggested_fix=(
                f"Identify the most common WHERE clauses on {t['table_name']} "
                f"and add targeted indexes with CREATE INDEX CONCURRENTLY."
            ),
            evidence={
                "live_rows": live_rows,
                "seq_scans": seq_scans,
                "idx_scans": idx_scans,
                "seq_scan_pct": seq_scan_pct,
            },
        ))
    return findings


def analyze_vacuum_staleness(table_stats: list[dict]) -> list[Finding]:
    findings = []
    now = datetime.now(timezone.utc)

    for t in table_stats:
        dead_count = t.get("n_dead_tup", 0)
        if dead_count < 1000:
            continue

        last_vacuum = t.get("last_autovacuum") or t.get("last_vacuum")
        if last_vacuum is None:
            findings.append(Finding(
                severity=Severity.MEDIUM,
                category=Category.AUTOVACUUM_LAG,
                table=t["table_name"],
                detail=(
                    f"Table has never been vacuumed but has {dead_count:,} dead tuples. "
                    f"Autovacuum may not be configured or may not have triggered yet."
                ),
                suggested_fix=f"VACUUM ANALYZE {t['table_name']};",
                evidence={"dead_tuples": dead_count, "last_vacuum": None},
            ))
            continue

        if last_vacuum.tzinfo is None:
            last_vacuum = last_vacuum.replace(tzinfo=timezone.utc)

        hours_since = (now - last_vacuum).total_seconds() / 3600
        if hours_since > VACUUM_STALE_HOURS and dead_count > 10_000:
            findings.append(Finding(
                severity=Severity.MEDIUM,
                category=Category.AUTOVACUUM_LAG,
                table=t["table_name"],
                detail=(
                    f"Table has {dead_count:,} dead tuples and hasn't been vacuumed "
                    f"in {hours_since:.0f} hours. Last vacuum: {last_vacuum}."
                ),
                suggested_fix=f"VACUUM ANALYZE {t['table_name']};",
                evidence={
                    "dead_tuples": dead_count,
                    "hours_since_vacuum": round(hours_since, 1),
                    "last_vacuum": str(last_vacuum),
                },
            ))
    return findings


def analyze_table_bloat(table_sizes: list[dict]) -> list[Finding]:
    findings = []
    for t in table_sizes:
        table_bytes = t.get("table_size_bytes", 0)
        toast_bytes = t.get("toast_size_bytes", 0)

        if table_bytes < 10_000_000:
            continue
        if toast_bytes <= 0:
            continue

        toast_ratio = toast_bytes / max(table_bytes, 1)
        if toast_ratio < TOAST_BLOAT_RATIO:
            continue

        findings.append(Finding(
            severity=Severity.MEDIUM,
            category=Category.TOAST_BLOAT,
            table=t["table_name"],
            detail=(
                f"TOAST storage ({t.get('toast_size', 'N/A')}) is "
                f"{toast_ratio:.1f}x the table size ({t.get('table_size', 'N/A')}). "
                f"Large JSONB or TEXT columns may be causing bloat. "
                f"TOAST tables have separate autovacuum tracking and can lag behind."
            ),
            estimated_impact="TOAST bloat increases disk usage and slows full table operations",
            evidence={
                "table_size": t.get("table_size"),
                "toast_size": t.get("toast_size"),
                "total_size": t.get("total_size"),
                "toast_ratio": round(toast_ratio, 2),
            },
        ))
    return findings
