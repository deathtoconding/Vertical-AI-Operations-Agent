# ADR-0010 — Real PostgreSQL in tests via a pip-installable server

* **Status:** Accepted
* **Date:** 2026-10-06
* **Story:** OPS-011, DEV-002

## Context
Integration tests that "prove restart survival" against SQLite prove very little, and a
Docker daemon is not available in every environment (including the sandbox this repository
was built in, and it is a heavy dependency for contributors).

## Decision
Integration tests obtain a real PostgreSQL 16 server through the `pgserver` package: a
pinned, self-contained PostgreSQL distribution started on demand into a temporary data
directory, exposed to tests as a `postgres_url` fixture. CI uses the same mechanism.

## Consequences
* `pytest tests/integration` exercises the production database engine with no daemon and no
  service container, and skips (with a reason) rather than false-passing if unavailable.
* The dependency is dev-only and pinned (`[project.optional-dependencies].localdb`); the
  runtime image talks to a normal PostgreSQL instance.
