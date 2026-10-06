# Architecture Overview

## 1. Logical architecture

```text
                         ┌───────────────────┐
                         │   Operations UI   │  app/ui/static (served by the API)
                         └─────────┬─────────┘
                                   │  (same-origin fetch, bearer token in memory)
                                   ▼
                         ┌───────────────────┐
                         │    API Gateway    │  app/api  — routes only, no business logic
                         └─────────┬─────────┘
                                   ▼
                   ┌───────────────────────────┐
                   │     Agent Orchestrator    │  app/agent
                   │  state machine · runs     │
                   └─────────────┬─────────────┘
                                 │
             ┌───────────────────┼───────────────────┐
             ▼                   ▼                   ▼
      ┌─────────────┐    ┌──────────────┐    ┌──────────────┐
      │ Detection   │    │ Investigation│    │ Policy       │
      │ Engine      │    │ Engine       │    │ Engine       │
      └──────┬──────┘    └──────┬───────┘    └──────┬───────┘
             │                  │                   │
             └──────────────────┼───────────────────┘
                                ▼
                         ┌──────────────┐
                         │ LLM Reasoner │  app/llm  (proposes only — never authorises)
                         └──────┬───────┘
                                ▼
                         ┌──────────────┐
                         │ Action Plan  │  app/actions/planner.py
                         └──────┬───────┘
                                ▼
                         ┌──────────────┐
                         │Risk / Policy │  app/policy  (deterministic)
                         │   Gateway    │
                         └──────┬───────┘
                                ▼
                         ┌──────────────┐
                         │   Approval   │  app/actions/approval.py
                         └──────┬───────┘
                                ▼
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
      ┌────────┐            ┌────────┐            ┌────────┐
      │GitHub  │            │ Jira   │            │ Slack  │
      └────────┘            └────────┘            └────────┘
                                │
                                ▼
                         ┌──────────────┐
                         │ Verification │  app/verification (independent observer)
                         │    Engine    │
                         └──────┬───────┘
                    ┌───────────┴───────────┐
                    ▼                       ▼
                 RESOLVED                ESCALATED
```

## 2. Dependency direction (enforced)

```text
API ──► application services ──► agent ──► {investigation, policy, actions, verification}
                                              │            │           │
                                              ▼            ▼           ▼
                                          integrations ── tools ──► external systems
                                              │
                                              ▼
                                          persistence (repositories) ──► PostgreSQL

domain  ◄── imported by everything, imports nothing (no FastAPI, no SQLAlchemy, no SDK)
```

`tests/unit/planning/test_architecture_rules.py` fails the build if:

* `app/domain/**` imports FastAPI, SQLAlchemy, an HTTP client or an LLM SDK;
* `app/api/routes/**` imports a repository, a tool, or `sqlalchemy` directly (routes go
  through application services);
* any module imports a shell/subprocess capability;
* a second web framework, a second ORM, or a second database driver appears;
* `app/tools/**` imports a shell/SQL/HTTP primitive.

## 3. Module responsibilities

| Module | Owns | Must not |
|---|---|---|
| `app/api` | HTTP surface, auth dependencies, error shapes, rate limiting | contain business rules |
| `app/application` | Use-case orchestration, transactions, idempotency at the use-case level | know about HTTP or LLM prompts |
| `app/agent` | Run lifecycle, state machine, recovery, stage sequencing | decide authorization |
| `app/detection` | Baselines, thresholds, severity, anomaly→incident | call the LLM |
| `app/investigation` | Evidence fan-out, normalisation, ranking, diagnosis validation | execute anything |
| `app/llm` | Provider abstraction, prompt assembly, schema enforcement | hold authority |
| `app/policy` | Risk classification, authorization, approval requirements | be influenced by LLM output beyond the proposal payload |
| `app/actions` | Planning, approval records, execution, idempotency, audit | assume success from HTTP status |
| `app/verification` | Expected state, independent observation, outcome evaluation | read the executor's result |
| `app/tools` | The trust boundary: registry + declared metadata | expose shell/SQL/HTTP |
| `app/integrations` | Transport-level clients for external systems | define what the agent may do |
| `app/persistence` | Models, repositories, migrations, pool | leak ORM objects upward |
| `app/core` | Config, logging, security, errors, telemetry, tracing, sanitisation | become a dumping ground |
| `app/sandbox` | Deterministic operated-system simulator (labelled) | be used when mode is `live` |

## 4. Request lifecycle (typical)

```text
POST /api/v1/agents/runs        Authorization: Bearer <token>
   │
   ├─ rate limit middleware          → 429 (audited) if exceeded
   ├─ authentication dependency      → 401 if token invalid (audited)
   ├─ role guard                     → 403 if role lacks agents:run (audited)
   ├─ request validation (strict)    → 422 on unknown fields / bounds
   ├─ application service            → creates AgentRun, commits BEFORE external calls
   │     └─ orchestrator.step() ...  → each stage writes audit + metrics + spans
   └─ response: run id, state, timeline, next required human input
```

The commit-before-external-work rule is what satisfies "failed dependencies must not
silently lose incidents": the incident is durable *before* anything leaves the process.

## 5. Data model (summary)

```text
incidents ──┬── anomalies            (detection events; dedup key)
            ├── evidence             (EV-… ids, source, kind, confidence, payload)
            ├── agent_runs ──┬── run_transitions   (from_state, to_state, reason, actor)
            │                └── tool_invocations  (tool, risk, outcome, duration)
            ├── action_plans ──── actions ──┬── approvals      (decision bound to payload hash)
            │                               └── verifications  (SUCCESS|FAILED|UNKNOWN)
            └── audit_events         (append-only, hash-chained, incident-scoped)
```

Every table carries `created_at` in UTC; every externally-influenced row carries the id of
the `agent_run` that produced it. See `app/persistence/models/`.

## 6. Failure philosophy

1. **Durable first, then act.** Persist intent before doing anything external.
2. **Degrade, don't disappear.** A failing evidence source produces a labelled degradation,
   never an empty success.
3. **Idempotency everywhere.** Every action carries an idempotency key derived from
   (incident, tool, canonical payload); replays return the original result.
4. **Unknown is a valid answer.** Verification and readiness both have an explicit
   `UNKNOWN`/`degraded` state.
5. **Recovery is safe by construction.** Startup recovery re-checks policy before
   re-executing anything, and never replays a high-risk action without fresh approval.
