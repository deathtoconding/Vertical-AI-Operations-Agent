# Incident Response

## 1. Severity and response

| Severity | Ack target | Mitigation target | Who |
|---|---|---|---|
| SEV1 | 5 min | 30 min | Incident commander + SRE + service owner |
| SEV2 | 15 min | 2 h | On-call SRE |
| SEV3 | Next business day | 3 days | On-call SRE |
| SEV4 | Tracked | — | Backlog |

## 2. Response loop

```text
detect ─► acknowledge ─► assess ─► mitigate ─► verify recovery ─► communicate ─► review
```

The agent participates up to and including *propose*; **a human owns any SEV1/SEV2 action
decision**. The agent's role in a human-led incident is faster evidence assembly: it
produces the timeline, the correlated deploy, and the blast radius.

## 3. Operating the agent during an incident

1. `GET /api/v1/slo` — confirm the zero-tolerance counters are still zero.
2. `GET /api/v1/incidents?status=open` — the agent's own view of the world.
3. `GET /api/v1/agents/runs/{id}` — the operational trace for a suspicious run.
4. If the agent is behaving unsafely: `POST /api/v1/admin/autonomy` with
   `{"level": "observe_only"}` (admin role, audited, immediate) — see
   `runbooks/unsafe-agent-behaviour.md`.
5. If verification results look wrong: `POST /api/v1/agents/runs/{id}/reverify` re-runs
   verification without repeating the action.

## 4. Communication

Slack channel per incident (`#inc-<incident-id>`), created by the agent's notification;
status updates at minimum every 30 minutes for SEV1/SEV2; a written timeline is exported
from the audit trail at the end.

## 5. Post-incident review

Required for SEV1/SEV2 and for **every** escalation caused by `verification FAILED` or
`UNKNOWN`, and for any `unsafe_action_attempts_total` increment. The review must produce:

* a timeline taken from `audit_events` (not from memory);
* the diagnosis the agent produced and whether it was correct (fed back into `evals/datasets/`);
* at least one test, alert or runbook change — a review with no artefact is incomplete.

## 6. SLO breach handling

Availability budget exhausted → reliability work first. Zero-tolerance objective breached →
release freeze until a regression test reproduces the failure and passes.
