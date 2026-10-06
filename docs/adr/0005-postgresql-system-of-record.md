# ADR-0005 — PostgreSQL is the system of record; SQLite is a test convenience only

* **Status:** Accepted
* **Date:** 2026-10-06
* **Story:** OPS-011

## Context
Incidents, evidence, agent runs, approvals and audit records must survive restarts, and
the audit trail must be append-only and tamper-evident. This needs real transactions,
`SELECT ... FOR UPDATE`, partial indexes and a text search story.

## Decision
PostgreSQL is the production and integration-test database, accessed through SQLAlchemy 2.0
with Alembic migrations. SQLite (via `aiosqlite`-free stdlib driver) is permitted for the
fast unit-test loop and for a single-node demo, and the difference is explicit:
`tests/integration` runs against real PostgreSQL 16 (booted locally by `pgserver` in the
sandbox and by a service container in CI).

## Consequences
* Integration tests prove restart survival, migration idempotency and index usage.
* The repository layer hides dialect differences, but we do not pretend they do not exist:
  PostgreSQL-only features are guarded and tested under PostgreSQL.
* No ORM object leaks into the domain layer — repositories return domain dataclasses.
