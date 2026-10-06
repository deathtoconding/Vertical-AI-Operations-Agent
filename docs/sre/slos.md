# SLOs and Error Budgets

> **Status of these numbers:** they are **initial project targets**, not measured claims
> about a production system. They exist so that instrumentation is built before traffic is,
> and they are expected to be retuned against observed behaviour.

## Service level indicators & objectives

| SLI | Definition | Initial target | Metric |
|---|---|---|---|
| API availability | non-5xx responses / total responses (excluding 4xx client errors) | ≥ 99.5% | `http_requests_total{status}` |
| Incident detection latency | anomaly timestamp → incident `created_at` | < 60 s (p95) | `detection_latency_seconds` |
| Investigation latency | investigation start → diagnosis persisted | < 120 s (p95) | `investigation_latency_seconds` |
| Verification latency | action completion → verification outcome | < 30 s (p95) | `verification_latency_seconds` |
| Tool execution success | successful tool invocations / attempts (excluding policy denials) | ≥ 99% | `tool_invocations_total{outcome}` |
| Lost critical incidents | SEV1/SEV2 incidents without a terminal state after 24 h | **0** | `incidents_open_total{severity="SEV1"}` |
| Unauthorized high-risk actions | high/critical actions executed without a valid approval | **0** | `unsafe_action_attempts_total` (must never increment on `outcome="executed"`) |
| Agent run success | runs reaching a terminal state without `FAILED` | ≥ 95% | `agent_run_failure_total` |

## Golden signals

`latency`, `traffic`, `errors`, `saturation` are exported per route, and saturation also
covers the database pool (`db_pool_in_use` / `db_pool_size`).

## Agent-specific signals (§32)

```text
aiops_agent_run_duration_seconds{state}
aiops_agent_run_failure_total{stage}
aiops_agent_run_recovered_total
aiops_tool_invocations_total{tool,risk,outcome}
aiops_tool_failure_rate
aiops_investigation_duration_seconds
aiops_action_success_rate
aiops_verification_total{outcome}
aiops_escalation_total{reason}
aiops_unsafe_action_attempts_total{tool}
aiops_llm_latency_seconds{provider}
aiops_llm_error_total{provider,reason}
aiops_llm_tokens_total{provider,kind}
aiops_detection_latency_seconds
aiops_evidence_collected_total{source}
aiops_evidence_source_failures_total{source}
```

## Error budget policy

* **Availability budget** (0.5% of requests per 30 days): when more than half is consumed,
  feature work pauses and reliability work is prioritised.
* **Zero-tolerance objectives** (lost critical incidents, unauthorized high-risk actions) are
  not error budgets. Any occurrence is a release blocker, investigated as an incident, and
  requires a test that would have caught it.
* **`verification UNKNOWN` rate** is tracked although it is not an SLO: a rising rate means
  verification cannot conclude, which is a silent quality loss and is alerted at > 5%.

## Alerting

Rules live in `infra/deployment/prometheus/rules.yml` and cover: API 5xx rate, p95 latency,
DB pool saturation, LLM error rate, tool failure rate, escalation spike, verification
`UNKNOWN` rate, `unsafe_action_attempts_total > 0` (page immediately), and
`audit_chain_broken > 0` (page immediately).

## Dashboards

`infra/deployment/grafana/dashboard.json` renders: golden signals, the agent funnel
(detected → investigated → proposed → approved → executed → verified → resolved), tool
reliability, LLM cost/latency, SLO burn, and the security panel for the two zero-tolerance
objectives.
