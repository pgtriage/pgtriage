"""Analyze index-health catalog snapshots conservatively."""

from datetime import UTC, datetime, timedelta
from typing import Any

from pgtriage.models import Category, Finding, Severity

MIN_INDEX_STATS_AGE_DAYS = 7


def _quote_identifier(value: str) -> str:
    """Return a PostgreSQL identifier with embedded quotes escaped."""
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _drop_index_sql(index: dict[str, Any]) -> str:
    return (
        "DROP INDEX CONCURRENTLY "
        f"{_quote_identifier(str(index['schema_name']))}."
        f"{_quote_identifier(str(index['index_name']))};"
    )


def _as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _protected_reasons(index: dict[str, Any]) -> list[str]:
    checks = (
        ("is_primary", "primary"),
        ("is_unique", "unique"),
        ("is_exclusion", "exclusion-backed"),
        ("is_constraint_backed", "constraint-backed"),
        ("is_replica_identity", "replica identity"),
        ("is_clustered", "clustered"),
    )
    reasons = [label for key, label in checks if bool(index.get(key))]
    if not bool(index.get("is_valid", True)):
        reasons.append("invalid")
    if not bool(index.get("is_ready", True)):
        reasons.append("not ready")
    if not bool(index.get("is_live", True)):
        reasons.append("not live")
    if str(index.get("index_relkind", "i")) != "i":
        reasons.append("unsupported partitioned index")
    return reasons


def _public_index(index: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": index["schema_name"],
        "name": index["index_name"],
        "qualified_name": f"{index['schema_name']}.{index['index_name']}",
        "scans": int(index.get("idx_scan", 0) or 0),
        "size": index.get("index_size"),
        "size_bytes": int(index.get("index_size_bytes", 0) or 0),
        "definition": index.get("index_definition"),
        "protected_reasons": _protected_reasons(index),
    }


def analyze_duplicate_indexes(
    duplicate_groups: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[Finding]:
    """Build one conservative finding for each structural equivalence class."""
    observed_at = _as_utc(now) or datetime.now(UTC)
    findings: list[Finding] = []

    for group in duplicate_groups:
        indexes = list(group.get("indexes", []))
        if len(indexes) < 2:
            continue

        reset_at = _as_utc(group.get("statistics_reset_at"))
        review_reason: str | None = None
        if reset_at is None:
            review_reason = "Database statistics-reset time is unavailable."
        elif observed_at - reset_at < timedelta(days=MIN_INDEX_STATS_AGE_DAYS):
            review_reason = (
                f"Database usage statistics are younger than "
                f"{MIN_INDEX_STATS_AGE_DAYS} days."
            )

        scan_counts = [int(index.get("idx_scan", 0) or 0) for index in indexes]
        max_scans = max(scan_counts)
        leaders = [
            index
            for index in indexes
            if int(index.get("idx_scan", 0) or 0) == max_scans
        ]
        if review_reason is None and max_scans == 0:
            review_reason = "All equivalent indexes have zero recorded scans."
        elif review_reason is None and len(leaders) != 1:
            review_reason = "The highest recorded scan count is tied."

        keeper = leaders[0] if review_reason is None else None
        candidates = []
        if keeper is not None:
            candidates = [
                index
                for index in indexes
                if index is not keeper and not _protected_reasons(index)
            ]
            if not candidates:
                review_reason = "No non-protected duplicate index is eligible for nomination."
                keeper = None

        public_indexes = [_public_index(index) for index in indexes]
        public_keeper = _public_index(keeper) if keeper is not None else None
        public_candidates = []
        for candidate in candidates if keeper is not None else []:
            candidate_evidence = _public_index(candidate)
            candidate_evidence["sql"] = _drop_index_sql(candidate)
            public_candidates.append(candidate_evidence)

        visible_standbys = group.get("visible_standbys")
        replication_available = bool(group.get("replication_context_available"))
        advisories = [
            "Index-usage statistics are local to the connected PostgreSQL server.",
            "Statistics age does not establish index age because PostgreSQL does not store index creation timestamps.",
        ]
        if replication_available and int(visible_standbys or 0) > 0:
            advisories.append(
                "Verify index usage on replicas before dropping; standby scan counts are not visible here."
            )
        elif not replication_available:
            advisories.append(
                "Replication context could not be read; verify replica usage separately."
            )
        else:
            advisories.append(
                "No directly visible standbys were reported; this does not prove that replicas do not exist."
            )

        table_name = str(group["table_name"])
        schema_name = str(group["schema_name"])
        definitions = ", ".join(
            f"'{item['qualified_name']}' ({item['size']}, {item['scans']:,} scans)"
            for item in public_indexes
        )

        if public_candidates:
            sql = "\n".join(item["sql"] for item in public_candidates)
            detail = (
                f"Structurally equivalent indexes on '{schema_name}.{table_name}': "
                f"{definitions}. '{public_keeper['qualified_name']}' has the uniquely "
                "highest recorded scan count; the remaining eligible indexes are "
                "drop candidates subject to workload review."
            )
            suggested_fix = (
                "Review every compared definition, current and periodic workload, and "
                "replica usage before considering:\n"
                f"{sql}\n"
                "DROP INDEX CONCURRENTLY cannot run inside a transaction block."
            )
        else:
            detail = (
                f"Structurally equivalent indexes on '{schema_name}.{table_name}': "
                f"{definitions}. pgtriage did not nominate a drop candidate because "
                f"{review_reason}"
            )
            suggested_fix = (
                "Review the compared definitions and representative workload manually; "
                "no DROP statement was generated."
            )

        evidence = {
            "structurally_equivalent": True,
            "equivalence": group.get("equivalence", {}),
            "statistics_reset_at": (
                reset_at.isoformat() if reset_at is not None else None
            ),
            "minimum_statistics_age_days": MIN_INDEX_STATS_AGE_DAYS,
            "in_recovery": group.get("in_recovery"),
            "visible_standbys": visible_standbys,
            "replication_context_available": replication_available,
            "indexes": public_indexes,
            "keep_index": public_keeper,
            "drop_candidates": public_candidates,
            "review_only_reason": review_reason,
            "advisories": advisories,
        }
        if len(public_indexes) >= 2:
            evidence["index_1_def"] = public_indexes[0]["definition"]
            evidence["index_2_def"] = public_indexes[1]["definition"]

        findings.append(Finding(
            severity=Severity.MEDIUM,
            category=Category.DUPLICATE_INDEX,
            table=table_name,
            detail=detail,
            suggested_fix=suggested_fix,
            safe_to_apply=False,
            evidence=evidence,
        ))

    return findings
