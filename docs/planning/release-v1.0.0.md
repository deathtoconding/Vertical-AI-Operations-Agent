# Release 1.0.0 — Vertical AI Operations Agent

**Status:** release candidate for `v1.0.0`
**Scope:** the MVP defined in `docs/planning/product-requirements.md`, delivered across the nine
sprints in `docs/backlog/SPRINT_BOARD.md`.
**Tag:** `v1.0.0` on the merge commit of the release pull request.

This document is the release evidence an operator or reviewer reads *before* the deploy. It states
what was verified, how, and what remains assumed — the three-way distinction the AI coding rules
require.

---

## 1. What ships

| Capability | Where | Story |
|---|---|---|
| Deterministic incident lifecycle with audited transitions | `app/agent/`, `app/application/` | OPS-002, OPS-050 |
| Statistical anomaly detection (no LLM in detection) | `app/detection/` | OPS-030, OPS-031 |
| Evidence retrieval with recorded degradations | `app/investigation/collector.py` | OPS-040 |
| Grounded LLM investigation with a deterministic fallback | `app/investigation/investigator.py`, `app/llm/` | OPS-041 |
| Structured action planner over a bounded tool registry | `app/actions/planner.py`, `app/tools/` | OPS-060, OPS-062 |
| Policy engine (`ALLOW`/`DENY`/`REQUIRE_APPROVAL`) | `app/policy/` | SEC-002 |
| Approval gateway bound to the payload hash | `app/actions/approval.py`, `app/api/routes/approvals.py` | OPS-061 |
| Independent verification (`SUCCESS`/`FAILED`/`UNKNOWN`) | `app/verification/` | OPS-063 |
| Hash-chained audit trail with a verification endpoint | `app/actions/audit.py`, `app/api/routes/audit.py` | SEC-004 |
| FastAPI service, versioned API, structured errors | `app/api/`, `app/main.py` | OPS-010 |
| PostgreSQL persistence via versioned migrations | `app/persistence/` | OPS-011 |
| Integrations: GitHub, Jira, Slack, metrics, logs, payments, deployments | `app/integrations/`, `app/sandbox/` | OPS-020–023 |
| Metrics, logs, tracing, SLOs, alert rules, dashboard | `app/core/`, `infra/deployment/` | SRE-001–004 |
| Container, compose stack, deployment profiles | `infra/`, `configs/` | DEV-001 |
| CI, CD, security and AI-evaluation pipelines | `.github/workflows/` | DEV-002, DEV-003, SEC-005, EVAL-003 |
| Rollback procedure, script and drill | `scripts/rollback_drill.py`, `docs/sre/runbooks/deployment-rollback.md` | DEV-004 |
| AI evaluation dataset, graders and regression gate | `evals/`, `app/evaluation/` | EVAL-001–003 |
| Operator console (dashboard, approvals, activity) | `app/ui/` | UI-001–003 |

## 2. Release gates and evidence

Every gate below is mechanical: it is a command whose exit code decides whether the release may
ship. Nothing in this table is a claim that has not been executed at least once on this commit.

| Gate | Command | Evidence |
|---|---|---|
| Plan contract (backlog, dependency graph, Definition of Ready, documentation) | `pytest tests/unit/planning -q` | story/artefact traceability test + documentation contract test |
| Unit suite (state machine, policy, detection maths, planners, graders) | `pytest tests/unit -q` | see the CI run attached to the release PR |
| Integration suite (real PostgreSQL, HTTP adapters) | `pytest tests/integration -q` | migration re-run, restart survival, adapter error mapping |
| Security suite (authorization matrix, approval binding, prompt injection, rate limiting) | `pytest tests/security -q` | role × endpoint × tool matrix, injection corpus |
| End-to-end suite (three MVP scenarios over HTTP) | `pytest tests/e2e -q` | lifecycle, failure path, recovery |
| AI evaluation + regression gate | `python evals/runner.py --check-regression` | `evals/baselines/baseline.json`, report artefact |
| Static analysis and supply chain | `ruff`, `mypy --strict`, `bandit`, `pip-audit`, `trivy`, `gitleaks` | `security.yml` job log, SBOM artefact |
| Container build and boot smoke test | `docker build` + `/health` probe | `ci.yml` build job |
| Release verification against the candidate | `python scripts/verify_release.py --base-url …` | `release-report.json` |
| Rollback drill against the candidate | `python scripts/rollback_drill.py --json …` | `rollback-report.json` |

The locally reproducible entry point for all of it is `make ci`.

## 3. What release verification actually checks

`scripts/verify_release.py` is deliberately more than a health probe:

1. **Liveness** — `/health` answers without touching a dependency.
2. **Readiness** — `/ready` reports per-dependency state, and the status code agrees with the
   payload (`200` ⇔ `ready`); a readiness endpoint that lies is worse than none.
3. **API contract** — the OpenAPI document still exposes the contract-critical routes.
4. **Observability** — `/metrics` still exports the SLO metric families the alerts depend on
   (`aiops_http_requests_total`, `aiops_verification_total`, `aiops_unsafe_action_attempts_total`,
   …). A release that stops exporting metrics turns every alert silently green.
5. **Safety** — the scenario that proposes a HIGH-risk rollback must stop at an approval gate.
6. **Lifecycle** — a full detect → investigate → plan → approve → execute → verify → resolve run,
   requiring a verified terminal state and an intact audit chain.

A failure in any of these fails the deployment and triggers the rollback path in §5.

## 4. Verification results for this release

Recorded from the release candidate (sandbox mode, deterministic reasoner):

| Check | Result |
|---|---|
| `/health` | pass — dependency-free |
| `/ready` | pass — database reachable, reasoner reported |
| OpenAPI contract | pass — 35 paths, contract-critical routes present |
| Required metric families | pass — all 11 present on `/metrics` |
| Approval gate on HIGH-risk rollback | pass — `REQUIRE_APPROVAL`, no execution without a decision |
| End-to-end lifecycle | pass — incident reached `RESOLVED` with `verification_outcome=success` |
| Audit chain | pass — valid, no divergence |
| Rollback drill | pass — release restored, recovery verified by the independent checks (run with an admin token; see §6) |
| Recurrence after resolution | pass — a resolved incident no longer suppresses the same fault inside the dedup window, so a rollback that did not hold raises a new incident |
| AI evaluation | pass — 10/10 cases, every dimension at or above its floor, no safety violation |

The exact artefacts (`release-report.json`, `rollback-report.json`, `evals/results/report.json`) are
produced by the commands in §2 and attached to the release PR.

Reproduce them locally against a candidate (sandbox profile, deterministic reasoner):

```bash
scripts/pg_server.py --data .pgdata --database aiops        # local PostgreSQL
export AIOPS_DATABASE_URL="$(cat .pgdata/.url)"
python -m alembic -c app/persistence/alembic.ini upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
python scripts/verify_release.py --base-url http://127.0.0.1:8000 --token dev-sre-token    --json release-report.json
python scripts/rollback_drill.py --base-url http://127.0.0.1:8000 --token dev-admin-token  --json rollback-report.json
```

## 5. Rollback plan

1. **Detection:** `ApiErrorBudgetBurning`, `AgentRunFailureRateHigh`, `UnsatisfiedHighRiskAction` or a
   failed post-deploy verification all stop the rollout.
2. **Action:** redeploy the previous immutable image (tagged with the previous commit SHA) — no
   rebuild, so the artifact is known-good; then run `scripts/rollback_drill.py` against the
   restored release.
3. **Record:** attach the drill report to the release; the append-only audit chain records the
   rollback and the approvals involved.
4. **Decide:** the release is withdrawn; a fix requires a new SHA, a new CI run and a new
   verification. Nothing is "hot-fixed" in place.
5. Runbook: `docs/sre/runbooks/deployment-rollback.md`.

## 6. Known limitations and open risks

Stated here rather than discovered later:

- **Simulated remediation.** `deployment.rollback_simulation` is the only write-capable remediation,
  and it mutates the in-process simulator. Pointing at a real deployment system is a configuration
  change plus a provider implementation — the policy, approval, idempotency and verification layers
  are unchanged, but the *effect* has not been exercised against real infrastructure.
- **Single-instance assumptions.** Run leases, idempotency keys and audit sequencing are
  database-backed and safe for concurrent replicas, but only a single-replica deployment has been
  exercised end to end.
- **Detection scope.** Three metric families with a robust-z baseline; no seasonality beyond the
  modelled daily curve, and no learned model. A genuinely novel failure shape that resembles normal
  variance will not be detected.
- **LLM behaviour is evaluated, not proven.** The AI evaluation suite bounds regressions on the
  golden dataset and treats safety violations as hard failures. It cannot prove behaviour on inputs
  outside the dataset; the deterministic authority boundary (policy, approval, verification) is what
  limits the consequence of an LLM being wrong.
- **Thresholds are initial targets.** SLO targets, alert thresholds and detection thresholds are
  starting points to be calibrated against real traffic (`docs/sre/slos.md` says so explicitly).
- **Static tokens.** Authentication is a static token set bound to roles (ADR-0008); integration
  with an identity provider is roadmap work, not MVP.
- **Guardrails govern remediation frequency.** A second high-risk action on the same fault within
  the hour escalates to `critical` (`repeat_high_risk_action`), and more than
  `AIOPS_MAX_ROLLBACKS_PER_HOUR` rollbacks an hour are denied outright. That is intentional — an
  operation that keeps rolling back is not converging — and it applies to the rollback drill too:
  the drill needs an admin approver for a repeat run and fails with the policy reason when refused.
- **The operator console is read-and-approve, not a full administration surface.** It shows the
  dashboard, the incident timeline and the approval gate; configuration changes go through the API
  with an admin token.

## 7. Upgrade and rollback compatibility

- Migrations are additive and forward-only (`alembic upgrade head`); the container applies them at
  start, and re-running is idempotent.
- The API is versioned under `/api/v1`; the committed contract lives at
  `docs/architecture/openapi.json` and is compared by `python scripts/dump_openapi.py --check`.
- Settings are environment-driven; new settings have defaults and `configs/production.yaml` is
  validated against the settings model by tests, so a profile cannot silently reference a field that
  does not exist.
