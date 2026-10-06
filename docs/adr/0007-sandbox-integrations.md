# ADR-0007 — A contract-complete sandbox for the operated system

* **Status:** Accepted
* **Date:** 2026-10-06
* **Story:** OPS-020…OPS-023, OPS-062, OPS-063

## Context
The agent must *demonstrate* a detected→investigated→remediated→verified loop. Pointing it
at a real SaaS production system is not acceptable for a demo or for CI.

## Decision
`app/sandbox/simulator.py` provides an in-process, deterministic SaaS simulator exposing
the same provider interfaces as the real adapters (metrics, logs, payments, deployments).
It is selected by `AIOPS_INTEGRATIONS_MODE=sandbox` and is the only implementation used in
tests and the local demo. Crucially:

* it is a **full implementation of the provider interfaces**, not a mock of the agent;
* every payload it returns is tagged `"simulated": true`, and the API surfaces that tag;
* a rollout of a bad release genuinely raises the error rate in the simulator, so
  `deployment.rollback_simulation` genuinely lowers it — the verification engine observes a
  real state change, not a canned one.

## Consequences
* The end-to-end safety loop is testable without lying about anything: the only fiction is
  the operated system, and it is labelled.
* Switching to live integrations is a configuration change plus credentials; the agent code
  does not change.
