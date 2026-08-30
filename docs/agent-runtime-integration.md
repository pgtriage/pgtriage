# Agent Runtime Integration

pgtriage is a diagnostic MCP server, not an autonomous database operator. An
agent runtime can use its findings as evidence, but the runtime remains
responsible for identity, authorization, budgets, retries, workflow state, and
final-output policy.

## Trust Boundary

Treat all three of these inputs as untrusted data:

- the caller's request;
- model-generated tool plans; and
- pgtriage findings and suggested fixes.

MCP tool annotations describe pgtriage's intended behavior. They are hints,
not an authorization mechanism. A consuming runtime should allowlist the
server and tool, validate arguments, authorize the call immediately before
execution, validate the structured result, and keep remediation advisory until
a human-controlled change process approves it.

## Tool Metadata

| Tool | Read-only | Destructive | Idempotent | Reason |
|---|---:|---:|---:|---|
| `check_table_health` | yes | no | yes | Reads PostgreSQL statistics and relation sizes |
| `check_index_health` | yes | no | yes | Reads index and table catalogs |
| `check_config` | yes | no | yes | Reads configuration and connection statistics |
| `analyze_slow_queries` | yes | no | no | May run bounded `EXPLAIN ANALYZE`; retries consume resources and results can change |
| `full_audit` | yes | no | no | Includes slow-query analysis |

The server also enforces a read-only database session. Slow-query analysis
accepts only validated `SELECT` statements, applies a statement timeout, and
wraps executable plans in a transaction that is rolled back. A dedicated
least-privilege PostgreSQL role is still required because a `SELECT` can call
a volatile function with effects outside the transaction.

## Schema-scoped Audits

`full_audit`, `check_table_health`, `check_index_health`, and
`analyze_slow_queries` accept an optional `schema_name` argument:

```json
{
  "slow_query_limit": 10,
  "schema_name": "accounts"
}
```

When `schema_name` is omitted, pgtriage audits all non-system schemas. When it
is supplied, pgtriage validates the name against `pg_namespace` using a bound
query parameter. Unknown and protected system schemas are rejected.

Table and index collectors filter directly on catalog schema columns.
Slow-query findings are attributed using PostgreSQL's verbose execution-plan
metadata rather than by parsing query text. Configuration and connection
pressure remain database-wide because PostgreSQL does not expose them as
schema-local settings.

## Recommended Control Flow

```text
request validation
  -> caller authentication
  -> intent classification
  -> typed tool plan validation
  -> execution-time authorization
  -> pgtriage MCP call
  -> evidence schema validation
  -> runbook retrieval
  -> advisory remediation synthesis
  -> output schema validation
  -> policy check
  -> completed result
```

Do not expose a generic SQL tool beside pgtriage and assume the model will use
the safer option. Tool registration and permissions should be controlled by
the platform. Do not automatically execute `suggested_fix` values. They are
starting points for validation against workload context, query plans,
constraints, replicas, and operational runbooks.

## Retry Guidance

Retries should depend on the failure class:

- retry transient transport failures within explicit budgets;
- do not retry invalid arguments or denied authorization;
- treat timeouts from slow-query analysis deliberately because a repeated
  `EXPLAIN ANALYZE` can add load;
- persist a tool-result hash before synthesis when durable recovery matters;
- never infer completion solely from a successful model response.

The same idempotency key should resolve to the existing terminal workflow in a
durable agent runtime. That workflow-level guarantee is owned by the runtime,
not by pgtriage.
