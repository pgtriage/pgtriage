# Audit run identity and reconciliation

Status: approved architecture decision for the 0.3.0 milestone.

This document freezes the first reconciliation contract for `full_audit`. Its
goal is to let an MCP client recover a known audit result after an ambiguous
transport failure or client-process restart without blindly repeating database
diagnostic work.

## Ownership and identity

- Clients should generate a UUID audit run ID before dispatch and persist it
  with their workflow.
- pgtriage accepts that ID through the optional `audit_run_id` argument.
- pgtriage generates an ID for backwards-compatible callers that omit it.
- A client-generated ID is the only form that supports recovery when the
  initial response, including a server-generated ID, is lost.

An audit run ID is permanently bound to:

- a hashed database identity;
- the normalized requested schema scope; and
- a digest of execution-affecting request arguments.

Reusing an ID with a different binding is a terminal conflict. IDs are never
allowed to select a result from another database or schema.

## Durable registry

pgtriage stores reconciliation records in a local SQLite database outside the
audited PostgreSQL database. The location is configurable and otherwise follows
the platform state directory. The state directory is created with mode `0700`
and the registry with mode `0600` where the operating system supports POSIX
permissions.

The registry contains only the minimum payload required for reconciliation:

- audit run ID, binding digests, and normalized schema scope;
- terminal or in-progress status and bounded failure information;
- start and completion timestamps;
- pgtriage package version and detector-contract version;
- PostgreSQL server version and statistics-reset context when available;
- canonical evidence hash; and
- the completed audit result for bounded recovery.

Credentials, passwords, and raw connection strings are never persisted or
included in hashes. Findings can contain sensitive database metadata, so the
registry is local, permission-restricted, short-lived, and excluded from normal
logs and traces.

Completed result payloads are retained for 24 hours by default. Cleanup removes
the result payload after expiry and retains a minimal `EXPIRED` tombstone long
enough to distinguish an expired result from an operation that never existed.
Retention is configurable. Explicit deletion removes both payload and tombstone.

## Database binding

The database identity stored in the registry is a SHA-256 digest of normalized
connection target metadata: service name, host or host address, port, database,
and database role. Passwords, connection options carrying secrets, and the raw
connection string are excluded. The unhashed target metadata is not persisted.

The operation binding digest is calculated from canonical JSON containing:

- the database identity digest;
- `schema_name`, represented explicitly as `null` when omitted;
- `slow_query_limit`; and
- the detector-contract version.

## State model

The registry uses these states:

- `STARTED`: identity and binding were committed before database diagnostics;
- `COMPLETED`: validated evidence and provenance were committed;
- `FAILED`: execution ended with a bounded failure reason; and
- `EXPIRED`: the recoverable payload was deleted by retention cleanup.

Creating a run and claiming an existing run are atomic. A repeated request with
the same ID and binding returns the existing state. It never starts another
audit. A repeated request with a conflicting binding fails closed.

A `STARTED` record that outlives the configured retention window becomes an
`EXPIRED` tombstone. This prevents a server crash from leaving an operation
permanently "in progress" while still requiring an explicit new run ID rather
than an unsafe automatic replay.

## MCP contract

`full_audit` gains an optional `audit_run_id` argument and returns its existing
audit fields plus a `provenance` envelope. Existing clients that ignore unknown
fields remain compatible.

`get_audit_run(audit_run_id)` is a read-only, idempotent MCP tool. It returns the
known state, provenance, and completed result when still retained. It does not
start or repeat diagnostic work.

`full_audit` remains annotated as non-idempotent in the first release. The safe
recovery path is lookup by operation identity, not blind replay.

## Canonical evidence hash

The evidence hash is SHA-256 over UTF-8 JSON produced with recursively sorted
object keys, compact separators, explicit JSON representations for enums and
timestamps, and rejection of non-finite numbers. The hash input contains the
audit evidence (`findings`, `summary`, and `sections`) and excludes provenance,
timestamps, credentials, connection metadata, and the hash itself.

Equivalent evidence therefore produces the same hash even when audit IDs or run
timestamps differ.

## Client recovery decisions

An Agent Lab workflow persists the audit run ID before MCP dispatch and records
it on tool spans.

After an ambiguous failure or process restart:

- `NOT_FOUND`: dispatch is safe because pgtriage has no committed operation;
- `STARTED`: bounded wait or human review; do not replay;
- `COMPLETED`: recover and validate the saved result;
- `FAILED`: persist the failure; retry only under explicit policy;
- `EXPIRED`: require an explicit rerun decision; and
- conflicting binding: terminal security error.

Unknown or unreachable reconciliation never becomes success and never causes a
blind retry.

## Non-goals

- exactly-once claims without durable evidence;
- storing database credentials;
- automatic SQL remediation;
- general workflow orchestration inside pgtriage; and
- weakening the existing read-only and bounded-execution controls.
