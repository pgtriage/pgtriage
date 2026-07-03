"""EXPLAIN ANALYZE runner and execution plan analyzer.

Safety model (three layers, all must fail for a write to occur):
  1. Session: SET default_transaction_read_only = true (connection.py)
  2. Validation: only SELECT statements are allowed (this module)
  3. Execution: wrapped in BEGIN/ROLLBACK with statement_timeout (this module)
"""

import re

import psycopg

from pgtriage.connection import ConnectionManager
from pgtriage.models import Category, Finding, Severity

SELECT_PATTERN = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
DANGEROUS_PATTERNS = re.compile(
    r";\s*\S"  # stacked queries
    r"|INTO\s+\w"  # SELECT INTO
    r"|FOR\s+UPDATE"  # SELECT FOR UPDATE (takes locks)
    r"|FOR\s+NO\s+KEY\s+UPDATE"
    r"|FOR\s+SHARE",
    re.IGNORECASE,
)

STATEMENT_TIMEOUT_MS = 10_000  # 10 seconds max per EXPLAIN ANALYZE


def is_safe_to_explain(query: str) -> bool:
    """Check if a query is safe to run through EXPLAIN ANALYZE."""
    if not query or not query.strip():
        return False
    if not SELECT_PATTERN.match(query):
        return False
    if DANGEROUS_PATTERNS.search(query):
        return False
    return True


async def run_explain_analyze(
    db: ConnectionManager,
    query: str,
) -> list[dict] | None:
    """Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) on a SELECT query.

    Safety guarantees:
      - Refuses non-SELECT queries
      - Refuses SELECT INTO, SELECT FOR UPDATE, stacked queries
      - Sets statement_timeout to prevent long-running EXPLAIN
      - Wraps in BEGIN/ROLLBACK so even if read-only is somehow bypassed,
        no changes are committed
      - Connection already has default_transaction_read_only = true

    Returns the JSON plan or None if the query is not safe or fails.
    """
    if not is_safe_to_explain(query):
        return None

    clean_query = query.rstrip().rstrip(";")
    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {clean_query}"

    await db._ensure_connected()
    try:
        async with db.conn.transaction():
            async with db.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                await cur.execute(
                    f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}'"
                )
                await cur.execute(explain_sql)
                row = await cur.fetchone()
                if row and "QUERY PLAN" in row:
                    result = row["QUERY PLAN"]
                else:
                    result = None
            raise _RollbackSignal()
    except _RollbackSignal:
        return result
    except psycopg.errors.QueryCanceled:
        return None
    except psycopg.errors.ReadOnlySqlTransaction:
        return None
    except psycopg.Error:
        return None


class _RollbackSignal(Exception):
    """Raised inside a transaction block to force ROLLBACK."""
    pass


def detect_plan_issues(
    plan_json: list[dict],
    original_query: str | None = None,
) -> list[Finding]:
    """Analyze an EXPLAIN ANALYZE JSON plan for performance issues."""
    if not plan_json:
        return []

    findings = []
    plan = plan_json[0].get("Plan", {})
    _walk_plan_node(plan, findings, original_query)
    return findings


def _walk_plan_node(
    node: dict,
    findings: list[Finding],
    original_query: str | None = None,
) -> None:
    node_type = node.get("Node Type", "")
    relation = node.get("Relation Name")
    actual_rows = node.get("Actual Rows", 0)
    plan_rows = node.get("Plan Rows", 0)

    if node_type == "Seq Scan" and actual_rows > 100_000:
        filter_text = node.get("Filter", "")
        findings.append(Finding(
            severity=Severity.HIGH if actual_rows > 1_000_000 else Severity.MEDIUM,
            category=Category.SEQUENTIAL_SCAN,
            table=relation,
            query=original_query,
            detail=(
                f"Sequential scan on '{relation}' reading {actual_rows:,} rows. "
                f"Filter: {filter_text or 'none'}. "
                f"An index on the filtered columns would likely eliminate this scan."
            ),
            estimated_impact=f"Scanning {actual_rows:,} rows instead of targeted index lookup",
            suggested_fix=(
                f"Identify the columns in the WHERE clause and create a targeted index: "
                f"CREATE INDEX CONCURRENTLY ON {relation} (...);"
                if relation else None
            ),
            evidence={
                "node_type": node_type,
                "actual_rows": actual_rows,
                "filter": filter_text,
                "relation": relation,
            },
        ))

    if plan_rows > 0 and actual_rows > 0:
        estimate_ratio = actual_rows / max(plan_rows, 1)
        if estimate_ratio > 10 or estimate_ratio < 0.1:
            findings.append(Finding(
                severity=Severity.MEDIUM,
                category=Category.STALE_STATS,
                table=relation,
                query=original_query,
                detail=(
                    f"Row estimate is off by {estimate_ratio:.1f}x on '{relation or 'unknown'}'. "
                    f"Planned: {plan_rows:,}, actual: {actual_rows:,}. "
                    f"Table statistics may be stale, causing the planner to pick a bad strategy."
                ),
                suggested_fix=f"ANALYZE {relation};" if relation else "Run ANALYZE on the relevant tables.",
                evidence={
                    "planned_rows": plan_rows,
                    "actual_rows": actual_rows,
                    "estimate_ratio": round(estimate_ratio, 2),
                    "relation": relation,
                },
            ))

    if node_type == "Nested Loop" and actual_rows > 10_000:
        inner = node.get("Plans", [{}])
        inner_type = inner[-1].get("Node Type", "") if inner else ""
        if inner_type == "Seq Scan":
            inner_relation = inner[-1].get("Relation Name", "unknown")
            findings.append(Finding(
                severity=Severity.HIGH,
                category=Category.MISSING_INDEX,
                query=original_query,
                detail=(
                    f"Nested loop join with sequential scan on '{inner_relation}' "
                    f"processing {actual_rows:,} rows. "
                    f"A hash join or index lookup would be faster."
                ),
                estimated_impact="Nested loop + seq scan is the slowest join strategy",
                evidence={
                    "outer_type": node_type,
                    "inner_type": inner_type,
                    "actual_rows": actual_rows,
                    "inner_relation": inner_relation,
                },
            ))

    for child in node.get("Plans", []):
        _walk_plan_node(child, findings, original_query)
