# Agent Runtime

*Implemented by* `app/agent/{state.py, state_machine.py, run_manager.py, orchestrator.py, recovery.py}`

## 1. Lifecycle (OPS-002)

```text
NEW ──► DETECTED ──► INVESTIGATING ──► PLANNED ──► WAITING_APPROVAL ──► EXECUTING
                                                         │                   │
                                                         │ (rejected)        ▼
                                                         └──────────────► VERIFYING
                                                                             │
                                                        ┌────────────────────┴────────────┐
                                                        ▼                                 ▼
                                                    RESOLVED                         ESCALATED

Any state ──► FAILED        (unrecoverable error in that stage)
FAILED    ──► INVESTIGATING (allowed: retry after operator intervention)
ESCALATED ──► INVESTIGATING (allowed: human hands it back with new information)
```

## 2. Transition rules

| From | Allowed to |
|---|---|
| `NEW` | `DETECTED`, `FAILED`, `ESCALATED` |
| `DETECTED` | `INVESTIGATING`, `FAILED`, `ESCALATED` |
| `INVESTIGATING` | `PLANNED`, `FAILED`, `ESCALATED` |
| `PLANNED` | `WAITING_APPROVAL`, `EXECUTING`, `FAILED`, `ESCALATED` |
| `WAITING_APPROVAL` | `EXECUTING`, `PLANNED`, `FAILED`, `ESCALATED` |
| `EXECUTING` | `VERIFYING`, `FAILED`, `ESCALATED` |
| `VERIFYING` | `RESOLVED`, `FAILED`, `ESCALATED` |
| `RESOLVED` | `ESCALATED` (regression after resolution) |
| `FAILED` | `INVESTIGATING`, `ESCALATED` |
| `ESCALATED` | `INVESTIGATING` |

`PLANNED → EXECUTING` is only legal when **every** action in the plan is policy-permitted
without approval. Otherwise the run must pass through `WAITING_APPROVAL` — this is the
structural reason "high-risk actions cannot bypass approval": the state machine itself has
no edge from `PLANNED` to `EXECUTING` for those plans, and the transition guard re-evaluates
policy from the database, not from in-memory state.

Invalid transitions raise `InvalidTransition` and write an audit event with
`outcome=rejected`. They are counted in `agent_transitions_total{result="invalid"}`.

## 3. Persistence and concurrency

* Every transition is appended to `run_transitions` with `from_state`, `to_state`, `actor`,
  `reason`, `occurred_at` (UTC) and the run's optimistic version.
* The run row carries `version`; a transition uses `UPDATE ... WHERE version = :expected`
  so two concurrent workers cannot both advance the same run.
* Stages are idempotent per `(run_id, stage)` via the idempotency store, so a retry after a
  crash re-reads state rather than repeating side effects.

## 4. Recovery (interrupted runs)

On startup (and on demand via `POST /api/v1/agents/recovery`), `recovery.py` scans for runs
in a non-terminal state whose `updated_at` is older than the lease timeout and:

1. marks the interrupted stage as `recovered` in the audit trail;
2. re-reads the persisted state;
3. for `EXECUTING`, re-checks policy and *does not* re-execute a high- or critical-risk
   action without a fresh approval record — the run moves to `WAITING_APPROVAL` instead;
4. otherwise resumes from the persisted state.

`agent_run_recovered_total` is emitted, and `tests/e2e/test_recovery.py` kills a run
mid-execution and asserts no duplicate side effect occurs.

## 5. Orchestrator shape

```python
async def step(run_id: UUID) -> RunSnapshot:
    run = repo.get(run_id)
    state = StateMachine(run.state)

    if state.is_terminal:
        return snapshot(run)

    match state.value:
        case "DETECTED":        await detect_and_collect(run)
        case "INVESTIGATING":   await investigate(run)
        case "PLANNED":         await plan(run)          # → WAITING_APPROVAL | EXECUTING
        case "WAITING_APPROVAL":await await_approval(run) # no-op until a decision exists
        case "EXECUTING":       await execute(run)
        case "VERIFYING":       await verify(run)         # → RESOLVED | ESCALATED

    return snapshot(repo.get(run_id))
```

The orchestrator owns **workflow**, never **truth**: it does not decide whether an action
is allowed (policy does) or whether it worked (verification does).

## 6. Observability per run

```text
trace_id ─┬─ span detection.run
          ├─ span investigation.collect        (one child span per evidence source)
          ├─ span llm.complete                 (tokens, latency, provider)
          ├─ span policy.evaluate
          ├─ span approval.wait                (human latency)
          ├─ span action.execute               (per tool)
          └─ span verification.observe         (per check)
```

Every span carries `incident.id`, `agent.run_id` and `tool.name` where relevant, which is
what makes the SRE timeline in `docs/sre/slos.md` and the AI evaluation in `evals/` possible.
