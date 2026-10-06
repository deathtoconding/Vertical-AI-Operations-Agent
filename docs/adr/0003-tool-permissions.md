# ADR-0003 — Tools are the only door, and they declare their risk

* **Status:** Accepted
* **Date:** 2026-10-06
* **Story:** OPS-051, SEC-002

## Context
The master plan (rule 7) forbids exposing arbitrary shell, SQL, HTTP or filesystem access
to the model. A capability that is not registered must not be reachable, and a registered
capability must carry enough metadata for policy to reason about it.

## Decision
Every capability is a registered tool with mandatory metadata:

```text
name, description, input_schema, output_schema, permission,
risk_level (low|medium|high|critical), requires_approval, timeout_seconds, max_retries
```

Execution path is always: `LLM proposal → registry lookup (unknown = rejected) →
schema validation → policy decision → approval (if required) → executor → audit`.

Integrations (`app/integrations/`) know *how to talk* to a system. Tools
(`app/tools/`) know *what the agent is allowed to do* with it. Keeping them separate means
a new read endpoint never accidentally becomes a new agent capability.

## Consequences
* Authorization is a lookup, not a judgement call.
* Risk classification is data, so it is testable and reviewable in a diff.
* Adding a tool requires: registry entry, risk level, permission, tests, and usually a
  backlog story — exactly the friction we want on the path to production side effects.
