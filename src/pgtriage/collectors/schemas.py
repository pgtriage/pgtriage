"""Schema validation helpers for scoped audits."""

from pgtriage.connection import ConnectionManager

SCHEMA_EXISTS_QUERY = """
SELECT EXISTS (
    SELECT 1
    FROM pg_namespace
    WHERE nspname = %s
      AND nspname <> 'information_schema'
      AND left(nspname, 3) <> 'pg_'
) AS valid
"""


async def validate_schema_name(
    db: ConnectionManager,
    schema_name: str | None,
) -> None:
    """Reject unknown and system schemas without interpolating identifiers."""
    if schema_name is None:
        return
    if not schema_name:
        raise ValueError("schema_name must not be empty")

    row = await db.fetch_one(SCHEMA_EXISTS_QUERY, (schema_name,))
    if not row or not row.get("valid", False):
        raise ValueError(
            f"Schema '{schema_name}' does not exist or is a protected system schema"
        )
