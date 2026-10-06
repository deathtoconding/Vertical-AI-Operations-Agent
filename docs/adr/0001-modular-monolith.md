# ADR-0001 — Modular monolith, not microservices

* **Status:** Accepted
* **Date:** 2026-10-06
* **Deciders:** engineering
* **Story:** OPS-001, OPS-010

## Context
The domain has clearly separable responsibilities (detection, investigation, agent runtime,
policy, actions, verification). The temptation is to make each a service. That would
introduce network failure modes, distributed transactions, service discovery, per-service
CI/CD and operational overhead for a single-team product with one operated system.

## Decision
One deployable FastAPI application with hard internal module boundaries under `app/`:
`detection`, `investigation`, `agent`, `tools`, `policy`, `actions`, `verification`,
`integrations`, `persistence`. Boundaries are enforced by an import-direction test rather
than by the network.

## Consequences
* One artifact, one migration path, one trace, one transaction manager — much easier to
  reason about for a system that must be auditable and recoverable.
* Modules can be extracted later if a real scaling or blast-radius requirement appears;
  the interfaces (`Protocol` types, repository pattern) already support that.
* Requires discipline: an architecture test (`tests/unit/planning/test_architecture_rules.py`)
  fails the build if a module imports against the allowed direction (e.g. `app/domain`
  importing FastAPI or Slack).
