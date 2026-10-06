# Integrations

*Implemented by* `app/integrations/**`, exercised by `tests/integration/**`

## 1. The two-layer rule

```text
app/integrations/<system>/     how to talk to the system   (transport, retries, mapping)
app/tools/<name>.py            what the agent may do with it (risk, permission, approval)
```

A new read endpoint in an integration does **not** become an agent capability until a tool
declares it. This is what keeps least privilege honest.

## 2. Provider interfaces

All providers are `typing.Protocol`s so the sandbox and the live clients are
interchangeable, and so tests can substitute a failure-injecting implementation:

| Interface | Methods | Used by |
|---|---|---|
| `MetricsProvider` | `query_metric(service, metric, window)` | detection, verification |
| `LogsProvider` | `query_logs(service, window, severity, contains)` | evidence |
| `GitHubClient` | `list_commits`, `list_pull_requests`, `list_deployments`, `repo_metadata` | evidence |
| `JiraClient` | `create_issue`, `get_issue`, `update_issue`, `add_comment` | actions |
| `SlackClient` | `notify(incident, blocks)` | actions, notifications |
| `PaymentsClient` | `payment_failures(window)`, `affected_customers(window)` | evidence (read-only) |
| `DeploymentClient` | `current_release`, `history`, `rollback(target)` | actions, verification |

## 3. HTTP behaviour (shared by all live clients)

Implemented once in `app/integrations/http.py` and reused:

* **Timeouts** — explicit per-request timeout (`AIOPS_REQUEST_TIMEOUT_SECONDS`, overridable
  per tool), never an unbounded socket.
* **Retries** — bounded, exponential backoff with jitter, only for idempotent verbs and
  only for retryable conditions (connection errors, 429, 5xx). POSTs carry an idempotency
  key so a retry cannot duplicate an issue.
* **Rate limits** — `Retry-After` and `X-RateLimit-Remaining` are honoured and exported as
  metrics (`github_rate_limit_remaining`); a 403 with `remaining: 0` is a distinct,
  non-retryable `RateLimitedError`.
* **Error mapping** — every transport failure becomes a typed error
  (`IntegrationUnavailable`, `IntegrationRateLimited`, `IntegrationAuthenticationError`,
  `IntegrationBadResponse`). **No integration returns an empty success on failure** — this
  rule is asserted in `tests/integration/test_error_contract.py`.
* **Sanitisation** — response bodies are truncated and stripped of control characters
  before becoming evidence, and any credential in a URL/header is redacted from logs.

## 4. Idempotency

`app/integrations/idempotency.py` stores `(scope, idempotency_key) → external_id` in
PostgreSQL. `jira.create_incident` and `slack.notify` are replayed-safe: a second call with
the same canonical payload returns the original issue key instead of creating a duplicate.
The key is `sha256(incident_id | tool_name | canonical_json(params))`, so it changes exactly
when the intended action changes.

## 5. Sandbox mode

`AIOPS_INTEGRATIONS_MODE=sandbox` (default everywhere except staging/production) selects
`app/sandbox/simulator.py` for metrics, logs, payments, deployment and the operated service.
The simulator:

* holds a real, mutable release/error-rate state with deterministic seeded traffic;
* raises the error rate when a bad release is "deployed" and lowers it on rollback — so the
  verification engine observes a genuine state transition;
* tags every payload with `"simulated": true`, which the API surfaces in evidence metadata.

## 6. Failure semantics used by the runbook set

| Failure | Typed error | Agent behaviour | Runbook |
|---|---|---|---|
| GitHub unreachable | `IntegrationUnavailable` | evidence degraded, investigation continues, note recorded | `github-unavailable.md` |
| Jira unreachable | `IntegrationUnavailable` | action fails, run escalates with the incident still durable | `jira-unavailable.md` |
| LLM unreachable / no key | `LLMUnavailable` | deterministic reasoner, `reasoner="deterministic"`, `degraded_reason` set | `llm-failure.md` |
| Metrics store unreachable | `IntegrationUnavailable` | detection skipped for that window (never "no anomaly = healthy") | `database-failure.md` §related |
| Database unreachable | `DatabaseUnavailable` | `/ready` reports not-ready, incidents are not lost (no write attempted) | `database-failure.md` |
