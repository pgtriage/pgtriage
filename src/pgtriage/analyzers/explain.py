"""EXPLAIN ANALYZE runner and execution plan analyzer.

Safety model (three independent layers protect transactional database state):
  1. Session: SET default_transaction_read_only = true (connection.py)
  2. Validation: only SELECT statements are allowed (this module)
  3. Execution: wrapped in BEGIN/ROLLBACK with statement_timeout (this module)
"""

import re

import psycopg

from pgtriage.connection import ConnectionManager
from pgtriage.models import Category, Finding, Severity

SAFE_START_PATTERN = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
DANGEROUS_PATTERNS = re.compile(
    r";\s*\S"  # stacked queries
    r"|INTO\s+\w"  # SELECT INTO
    r"|FOR\s+UPDATE"  # SELECT FOR UPDATE (takes locks)
    r"|FOR\s+NO\s+KEY\s+UPDATE"
    r"|FOR\s+SHARE",
    re.IGNORECASE,
)
DML_ANYWHERE_PATTERN = re.compile(
    r"\bINSERT\s+INTO\b"
    r"|\bUPDATE\s+\w+\s+SET\b"
    r"|\bDELETE\s+FROM\b"
    r"|\bTRUNCATE\b"
    r"|\bDROP\b"
    r"|\bALTER\b"
    r"|\bCREATE\b",
    re.IGNORECASE,
)

STATEMENT_TIMEOUT_MS = 10_000  # 10 seconds max per EXPLAIN ANALYZE
PARAMETER_PATTERN = re.compile(r"\$(\d+)\b")


def _mask_sql_literals_and_comments(query: str) -> str | None:
    """Blank SQL literals, quoted identifiers, and comments for keyword scans.

    The returned string preserves length and newlines so validation can inspect
    SQL structure without treating harmless text inside a literal or comment as
    executable SQL. Malformed or unterminated input fails closed with ``None``.
    """
    masked = list(query)
    length = len(query)
    i = 0

    def blank(start: int, end: int) -> None:
        for pos in range(start, end):
            if masked[pos] not in ("\n", "\r"):
                masked[pos] = " "

    while i < length:
        if query.startswith("--", i):
            end = query.find("\n", i + 2)
            if end == -1:
                end = length
            blank(i, end)
            i = end
            continue

        if query.startswith("/*", i):
            start = i
            i += 2
            depth = 1
            while i < length and depth:
                if query.startswith("/*", i):
                    depth += 1
                    i += 2
                elif query.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                return None
            blank(start, i)
            continue

        if query[i] == "'":
            start = i
            prefix_position = i - 1
            backslash_escapes = (
                prefix_position >= 0
                and query[prefix_position] in ("e", "E")
                and (
                    prefix_position == 0
                    or not (
                        query[prefix_position - 1].isalnum()
                        or query[prefix_position - 1] in ("_", "$")
                    )
                )
            )
            i += 1
            while i < length:
                if backslash_escapes and query[i] == "\\":
                    i += 2
                elif query[i] == "'":
                    if i + 1 < length and query[i + 1] == "'":
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            else:
                return None
            blank(start, min(i, length))
            continue

        if query[i] == '"':
            start = i
            i += 1
            while i < length:
                if query[i] == '"':
                    if i + 1 < length and query[i + 1] == '"':
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            else:
                return None
            blank(start, i)
            continue

        if query[i] == "$":
            delimiter_match = re.match(
                r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$",
                query[i:],
            )
            if delimiter_match:
                delimiter = delimiter_match.group(0)
                start = i
                content_start = i + len(delimiter)
                end = query.find(delimiter, content_start)
                if end == -1:
                    return None
                i = end + len(delimiter)
                blank(start, i)
                continue

        i += 1

    return "".join(masked)


def is_safe_to_explain(query: str) -> bool:
    """Check if a query is safe to run through EXPLAIN ANALYZE.

    Validation rules:
      - Must start with SELECT (rejects WITH/CTE, INSERT, UPDATE, DELETE, etc.)
      - Must not contain DML keywords anywhere (catches subquery tricks)
      - Must not contain SELECT INTO, FOR UPDATE/SHARE, stacked queries

    Note: volatile functions (SELECT write_function()) can pass lexical
    validation. The dedicated database role must restrict function privileges;
    rollback cannot undo non-transactional external side effects.
    """
    if not query or not query.strip():
        return False
    masked_query = _mask_sql_literals_and_comments(query)
    if masked_query is None:
        return False
    if not SAFE_START_PATTERN.match(masked_query):
        return False
    if DANGEROUS_PATTERNS.search(masked_query):
        return False
    return not DML_ANYWHERE_PATTERN.search(masked_query)


async def run_explain_analyze(
    db: ConnectionManager,
    query: str,
) -> list[dict] | None:
    """Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) on a SELECT query.

    Safety guarantees:
      - Refuses non-SELECT queries
      - Refuses SELECT INTO, SELECT FOR UPDATE, stacked queries
      - Sets statement_timeout to prevent long-running EXPLAIN
      - Wraps in BEGIN/ROLLBACK so transactional changes are not committed
      - Connection already has default_transaction_read_only = true

    Returns the JSON plan or None if the query is not safe or fails.
    """
    if not is_safe_to_explain(query):
        return None

    clean_query = query.rstrip().rstrip(";")
    masked_query = _mask_sql_literals_and_comments(clean_query) or ""
    parameter_numbers = [
        int(match.group(1))
        for match in PARAMETER_PATTERN.finditer(masked_query)
    ]
    if parameter_numbers:
        return await _run_parameterized_explain(
            db,
            clean_query,
            max(parameter_numbers),
        )

    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) {clean_query}"

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


async def _run_parameterized_explain(
    db: ConnectionManager,
    query: str,
    parameter_count: int,
) -> list[dict] | None:
    """Plan a normalized pg_stat_statements query without executing it.

    PostgreSQL 16+ supports GENERIC_PLAN directly. Older supported versions
    fall back to a transaction-scoped prepared statement forced to use a
    generic plan. Both paths avoid inventing parameter values and never run the
    underlying query.
    """
    await db._ensure_connected()
    try:
        async with db.conn.transaction():
            async with db.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                await cur.execute(
                    f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}'"
                )
                await cur.execute(
                    f"EXPLAIN (GENERIC_PLAN, VERBOSE, FORMAT JSON) {query}"
                )
                row = await cur.fetchone()
                result = row.get("QUERY PLAN") if row else None
            raise _RollbackSignal()
    except _RollbackSignal:
        return result
    except psycopg.Error:
        return await _run_prepared_generic_explain(
            db,
            query,
            parameter_count,
        )


async def _run_prepared_generic_explain(
    db: ConnectionManager,
    query: str,
    parameter_count: int,
) -> list[dict] | None:
    """Fallback generic-plan implementation for PostgreSQL 12 through 15."""
    await db._ensure_connected()
    null_parameters = ", ".join("NULL" for _ in range(parameter_count))
    statement_name = "pgtriage_generic_explain"
    try:
        async with db.conn.transaction():
            async with db.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                await cur.execute(
                    f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}'"
                )
                await cur.execute("SET LOCAL plan_cache_mode = force_generic_plan")
                await cur.execute(f"PREPARE {statement_name} AS {query}")
                await cur.execute(
                    "EXPLAIN (VERBOSE, FORMAT JSON) "
                    f"EXECUTE {statement_name} ({null_parameters})"
                )
                row = await cur.fetchone()
                result = row.get("QUERY PLAN") if row else None
            raise _RollbackSignal()
    except _RollbackSignal:
        return result
    except psycopg.Error:
        return None


_IDENTIFIER = r'(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)'
_TYPE_NAME = (
    rf'{_IDENTIFIER}(?:\.{_IDENTIFIER})?'
    r'(?:\s+(?:varying|precision|with(?:out)?\s+time\s+zone))?'
    r'(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?'
    r'(?:\[\])?'
)
_COLON_CAST_PATTERN = re.compile(
    rf'\(*\s*(?:(?P<qualifier>{_IDENTIFIER})\.)?'
    rf'(?P<column>{_IDENTIFIER})\s*\)*\s*::\s*'
    rf'(?P<target_type>{_TYPE_NAME})',
    re.IGNORECASE,
)
_CAST_FUNCTION_PATTERN = re.compile(
    rf'\bCAST\s*\(\s*(?:(?P<qualifier>{_IDENTIFIER})\.)?'
    rf'(?P<column>{_IDENTIFIER})\s+AS\s+'
    rf'(?P<target_type>{_TYPE_NAME})\s*\)',
    re.IGNORECASE,
)

_TYPE_ALIASES = {
    "bigint": "int8",
    "bigserial": "int8",
    "boolean": "bool",
    "character varying": "varchar",
    "double precision": "float8",
    "decimal": "numeric",
    "integer": "int4",
    "real": "float4",
    "smallint": "int2",
    "smallserial": "int2",
    "serial": "int4",
    "time with time zone": "timetz",
    "time without time zone": "time",
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
}


def _unquote_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value.lower()


def _normalize_type(value: str | None) -> str:
    if not value:
        return ""
    normalized = re.sub(r"\s+", " ", value.replace('"', "").strip().lower())
    normalized = re.sub(
        r"\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\)",
        "",
        normalized,
    )
    if "." in normalized:
        normalized = normalized.rsplit(".", 1)[-1]
    return _TYPE_ALIASES.get(normalized, normalized)


def _extract_column_casts(expression: str) -> list[dict[str, str | None]]:
    casts: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str, str]] = set()
    for pattern in (_COLON_CAST_PATTERN, _CAST_FUNCTION_PATTERN):
        for match in pattern.finditer(expression or ""):
            qualifier = _unquote_identifier(match.group("qualifier"))
            column = _unquote_identifier(match.group("column"))
            target_type = _normalize_type(match.group("target_type"))
            if not column or not target_type:
                continue
            key = (qualifier, column, target_type)
            if key in seen:
                continue
            seen.add(key)
            casts.append({
                "qualifier": qualifier,
                "column": column,
                "target_type": target_type,
            })
    return casts


def _rows_examined(node: dict) -> int:
    try:
        loops = max(float(node.get("Actual Loops", 1) or 1), 1.0)
        returned = float(node.get("Actual Rows", 0) or 0) * loops
        removed = float(node.get("Rows Removed by Filter", 0) or 0) * loops
        return int(returned + removed)
    except (TypeError, ValueError):
        return 0


def _relation_index_metadata(
    index_metadata: list[dict],
    relation: str | None,
    schema: str | None,
) -> list[dict]:
    if not relation:
        return []
    return [
        item for item in index_metadata
        if item.get("table_name") == relation
        and (not schema or item.get("schema_name") == schema)
    ]


def _has_matching_expression_index(
    relation_metadata: list[dict],
    column: str,
    target_type: str,
) -> bool:
    for item in relation_metadata:
        expression = item.get("index_expression")
        if not expression:
            continue
        expression_casts = _extract_column_casts(str(expression))
        if any(
            cast["column"] == column
            and cast["target_type"] == target_type
            for cast in expression_casts
        ):
            normalized_expression = re.sub(r"\s+", "", str(expression).lower())
            normalized_column = column.lower()
            normalized_target = target_type.lower()
            exact_forms = {
                f"{normalized_column}::{normalized_target}",
                f"({normalized_column})::{normalized_target}",
                f"(({normalized_column})::{normalized_target})",
                f"cast({normalized_column}as{normalized_target})",
                f"(cast({normalized_column}as{normalized_target}))",
            }
            if normalized_expression in exact_forms:
                return True
    return False


def _detect_index_suppressing_casts(
    node: dict,
    index_metadata: list[dict],
    original_query: str | None,
) -> list[Finding]:
    if node.get("Node Type") != "Seq Scan":
        return []

    relation = node.get("Relation Name")
    schema = node.get("Schema")
    filter_text = str(node.get("Filter", "") or "")
    relation_metadata = _relation_index_metadata(index_metadata, relation, schema)
    if not relation_metadata or not filter_text:
        return []

    catalog_rows = max(
        (
            int(item.get("table_live_rows", 0) or 0)
            for item in relation_metadata
        ),
        default=0,
    )
    rows_examined = max(_rows_examined(node), catalog_rows)
    if rows_examined <= 100_000:
        return []

    result_rows = node.get("Actual Rows", node.get("Plan Rows", 0)) or 0
    try:
        selectivity = float(result_rows) / rows_examined
    except (TypeError, ValueError, ZeroDivisionError):
        selectivity = 0.0
    if selectivity > 0.10:
        # A sequential scan can be the correct plan when the predicate returns
        # a substantial part of the table, even if a compatible index exists.
        return []

    findings: list[Finding] = []
    for cast in _extract_column_casts(filter_text):
        column = str(cast["column"])
        target_type = str(cast["target_type"])
        qualifier = cast["qualifier"]
        alias = node.get("Alias")
        if qualifier and qualifier not in {relation, alias}:
            continue

        column_indexes = [
            item for item in relation_metadata
            if item.get("column_name") == column
            and item.get("is_key_column", True)
            and int(item.get("index_position", 1) or 1) == 1
            and item.get("index_method", "btree") in {"btree", "hash"}
            and not item.get("index_predicate")
        ]
        if not column_indexes:
            continue

        source_types = {
            _normalize_type(str(item.get("column_type", "")))
            for item in column_indexes
            if item.get("column_type")
        }
        if not source_types or target_type in source_types:
            continue
        if _has_matching_expression_index(relation_metadata, column, target_type):
            continue

        source_type = min(source_types)
        index_names = sorted({
            str(item["index_name"])
            for item in column_indexes
            if item.get("index_name")
        })
        findings.append(Finding(
            severity=Severity.HIGH if rows_examined > 1_000_000 else Severity.MEDIUM,
            category=Category.TYPE_MISMATCH,
            table=relation,
            query=original_query,
            detail=(
                f"Filter casts indexed column '{relation}.{column}' from "
                f"{source_type} to {target_type} during a sequential scan examining "
                f"{rows_examined:,} rows. Applying a cast to the column can prevent "
                f"PostgreSQL from using {', '.join(index_names)}."
            ),
            estimated_impact=(
                f"Existing index lookup replaced by a sequential scan over "
                f"{rows_examined:,} rows"
            ),
            suggested_fix=(
                f"Prefer binding or casting the compared value as {source_type} so "
                f"'{relation}.{column}' remains uncast. If the query cannot change, "
                f"validate an expression index on (({column})::{target_type}) with "
                "EXPLAIN before creating it concurrently."
            ),
            safe_to_apply=False,
            evidence={
                "node_type": "Seq Scan",
                "relation": relation,
                "schema": schema,
                "column": column,
                "source_type": source_type,
                "cast_target_type": target_type,
                "filter": filter_text,
                "rows_examined": rows_examined,
                "actual_rows": node.get("Actual Rows", 0),
                "rows_removed_by_filter": node.get("Rows Removed by Filter", 0),
                "estimated_selectivity": round(selectivity, 6),
                "index_names": index_names,
            },
        ))
    return findings


def detect_plan_issues(
    plan_json: list[dict],
    original_query: str | None = None,
    index_metadata: list[dict] | None = None,
) -> list[Finding]:
    """Analyze an EXPLAIN ANALYZE JSON plan for performance issues."""
    if not plan_json:
        return []

    findings = []
    plan = plan_json[0].get("Plan", {})
    _walk_plan_node(plan, findings, original_query, index_metadata or [])
    return findings


def _walk_plan_node(
    node: dict,
    findings: list[Finding],
    original_query: str | None = None,
    index_metadata: list[dict] | None = None,
) -> None:
    node_type = node.get("Node Type", "")
    relation = node.get("Relation Name")
    actual_rows = node.get("Actual Rows", 0)
    plan_rows = node.get("Plan Rows", 0)
    rows_examined = _rows_examined(node)
    cast_findings = _detect_index_suppressing_casts(
        node,
        index_metadata or [],
        original_query,
    )
    findings.extend(cast_findings)

    if node_type == "Seq Scan" and rows_examined > 100_000 and not cast_findings:
        filter_text = node.get("Filter", "")
        findings.append(Finding(
            severity=Severity.HIGH if rows_examined > 1_000_000 else Severity.MEDIUM,
            category=Category.SEQUENTIAL_SCAN,
            table=relation,
            query=original_query,
            detail=(
                f"Sequential scan on '{relation}' examining {rows_examined:,} rows. "
                f"Filter: {filter_text or 'none'}. "
                f"An index on the filtered columns would likely eliminate this scan."
            ),
            estimated_impact=(
                f"Scanning {rows_examined:,} rows instead of targeted index lookup"
            ),
            suggested_fix=(
                f"Identify the columns in the WHERE clause and create a targeted index: "
                f"CREATE INDEX CONCURRENTLY ON {relation} (...);"
                if relation else None
            ),
            evidence={
                "node_type": node_type,
                "actual_rows": actual_rows,
                "rows_examined": rows_examined,
                "rows_removed_by_filter": node.get("Rows Removed by Filter", 0),
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
        _walk_plan_node(child, findings, original_query, index_metadata or [])
