# ADR-0004 — Verification is independent, and "HTTP 200" is not success

* **Status:** Accepted
* **Date:** 2026-10-06
* **Story:** OPS-063

## Context
The most common failure mode in automation is claiming success because the API call did
not error. A rollback endpoint can return `202 Accepted` while the deployment stays broken;
a Jira create can return 200 for a duplicate; a Slack webhook can accept a message nobody
reads. An agent that reports "resolved" on that basis is worse than no agent.

## Decision
Verification is a separate engine with its own inputs and its own vocabulary:

```text
expected state (declared BEFORE execution, by the planner)
        ↓
observe actual state from an INDEPENDENT source (re-query the system of record)
        ↓
compare against thresholds within a bounded window
        ↓
SUCCESS | FAILED | UNKNOWN   (never inferred from the executor's response code)
```

* `UNKNOWN` is a first-class, honest outcome used when data is insufficient or the
  observation window is incomplete — with the reason persisted.
* The verifier does not read the executor's result object.
* `FAILED` and `UNKNOWN` both route to escalation rather than to a claim of success.

## Consequences
* Proved by `tests/unit/verification/test_engine.py::test_http_ok_without_effect_is_failed`.
* Incident resolution is a *verification* outcome, so "resolved" always has evidence
  attached — which is also what makes the SRE timeline and the AI evaluation meaningful.
