# Verification Model

*Implemented by* `app/verification/{engine.py, checks.py, expected_state.py, outcomes.py}`

## 1. Why it exists

An action executor can only tell you *what it sent*. Verification tells you *what changed*.
Separating them is what allows the system to say "resolved" without lying, and it is the
single most important reliability property of this agent.

## 2. Flow

```text
planner declares expected state  (before execution, part of the action plan)
        │
executor runs the action         (writes its own result — NOT read by the verifier)
        │
verifier observes independently  (re-queries metrics/logs/deployment/payments)
        │
compare within a bounded window  (thresholds, direction, minimum samples)
        │
SUCCESS | FAILED | UNKNOWN
```

## 3. Checks

| Check | Applies to | Independent source | Passes when |
|---|---|---|---|
| `error_rate_below` | `deployment.rollback_simulation`, `incident.escalate` | metrics provider | observed error rate ≤ threshold for the whole window |
| `latency_below` | latency remediation | metrics provider | p95 ≤ threshold with enough samples |
| `deployment_at_target` | rollback | deployment provider | current release == target release, observed *after* the action |
| `payment_failure_rate_below` | payment incidents | payments provider | failure rate ≤ threshold |
| `external_object_exists` | `jira.create_incident` | Jira provider | issue key resolves and is not closed |
| `notification_delivered` | `slack.notify` | Slack provider | message id resolves |

## 4. Outcome semantics

| Outcome | Meaning | System reaction |
|---|---|---|
| `SUCCESS` | Expected state observed, with sufficient data, inside the window | run → `RESOLVED`, incident closed with evidence |
| `FAILED` | Expected state contradicted by sufficient data | run → `ESCALATED`, rollback/regression incident considerations, notify on-call |
| `UNKNOWN` | Cannot decide: insufficient samples, window not elapsed, source unavailable | run → `ESCALATED` with reason `verification_unknown`, **never** `RESOLVED` |

Rules:

* an HTTP 2xx from the executor never produces `SUCCESS`
  (`tests/unit/verification/test_engine.py::test_http_ok_without_effect_is_failed`);
* insufficient data is `UNKNOWN`, not `SUCCESS` (no "benefit of the doubt");
* each check result is persisted with the raw observed values, so a human can audit the
  comparison rather than trusting the verdict.

## 5. Timeline integration

Verification results are written to the incident timeline and the audit chain, which is how
the SRE view (`UI-003`) can render:

```text
13:02:15 action executed        deployment.rollback_simulation → release-41
13:02:40 verification started   checks: error_rate_below, deployment_at_target
13:02:55 verification passed    error_rate 0.012 (≤ 0.02), release release-41 (== target)
13:02:56 incident resolved
```

## 6. SLO

`verification_latency_seconds` (target p95 < 30 s) is exported and alertable; the checks are
bounded by `verification_window_seconds` so verification can always conclude — with
`UNKNOWN` if necessary.
