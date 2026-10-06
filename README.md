# Vertical AI Operations Agent

A **SaaS operations engineer** that runs as one service: it detects an incident, investigates it,
proposes a remediation, asks a human when the action is risky, executes it through a registered
tool, and then **verifies the outcome independently of whoever executed it**.

The design decision that shapes everything else:

> **The LLM reasons. The deterministic system decides.**
> The model produces hypotheses, evidence references and recommendations. It never owns state,
> policy, authorisation, execution or verification, and it cannot cause a side effect by asking for
> one. Every action passes a policy engine, high-risk actions require a human approval bound to the
> exact payload, and success is decided by the verification engine — never by an HTTP 200.

* **Version:** `1.0.0` (MVP scope, nine sprints — see `CHANGELOG.md`)
* **Stack:** Python 3.11, FastAPI, Pydantic v2, PostgreSQL, SQLAlchemy 2 + Alembic, OpenTelemetry,
  Prometheus/Loki-compatible integrations
* **Shape:** modular monolith (deliberately *not* microservices — ADR-0001)
* **Status:** release candidate, verified by `make ci` + release verification + a rollback drill

---

## 1. What it actually does

| Stage | Owner | What happens |
|---|---|---|
| **Detect** | Deterministic | Robust-z anomaly detection over metric windows. No LLM: "does this look unusual" is statistics (`OPS-030`). |
| **Create incident** | Deterministic | Dedup window (while the incident is still *open* — a recurrence after resolution raises a new incident, so a fix that did not hold stays visible), severity, response targets, an anomaly row linked to the incident (`OPS-031`). |
| **Investigate** | Deterministic + LLM | Evidence is collected from metrics, logs, GitHub, deployments, payments and application events. The model must answer in a strict schema — hypothesis, `evidence_ids`, `counter_evidence_ids`, confidence, recommended actions — and any claim whose evidence ids do not exist is rejected. Untrusted content is treated as data and instruction-like text is flagged, never obeyed. |
| **Plan** | Deterministic | A bounded planner turns the diagnosis into registered tool calls with declared expectations, capped by the settings (e.g. max rollbacks/hour). |
| **Policy** | Deterministic | `ALLOW` / `DENY` / `REQUIRE_APPROVAL` per tool, risk and role. The model cannot argue its way past this. |
| **Approve** | Human | High-risk actions park the run in `WAITING_APPROVAL`. The approval is bound to the `payload_hash`; changing the payload invalidates it. |
| **Execute** | Deterministic | Idempotent invocation with timeouts, bounded retries and per-tool schemas. No shell, SQL, filesystem or arbitrary HTTP tool exists (see §6). |
| **Verify** | Deterministic | Independent checks answer `SUCCESS` / `FAILED` / `UNKNOWN`. The engine is a separate component from the executor by construction — it re-reads the systems. |
| **Resolve / Escalate** | Deterministic | `RESOLVED` only with verified success; otherwise `FAILED`/`ESCALATED`, with an escalation issue and a human owner. |
| **Audit** | Deterministic | Append-only, hash-chained trail of every transition, decision and invocation, verifiable via `GET /api/v1/audit/verify`. |

Three MVP scenarios are simulated end-to-end by the sandbox: **A** API error spike → rollback with
approval, **B** latency spike, **C** subscription/payment anomaly.

## 2. Architecture

```
app/
  api/            FastAPI routers, dependencies, authn/z, error contract, UI hosting
  core/           settings, logging, tracing, metrics, security, sanitisation
  domain/         enums, incidents, actions, policy, tools — no I/O
  detection/      statistical detector + incident factory
  investigation/  evidence collector (per-source fan-out) + grounded investigator
  agent/          orchestrator, run state machine, recovery
  tools/          registered tool definitions (schema, risk, permission, handler)
  policy/         ALLOW / DENY / REQUIRE_APPROVAL engine
  actions/        planner, approval gateway, idempotent executor, audit writer
  verification/   independent checks + verdict aggregation
  integrations/   sandbox | live providers behind one facade (GitHub, Jira, Slack, metrics, logs, payments, deployment)
  persistence/    SQLAlchemy models, repositories, Alembic migrations
  ui/             operator console (static, same-origin, CSP-locked)
  evaluation/     AI evaluation harness support
```

Dependency direction is one-way and enforced in review: `api → application → domain`, with
`domain` importing nothing from the layers above it. See
`docs/architecture/overview.md` and the ten ADRs in `docs/adr/`.

**Incident lifecycle**

```
NEW → DETECTED → INVESTIGATING → PLANNED → WAITING_APPROVAL → EXECUTING → VERIFYING → RESOLVED
                                              └──────────── FAILED / ESCALATED ───────────┘
```

Invalid transitions are rejected by the state machine (not merely discouraged), and every accepted
transition is audited.

## 3. Quickstart

Requires Python 3.11+. No Docker, no external services, no credentials: the default profile runs
against the in-process sandbox and a local PostgreSQL started by the pinned `pgserver` build.

```bash
make install                                    # venv + pinned dev/runtime deps
./.venv/bin/python scripts/pg_server.py --data .pgdata --database aiops &   # or: make pg
export AIOPS_DATABASE_URL="$(cat .pgdata/.url)"  # postgresql+psycopg://… unix socket
make migrate                                     # alembic upgrade head
make run                                         # http://127.0.0.1:8000  (console at /)
```

Drive scenario A end to end (detect → investigate → propose → **stop at the approval gate**):

```bash
curl -s localhost:8000/api/v1/detection/simulate \
     -H 'Authorization: Bearer dev-sre-token' -H 'Content-Type: application/json' \
     -d '{"scenario":"A","orchestrate":true,"reset":true}' | jq .

# what is waiting for a human, and the hash the approval must bind to
curl -s 'localhost:8000/api/v1/approvals?pending_only=true' -H 'Authorization: Bearer dev-sre-token' | jq .

# approve with that exact hash, then resume the run so the executor acts
curl -s -XPOST localhost:8000/api/v1/approvals/<approval_id>/decision \
     -H 'Authorization: Bearer dev-sre-token' -H 'Content-Type: application/json' \
     -d '{"decision":"APPROVED","payload_hash":"<payload_hash>","reason":"on-call approval"}'
curl -s -XPOST localhost:8000/api/v1/agents/runs/<run_id>/resume \
     -H 'Authorization: Bearer dev-sre-token' -H 'Content-Type: application/json' \
     -d '{"reason":"execute approved rollback"}'
```

The incident then reaches `RESOLVED` **only because** the verifier could re-read the deployment and
confirm the metric recovered. If it cannot, the incident escalates — that difference is the product.

Everything CI runs is one command: `make ci`. Individual layers: `make test-unit`,
`make test-integration`, `make test-security`, `make test-e2e`, `make eval-check`.

## 4. Configuration

Settings are environment-driven and validated at boot; production refuses to start with sandbox
integrations, an empty token set, or `trust_proxy` unset. Copy `.env.example` for local work.
Deployment profiles (`configs/staging.yaml`, `configs/production.yaml`) contain **no secrets** —
`scripts/load_profile.py` rejects a profile that names a token, key or password.

| Area | Key settings |
|---|---|
| Core | `AIOPS_ENV`, `AIOPS_LOG_LEVEL`, `AIOPS_API_PREFIX` |
| Database | `AIOPS_DATABASE_URL` (PostgreSQL is the system of record — ADR-0005) |
| Auth | `AIOPS_API_TOKENS` (`token:role:actor_id`, comma separated — ADR-0008) |
| Integrations | `AIOPS_INTEGRATIONS_MODE=sandbox\|live`, then per-system URLs/tokens |
| LLM | `AIOPS_LLM_BASE_URL`, `AIOPS_LLM_API_KEY`, `AIOPS_LLM_MODEL`, `AIOPS_LLM_TIMEOUT_SECONDS` |
| Detection | `AIOPS_DETECTION_WINDOW_MINUTES`, `…_MIN_SAMPLES`, `…_Z_THRESHOLD`, `…_MIN_RELATIVE_DEVIATION` |
| Guardrails | `AIOPS_MAX_ROLLBACKS_PER_HOUR`, `AIOPS_ACTION_TIMEOUT_SECONDS`, `AIOPS_APPROVAL_TIMEOUT_SECONDS` |
| Verification | `AIOPS_VERIFICATION_WINDOW_SECONDS`, `…_POLL_INTERVAL_SECONDS`, `…_MIN_SAMPLES` |
| Autonomy | `AIOPS_AUTONOMY_LEVEL=observe_only\|approval_required\|selective_autonomy` |
| Observability | `AIOPS_TRACING_ENABLED`, `AIOPS_TRACING_EXPORTER=console\|otlp\|none`, `AIOPS_SERVICE_NAME` |

With `AIOPS_LLM_API_KEY` empty the agent uses the deterministic offline reasoner (ADR-0006), which
is what CI and the evaluation harness run. The chosen reasoner is reported in the API response, so a
degraded run is never disguised as a model-backed one.

## 5. API

Versioned under `/api/v1`; the committed contract is `docs/architecture/openapi.json` (35 paths) and
`python scripts/dump_openapi.py --check` fails on accidental drift. Operational endpoints sit
outside the prefix so probes need no credentials: `/health`, `/ready`, `/metrics`, `/slo`,
`/integrations/health`, plus the operator console at `/`.

| Group | Endpoints |
|---|---|
| Detection | `POST /detection/scan`, `POST /detection/simulate`, `GET /detection/metrics` |
| Incidents | `GET /incidents`, `GET /incidents/{id}`, `…/anomalies`, `…/evidence`, `POST …/evidence/collect`, `POST …/investigate` |
| Agents | `GET /agents/runs`, `GET /agents/runs/{id}`, `POST /agents/runs`, `POST …/investigate`, `POST …/resume`, `POST …/reverify`, `POST /agents/recovery` |
| Approvals | `GET /approvals`, `GET /approvals/{id}`, `POST /approvals/{id}/decision` |
| Audit | `GET /audit/events`, `GET /audit/recent`, `GET /audit/export`, `GET /audit/verify` |
| Admin | `GET /admin/tools`, `GET /admin/sandbox`, `POST /admin/sandbox/reset`, `POST /admin/autonomy`, `POST /admin/pool/reset`, `GET /release/current`, `GET /actions/{id}`, `POST /actions/{id}/retry` |
| Observability | `GET /health`, `GET /ready`, `GET /metrics`, `GET /slo`, `GET /integrations/health` |

Errors are a single typed envelope (`code`, `message`, `details`, `correlation_id`) — no endpoint
returns an ad-hoc shape, and a rejected request is audited without storing the rejected payload.

## 6. Tools, policy and approvals

The LLM can only ask for tools that exist in the registry, and every tool declares its risk, its
required permission, its JSON schema and whether it is simulated:

| Tool | Risk | Approval | Permission | Simulated |
|---|---|---|---|---|
| `deployment.rollback_simulation` | high | **yes** | `deployment.rollback` | yes |
| `slack.notify` | medium | no | `slack.write` | no |
| `jira.create_incident` | medium | no | `jira.write` | no |
| `incident.escalate` | medium | no | `incidents.write` | no |
| `github.read_commits` | low | no | `github.read` | no |
| `metrics.query` | low | no | `metrics.read` | no |
| `logs.query` | low | no | `logs.read` | no |
| `knowledge.base_example` | low | no | `knowledge.read` | no |

There is deliberately **no** shell, SQL, filesystem or arbitrary-HTTP tool: an LLM with those has
unbounded authority, which is the failure this system exists to prevent (threat model T-10…T-14).
The only write-capable remediation is a **simulation**, labelled as such in every response and
recorded as `simulated` in the audit trail — see the limitations in §11.

Policy answers *"can this action happen?"*: risk and role can raise the effective risk, a DENY is
recorded as a rejected proposal, and `REQUIRE_APPROVAL` parks the run. Approving is not executing:
the executor runs only when the run is resumed, and it re-verifies that the approval still matches
the payload hash. Human decisions go through the same authenticated, authorised, audited API as
everything else.

## 7. Security

* **RBAC with least privilege** (`SEC-002`): `viewer` reads; `operator` adds incident writes, evidence
  collection, approvals, Jira/Slack; `sre` adds `deployment.rollback` and admin recovery; `admin`
  holds the full set. A viewer attempting a write gets `403 authorization_denied`, and the denial is
  audited.
* **Threat model** across LLM, tools, external inputs, APIs, database, UI and credentials, each with
  a likelihood × impact rating, mitigation and residual risk: `docs/security/threat-model.md`.
  Controls and their proof: `docs/security/security-controls.md`.
* **Prompt injection**: instruction-like content found in evidence is reported as data with
  `injection_flags`, counted in metrics and audited (`prompt_injection_detected`); a corpus of
  adversarial payloads is injected through every evidence source in `tests/security/`.
* **Input validation**: unknown fields are rejected (422), strings are bounded, request bodies are
  schema-validated per tool, and rejected requests increment
  `aiops_validation_rejections_total` and write an audit event — without storing the payload
  (`SEC-006`).
* **Outbound sanitisation**: text sent to Slack/Jira/GitHub is defanged (`@here`/`@channel`/
  `@everyone`, `javascript:`/`data:` links) and bounded.
* **Supply chain** (`SEC-005`): every direct dependency is pinned (`scripts/check_pins.py`),
  `pip-audit`, `bandit` and `gitleaks` are blocking in `security.yml`, an SBOM is generated from the
  installed environment (`make sbom`), and the container image is scanned with Trivy.
* **Secrets**: none in source or config profiles; `.env.example` contains placeholders only, and
  `scripts/scan_secrets.sh` + `check_repo_hygiene.sh` gate the working tree.
* **Audit integrity** (`SEC-004`): append-only hash-chained events; tampering is detected by
  `GET /api/v1/audit/verify`, which returns the first divergence instead of a boolean opinion.

## 8. Observability and SRE

* **Metrics** — 48 `aiops_*` families at `/metrics`, with a boot-time contract assertion so a
  release cannot silently stop exporting the families the alerts depend on.
* **SLOs** — objectives and error budgets in `docs/sre/slos.md`, exposed at `/slo` and backed by
  alert rules in `infra/deployment/prometheus/rules.yml`.
* **Tracing** — OpenTelemetry. One incident produces **one trace** whose id is derived from the
  incident id, so it spans process and request boundaries (approval in one request, execution in
  the next): `lifecycle.*`, `evidence.<source>`, `reasoning.llm`, `policy.decision`, `tool.invoke`,
  verification spans. Attributes carry ids, risk and outcomes — never evidence text or credentials.
* **Runbooks** — database failure, LLM failure, GitHub/Jira unavailability, excessive agent errors,
  unsafe agent behaviour and rollback: `docs/sre/runbooks/`, each with symptoms, impact, diagnosis,
  mitigation and verification, referencing the real endpoints and metrics above.
* **Incident response** — `docs/sre/incident-response.md`.

## 9. Testing

The suite is deterministic, uses a real PostgreSQL for the persistence boundaries, and skips (with a
reason) rather than passing on a lie when a dependency is missing.

| Layer | Command | What it proves |
|---|---|---|
| Unit (382 tests) | `make test-unit` | state machine, policy matrix, detection maths, planners, providers, schemas |
| Integration (122) | `make test-integration` | real PostgreSQL, migrations, repositories, HTTP adapters against mock transports, tracing, audit chain |
| Security (79) | `make test-security` | role × endpoint × tool authorisation, approval binding, injection corpus, rate limiting, secret exposure |
| End-to-end (13) | `make test-e2e` | the three MVP scenarios plus the DEV-004 rollback drill, over the HTTP API |
| AI evaluation (separate) | `make eval-check` | golden dataset behaviour and regression floors — deliberately **outside** `pytest`, because model behaviour is measured, not unit-tested (`evals/`) |

Model behaviour lives in `evals/` (datasets, graders, runner, baseline, report); every normal test
is expected to be deterministic. `tests/unit/planning/` keeps the plan itself honest: the backlog,
the documentation contract, the threat model, the runbooks, the infra manifests and the release
verification are all machine-checked.

## 10. CI, CD and release verification

Four workflows: `ci.yml` (PR gates), `security.yml` (SAST, dependency and image scanning, SBOM),
`ai-evaluation.yml` (evaluation + regression gate) and `cd.yml` (deploy + verification). `make ci`
is the local mirror of the PR pipeline, stage for stage — lint, types, plan contract, unit,
integration, security, end-to-end, evaluation.

Delivery is trunk-oriented: `main` is always releasable, work happens on a branch with a PR, and a
squash merge keeps history readable. Deployment builds an image tagged with the commit SHA (never
`latest` in an environment), auto-deploys to **staging**, requires an approval on the `production`
environment, and then runs the same verification a human would:

```bash
python scripts/verify_release.py  --base-url https://candidate --token "$OPERATOR_TOKEN" --json release-report.json
python scripts/rollback_drill.py  --base-url https://candidate --token "$ADMIN_TOKEN"    --json rollback-report.json
```

`verify_release.py` checks liveness, readiness (status code agreeing with the payload), the OpenAPI
contract, the SLO metric families, the approval gate on a HIGH-risk action, and a full lifecycle run
with an intact audit chain. `rollback_drill.py` establishes a healthy baseline, injects a bad
release, drives the real rollback path (policy → approval → execute) and requires the independent
verifier to confirm recovery — an unverified rollback exits non-zero. It uses an admin token when a
rollback already happened inside the guardrail window (a *repeat* high-risk action escalates to
`critical`), and when policy refuses the proposal the drill fails with that policy reason rather
than reporting a pass. Release evidence, gates and
the rollback plan: `docs/planning/release-v1.0.0.md`.

## 11. Scope, roadmap and honest limitations

MVP scope is deliberately narrow: Kubernetes, microservices, Kafka, event sourcing, vector
databases, multi-agent orchestration, arbitrary shell/SQL/HTTP tools and autonomous high-risk
remediation are **out of scope**, with the roadmap (V0–V6) in `docs/planning/product-requirements.md`.

Known limitations, stated rather than discovered:

* **Remediation is simulated.** `deployment.rollback_simulation` mutates the in-process simulator.
  The policy, approval, idempotency and verification layers are provider-agnostic and unchanged by
  pointing at real infrastructure — but that effect has not been exercised against real systems.
* **Single-replica exercise.** Run leases, idempotency keys and audit sequencing are
  database-backed, but only a single replica has been exercised end to end.
* **Detection is three metric families of robust-z statistics.** A novel failure shape that looks
  like normal variance will not be detected.
* **LLM behaviour is evaluated, not proven.** The consequence of a wrong model is bounded by the
  authority boundary, not by the model's accuracy.
* **Static bearer tokens** (ADR-0008); identity-provider integration is roadmap work.
* **Thresholds are initial targets** to calibrate against real traffic.

## 12. Documentation map

| Document | Contents |
|---|---|
| `docs/planning/product-requirements.md` | problem, users, requirements, MVP scope and non-goals |
| `docs/planning/delivery-plan.md` | epics, sprints, Definition of Done, git and release strategy |
| `docs/planning/testing-strategy.md` | layers, fixtures, what is deliberately not unit-tested |
| `docs/planning/ai-coding-rules.md` | the rules every change in this repo follows |
| `docs/planning/release-gates.md`, `…/release-v1.0.0.md` | gates, evidence, rollback plan, limitations |
| `docs/architecture/` | overview, agent runtime, verification, integrations, audit trail, OpenAPI |
| `docs/adr/` | ten architecture decision records, including the ones that say *no* |
| `docs/security/` | threat model and security controls mapped to tests |
| `docs/sre/` | SLOs, incident response, six runbooks |
| `docs/backlog/` | validated backlog (`backlog.yaml`), Jira-import CSV, sprint board |

## 13. Contributing

Commits use conventional prefixes (`feat:`, `fix:`, `test:`, `refactor:`, `docs:`, `security:`,
`build:`, `ci:`, `perf:`), one story per branch and one story per PR, with tests accompanying every
behavioural change and no silent architecture changes. The full contract — including the rule
against fake implementations and the requirement to distinguish *implemented*, *tested*, *assumed*
and *not verified* — is `docs/planning/ai-coding-rules.md`.
