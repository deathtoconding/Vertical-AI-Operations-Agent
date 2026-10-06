# ADR-0009 — Append-only, hash-chained audit trail

* **Status:** Accepted
* **Date:** 2026-10-06
* **Story:** SEC-004

## Context
"Every consequential action is traceable" is only meaningful if traceability cannot be
edited after the fact — by an operator, a bug, or an agent that was talked into it.

## Decision
`audit_events` is append-only at the application layer: no update or delete path exists in
the repository, and the API exposes read-only access. Each row stores
`prev_hash` and `entry_hash = sha256(prev_hash || canonical_json(payload))`, forming a
chain that `verify_chain()` walks; any mutation breaks it.

## Consequences
* Tampering is *detectable*, not merely forbidden (which is the honest claim — a
  determined attacker with database write access can rewrite the whole chain, which is why
  the threat model lists that as a residual risk with the mitigation "ship the chain head
  to external storage/append-only sink" for the next iteration).
* The chain gives the incident timeline and the AI evaluation harness a trustworthy
  ordering of events.
