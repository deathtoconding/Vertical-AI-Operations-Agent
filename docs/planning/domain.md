# OPS-001 — SaaS Operations Domain

> **Story:** OPS-001 · **Epic:** EPIC-01 · **Status:** implemented
> This document is the contract that bounds agent behaviour. If a capability is not
> listed here, the agent does not have it.

---

## 1. Mission

Operate the production surface of a single SaaS service (the *operated system*): notice
when it misbehaves, work out why using evidence rather than vibes, propose the smallest
safe remediation, get human sign-off when the risk demands it, do the thing, and then
**prove from an independent source that the thing worked**.

The agent is an operations *engineer*, not a chatbot. It is judged on the loop
`observe → detect → investigate → propose → approve → execute → verify → resolve`, not
on the quality of its prose.

---

## 2. Supported systems

| System | Role in the domain | MVP access | Adapter |
|---|---|---|---|
| Operated SaaS service (`checkout-service` in the sandbox profile) | The thing being operated | read/act via simulation | `app/sandbox/simulator.py` |
| Deployment platform | Releases, rollback target | read + rollback (simulated) | `app/integrations/live.py`, `app/integrations/sandbox_providers.py` |
| GitHub | Commits, PRs, deployments, repo metadata | **read-only** | `app/integrations/github/client.py` |
| Jira | Incident tickets, context attachments | create/read/update | `app/integrations/jira/client.py` |
| Slack | Operational notification | write-only (webhook) | `app/integrations/slack/client.py` |
| Metrics store (Prometheus-compatible) | Error rate, latency, traffic, saturation | read-only | `app/integrations/metrics/provider.py` |
| Log store (Loki-compatible) | Application and platform logs | read-only | `app/integrations/logs/provider.py` |
| Payments provider (Stripe-compatible) | Payment failures, subscription state | **read-only** | `app/integrations/payments/client.py` |
| PostgreSQL | System of record for the agent itself | read/write (own schema) | `app/persistence/` |

**Tenancy:** single-tenant operations domain. One agent instance operates one SaaS
service. Multi-tenant operation is explicitly out of scope (see §7).

---

## 3. Incident taxonomy

Every incident has a `type` that determines which evidence sources are fetched and which
actions are legal. The taxonomy is closed — an unknown type cannot be created.

| Type | Trigger signal | Primary evidence | Typical remediation | MVP scenario |
|---|---|---|---|---|
| `API_ERROR_SPIKE` | `error_rate` anomaly, upward | error metrics, error logs, recent deploys, commits | `deployment.rollback_simulation`, notify | **A** |
| `API_LATENCY_SPIKE` | `latency_p95` anomaly, upward | latency metrics per endpoint, saturation metrics, deploys | rollback / capacity note, notify | **B** |
| `TRAFFIC_ANOMALY` | `request_rate` anomaly, either direction | traffic metrics, deploys, upstream dependency logs | notify, hold for human | — |
| `SUBSCRIPTION_PAYMENT_ANOMALY` | payment-failure-rate anomaly | payment failures, affected customer list, provider status | notify operations, open ticket, track recovery | **C** |
| `DEPLOYMENT_REGRESSION` | created by an operator or by verification failure | deploy history, commit diff, error logs | rollback, ticket | A/B |
| `TOOL_FAILURE` | repeated tool error rate above threshold | tool invocations, integration errors | escalate, degrade | — |

Severity is assigned from the anomaly score by `app/detection/severity.py`
(`SEV1…SEV4`); an operator may raise severity but not silently lower it.

### Evidence sources

Every evidence item is source-labelled from this closed set, and only these sources can be
queried: `metrics`, `logs`, `github`, `jira`, `slack`, `deployment`, `payments`,
`application_events`, `detection`, `verification`.

---

## 4. Severity model

| Severity | Meaning | Customer impact | Response target | Autonomy |
|---|---|---|---|---|
| `SEV1` | Critical outage or data/payment correctness at risk | Broad, ongoing | Acknowledge < 5 min, mitigate < 30 min | Human leads; agent may propose only |
| `SEV2` | Major degradation, partial customer impact | Significant | Acknowledge < 15 min, mitigate < 2 h | Agent proposes, human approves |
| `SEV3` | Minor degradation or single-feature impact | Limited | Next business day | Agent may act on low-risk tools after approval |
| `SEV4` | Informational / no customer impact | None | Tracked | Agent may act autonomously on read-only + notify |

`AIOPS_AUTONOMY_LEVEL` moves the whole system between `observe_only`,
`approval_required` (default) and `selective_autonomy` (V6 roadmap). Setting it is an
operator action and is audited.

### Actors and roles

| Role | Can read | Can approve | Can execute |
|---|---|---|---|
| `viewer` | everything | no | no |
| `operator` | everything | medium risk | no high/critical risk |
| `sre` | everything | high risk | high risk, after approval |
| `admin` | everything | all | all, plus autonomy and registry changes |

Roles are enforced by the API and by the executor; the UI reflects them but never enforces
them. One role per token, deny-by-default.

---

## 5. Supported actions

Actions are *registered tools*; there is no other path to a side effect.

| Tool | Risk | Permission | Approval | Effect |
|---|---|---|---|---|
| `knowledge.base_example` | LOW | `knowledge.read` | none | Deterministic reference lookup; proves the tool path |
| `github.read_commits` | LOW | `github.read` | none | Read-only commit listing |
| `metrics.query` | LOW | `metrics.read` | none | Read-only metric window |
| `logs.query` | LOW | `logs.read` | none | Read-only log window |
| `jira.create_incident` | MEDIUM | `jira.write` | policy-dependent | Creates an idempotent Jira issue |
| `slack.notify` | MEDIUM | `slack.write` | policy-dependent | Posts a structured operational message |
| `deployment.rollback_simulation` | **HIGH** | `deployment.rollback` | **required** | Moves the sandbox deployment back to the previous release |
| `incident.escalate` | MEDIUM | `incident.write` | none | Escalates to a human, notifies the on-call channel |

### Explicitly blocked (never available to the LLM)

Arbitrary shell, arbitrary SQL, arbitrary HTTP, filesystem access, CI/CD pipeline
triggering, credential/secret reading, customer data export, payment mutation
(refund/charge), direct database writes to the operated system, disabling of monitoring,
deleting or mutating audit records.

This is enforced structurally: `app/tools/registry.py` only knows about registered
tools, and `app/policy/engine.py` denies unknown tools — the LLM cannot name a
capability that does not exist in the registry.

---

## 6. Operational terminology

| Term | Definition |
|---|---|
| **Anomaly** | A deterministic statistical deviation of a metric from its baseline. Not an LLM judgement. |
| **Incident** | The durable record of a detected operational problem and its resolution lifecycle. |
| **Evidence** | A normalised, timestamped, source-labelled fact attached to an incident, addressed by `EV-…` id. |
| **Hypothesis** | A candidate explanation that cites evidence ids, with explicit counter-evidence and confidence. |
| **Diagnosis** | The hypothesis the reasoner selects, plus unsupported claims it explicitly refuses to assert. |
| **Action plan** | An ordered list of registered tool calls with rationale, risk and verification expectations. |
| **Approval** | A recorded human decision bound to the hash of one exact action payload. |
| **Verification** | An independent observation, after execution, of whether the expected state holds. |
| **Agent run** | One execution of the agent workflow for one incident, with a durable state machine. |
| **Escalation** | Handing the incident to a human because the agent cannot safely or successfully proceed. |

---

## 7. Scope boundaries (out of scope for v1)

* Multi-tenant or cross-service operation.
* Autonomous production shell access, arbitrary SQL and arbitrary HTTP (rule 7).
* Self-modifying prompts or agent-authored tool registration.
* Kubernetes operators, microservice decomposition, Kafka, event sourcing.
* ML-based anomaly detection before deterministic baselines prove insufficient.
* Automatic remediation of SEV1 without a human decision.
* Financial mutations (refunds, plan changes) — payment integration is read-only.
* The agent reasoning about its own permissions: policy is not LLM-mediated.

Anything outside this boundary must be *proposed as a plan item*, not silently added
(AI coding rule 1).

---

## 8. Invariants

These are asserted by tests and must never be violated:

1. No side effect occurs without a registered tool and a policy decision.
2. No HIGH or CRITICAL risk action executes without an approval bound to its payload hash.
3. An HTTP 2xx from an integration never constitutes action success on its own.
4. Every consequential step writes an append-only audit record.
5. The LLM output is data. It can never select a tool, role, or approval outcome.
6. Every incident is either `RESOLVED` or `ESCALATED` — no incident disappears.
