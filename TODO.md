# pgtriage - Launch Checklist

## Must-fix before announcement (the "won't embarrass you in 10 minutes" bar)

### 1. EXPLAIN ANALYZE safety (CRITICAL)
- [ ] Verify it wraps in BEGIN/ROLLBACK (defense in depth on top of read-only session)
- [ ] Verify it refuses non-SELECT statements (already implemented, needs test coverage)
- [ ] Add statement_timeout before running EXPLAIN ANALYZE (prevent slow query adding load)
- [ ] Test against actual DML captured in pg_stat_statements to confirm rejection
- [ ] Document the safety model in README "Safety" section with specifics, not just "read-only by design"

### 2. Output size (PRODUCT VIABILITY)
- [ ] Retest full_audit through Claude Code MCP after the unused-index cap fix
- [ ] Stress test with pgbench-generated database (few hundred tables, pg_stat_statements full of queries)
- [ ] Verify output stays under a reasonable size on worst-case databases

### 3. CI badge green
- [ ] Add GitHub Actions workflow (lint + test on push)
- [ ] Add badge to README

### 4. One loaded-database test pass
- [ ] Set up pgbench with realistic load
- [ ] Run full_audit against it
- [ ] Verify slow query analysis works with pg_stat_statements actually loaded
- [ ] Verify N+1 detection returns sensible results

## Can improve in public (GitHub issues, not reputation events)
- N+1 detection accuracy
- More nuanced index recommendations
- Better duplicate index detection
- Support for more pg_stat_statements columns
- MySQL/MongoDB support (future)
