# Runbook — Database failure

**Alerts:** `AIOPSDatabaseNotReady`, `AIOPSDbPoolSaturated`, `AIOPSAuditWriteFailures`
**Metrics:** `aiops_db_pool_in_use`, `aiops_db_pool_size`, `aiops_db_query_failures_total`
**Endpoint:** `GET /api/v1/ready` (reports `database: "unavailable"`)

## Symptoms
* `/health` returns 200 (process alive) while `/ready` returns 503.
* API requests return `503 SERVICE_UNAVAILABLE` with `error.code = "dependency_unavailable"`.
* `db_pool_in_use == db_pool_size` for a sustained period.

## Impact
The agent cannot persist incidents, evidence, approvals or audit records. **By design it
fails closed**: no incident is created in memory and then lost, and no action is executed
without a durable audit path. Detection resumes when the database returns.

## Diagnosis
1. `GET /api/v1/ready` — confirm which dependency reports unavailable.
2. Check the pool: saturated means slow queries or leaked sessions, unavailable means
   connectivity/credentials.
3. `SELECT count(*) FROM pg_stat_activity WHERE state = 'active';`
4. Verify the configured `AIOPS_DATABASE_URL` host is reachable from the pod.
5. Check whether migrations are pending (`alembic current` vs `alembic heads`).

## Mitigation
* Connectivity: restore the database or fix credentials; restart the API pods after the
  dependency is healthy.
* Saturation: identify the long-running query; if it is an evidence fan-out, lower
  `AIOPS_EVIDENCE_CONCURRENCY`. If it is a pool leak, `POST /api/v1/admin/pool/reset`
  (admin, audited) drains and recreates the pool.
* Pending migration: **do not** start the app against a schema it does not understand —
  run `alembic upgrade head` as a pre-deploy step (the container entrypoint does this).

## Verification
1. `/ready` returns 200 with `database: "ok"`.
2. Create a probe incident (`POST /api/v1/detection/simulate`) and confirm it is readable
   after an application restart.
3. `verify_chain()` on the audit table returns `{valid: true}`.

## Prevention
Pool size vs replica count sanity check in CI config lint; query timeout enforced at the
SQLAlchemy level; slow-query alert at p99 > 1 s.
