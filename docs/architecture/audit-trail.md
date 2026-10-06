# Audit Trail

*Implemented by* `app/actions/audit.py`, `app/persistence/repositories/audit.py`

The audit trail answers one question with no room for interpretation: **who did what, when,
on which incident, and with what result** — including when the answer is "the agent tried
and was refused".

## 1. Record shape

```text
audit_event_id   UUID
event_type       AuditEventType (closed vocabulary)
actor            "agent" | "system" | role-qualified human actor id
role             viewer | operator | sre | admin | system
incident_id      nullable
agent_run_id     nullable
tool_name        nullable
action_id        nullable
approval_id      nullable
outcome          success | failure | denied | rejected | info
reason           bounded, human-readable
payload          canonical JSON (redacted, size-bounded)
occurred_at      UTC timestamp
prev_hash        hash of the previous entry
entry_hash       sha256(prev_hash || canonical_json(entry))
```

## 2. Event vocabulary

| Event type | Emitted when |
|---|---|
| `incident_created` | A detection event creates an incident |
| `incident_resolved` | Verification succeeded and the incident closed |
| `incident_escalated` | The run handed the incident to a human |
| `anomaly_detected` | The detector classified a series as anomalous |
| `anomaly_deduplicated` | An anomaly folded into an existing incident |
| `evidence_collected` | Evidence persisted for an incident |
| `evidence_source_degraded` | An evidence source failed and the run continued without it |
| `investigation_completed` | A grounded diagnosis was persisted |
| `investigation_failed` | Investigation could not produce a usable diagnosis |
| `diagnosis_rejected` | A diagnosis cited evidence that does not exist |
| `action_proposed` | The planner produced an action request |
| `action_proposal_rejected` | A proposal named an unknown tool or invalid parameters |
| `policy_evaluated` | Policy returned ALLOW, DENY or REQUIRE_APPROVAL |
| `approval_requested` | A human decision was requested |
| `approval_granted` | A human approved the exact payload |
| `approval_rejected` | A human rejected it |
| `approval_invalidated` | The payload changed after approval |
| `action_executed` | A tool executed (this is *not* a claim that it worked) |
| `action_failed` | A tool failed after its retry policy |
| `action_replayed` | An idempotent replay returned the original result |
| `verification_started` | Independent observation began |
| `verification_completed` | Outcome recorded: SUCCESS, FAILED or UNKNOWN |
| `run_state_changed` | A state-machine transition succeeded |
| `run_recovered` | An interrupted run was recovered safely |
| `authentication_failed` | Missing or invalid credential |
| `authorization_denied` | Authenticated actor lacked the required permission |
| `rate_limit_exceeded` | Rate limiter rejected a request |
| `validation_rejected` | Input validation rejected a payload |
| `prompt_injection_detected` | Untrusted content matched an injection heuristic |
| `unknown_tool_requested` | A proposal named a tool that is not registered |
| `unsafe_action_attempted` | A high-risk action was attempted without a valid approval |
| `autonomy_level_changed` | An admin changed `AIOPS_AUTONOMY_LEVEL` |
| `audit_chain_broken` | Hash-chain verification failed (zero-tolerance alert) |

## 3. Guarantees

* **Append-only.** The repository exposes `append` and read methods only — there is no update
  or delete path, and the API is read-only. `tests/integration/test_audit.py` asserts the
  absence of mutation methods rather than trusting the comment.
* **Hash-chained.** `entry_hash = sha256(prev_hash || canonical_json(payload))`; `verify_chain()`
  walks the chain and reports the first divergent index. `aiops_audit_chain_broken` is a
  paging alert.
* **Redacted.** The payload passes through the same redaction processor as logging, so a
  token that accidentally reaches an audit payload is stored as `***`.
* **Correlated.** Every record carries the ids a human needs to pivot: incident, run, tool,
  action, approval.
* **Survives restart.** It is a PostgreSQL table, not a log line.

## 4. What is deliberately *not* audited

Read-only tool calls against already-recorded evidence (metrics/logs queries) are recorded
as *counters and spans*, not audit rows, to keep the chain focused on consequential steps.
Credential material is never recorded, in any form.
