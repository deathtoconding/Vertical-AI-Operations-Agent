# Product Requirements — v1.0.0

## 1. Goal

An AI-powered SaaS operations system that continuously observes operational systems,
detects abnormal behaviour, opens incidents, collects evidence, investigates likely
causes, proposes remediation, enforces security and risk policy, requests human approval
where required, executes approved actions, **independently verifies the result**, resolves
or escalates, preserves a complete audit trail, and evaluates its own AI behaviour
through regression tests.

## 2. Functional loop (§1.1 of the master plan)

| # | Capability | Where it lives | Proven by |
|---:|---|---|---|
| 1 | Continuously observe operational systems | `app/integrations/*`, `app/sandbox` | `tests/integration/observability` |
| 2 | Detect abnormal behaviour (deterministic) | `app/detection/detector.py` | `tests/unit/detection` |
| 3 | Create and prioritise incidents | `app/detection/incident_factory.py` | `tests/unit/detection/test_incident_factory.py` |
| 4 | Collect relevant evidence | `app/investigation/collector.py` | `tests/unit/investigation` |
| 5 | Investigate likely causes | `app/investigation/investigator.py` | `tests/unit/investigation` |
| 6 | Produce an evidence-backed diagnosis | `app/llm`, `prompts/investigation` | `evals` diagnosis graders |
| 7 | Propose remediation | `app/actions/planner.py` | `tests/unit/actions` |
| 8 | Enforce security and risk policy | `app/policy/engine.py` | `tests/unit/policy` |
| 9 | Request human approval when necessary | `app/actions/approval.py` | `tests/security/authorization` |
| 10 | Execute approved actions | `app/actions/executor.py` | `tests/unit/actions/test_executor.py` |
| 11 | Independently verify the result | `app/verification/engine.py` | `tests/unit/verification` |
| 12 | Resolve or escalate | `app/agent/orchestrator.py` | `tests/e2e/test_incident_lifecycle.py` |
| 13 | Preserve a complete audit trail | `app/actions/audit.py` | `tests/integration/test_audit.py` |
| 14 | Evaluate its own AI behaviour | `evals/` | `evals/runner.py` + regression gate |

## 3. Non-functional requirements (§2) and how each is met

| Category | Requirement | Implementation |
|---|---|---|
| Reliability | Failed dependencies must not silently lose incidents | Incidents are committed before any external call; failures are recorded as degraded evidence; `tests/integration/test_resilience.py` |
| Security | Least privilege per integration | Read-only GitHub/payments scopes; RBAC matrix; deny-by-default policy |
| AI safety | LLM cannot bypass policy enforcement | `app/policy` never consults the LLM (ADR-0002) |
| Auditability | Every consequential action traceable | Append-only, hash-chained `audit_events` |
| Observability | Metrics, logs, traces | `app/core/telemetry.py`, `logging.py`, `tracing.py` |
| Testability | Components independently testable | Pure domain layer with no framework imports |
| Recoverability | Failed actions retried/reconciled | Idempotency keys + run recovery on startup |
| Explainability | Decisions linked to evidence | Every diagnosis cites `evidence_ids`; unsupported claims flagged |
| Performance | Investigation within target | `investigation_latency_seconds` metric vs 120 s SLO |
| Maintainability | Modular services, clear interfaces | Dependency direction enforced by architecture test |
| Deployment | Reproducible CI/CD | `ci.yml`, `cd.yml`, multi-stage Dockerfile |
| Evaluation | Agent regressions detected | `evals/regression.py` gate in `ai-evaluation.yml` |

## 4. MVP scenarios

See `docs/planning/domain.md` §3. All three are covered end to end by
`tests/e2e/test_incident_lifecycle.py`, and evaluated by `evals/datasets/`.
