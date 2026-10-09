# pgtriage roadmap

pgtriage is an early-stage, read-only PostgreSQL performance auditor. The current
release covers the core diagnostic loop: collect server telemetry, detect known
performance patterns, and return structured findings over MCP.

## Shipped foundation

- Read-only PostgreSQL sessions and a dedicated least-privilege role guide
- Bounded `EXPLAIN ANALYZE` with query validation, timeout, and rollback
- Table, index, query-plan, configuration, and connection-health analysis
- Schema-scoped audits and catalog-backed type-mismatch detection
- Unit, integration, pgbench, packaging, and CLI smoke tests in CI
- PyPI and official MCP Registry publishing

## Near-term priorities

- Benchmark full-audit latency and output size across larger database estates
- Improve recommendation precision and suppress low-value duplicate findings
- Expand real-plan fixtures for type mismatches, stale statistics, and N+1 patterns
- Add machine-readable benchmark reports for release-to-release comparison
- Publish a short end-to-end demo and operator-focused troubleshooting guide

## Later exploration

- Configurable finding thresholds for different workload profiles
- Additional PostgreSQL version and managed-provider compatibility fixtures
- More granular audit profiles for tables, indexes, queries, and configuration

## Non-goals

- Executing remediation or arbitrary SQL
- Replacing database observability or production monitoring
- Claiming a recommendation is safe without workload-specific human review

Suggestions and reproducible database cases are welcome through
[GitHub issues](https://github.com/pgtriage/pgtriage/issues).
