# Release Gates (section 38 of the master plan)

A release is only valid when every box below is checked **with evidence**. The evidence
column names the artefact or command that proves it — `scripts/verify_release.py` checks
the machine-checkable subset automatically.

## FUNCTIONAL
| Gate | Evidence |
|---|---|
| Acceptance criteria passed | `docs/planning/verification-report.md` (story → test mapping) |
| E2E scenarios pass (A, B, C) | `tests/e2e/test_incident_lifecycle.py`, `tests/e2e/test_production_simulation.py` |

## QUALITY
| Gate | Evidence |
|---|---|
| Unit tests pass | `pytest tests/unit` |
| Integration tests pass | `pytest tests/integration` (PostgreSQL 16) |
| AI evaluation passes | `evals/runner.py` + `evals/baselines/baseline.json` |

## SECURITY
| Gate | Evidence |
|---|---|
| No critical vulnerabilities | `pip-audit`, `bandit`, Trivy in `security.yml` |
| No leaked secrets | gitleaks job; `tests/security/secrets/` |
| RBAC verified | `tests/security/authorization/test_role_matrix.py` |
| Prompt-injection tests pass | `tests/security/prompt_injection/` |

## RELIABILITY
| Gate | Evidence |
|---|---|
| Health checks pass | `scripts/verify_release.py` against `/health`, `/ready` |
| SLO instrumentation works | `/metrics` contains every SLO SLI (asserted in `tests/integration/test_observability.py`) |
| Rollback tested | `scripts/rollback_drill.py` + `tests/e2e/test_rollback_drill.py` |

## OPERATIONS
| Gate | Evidence |
|---|---|
| Runbooks exist | `docs/sre/runbooks/*.md` (six runbooks, section-validated) |
| Dashboards exist | `infra/deployment/grafana/dashboard.json` |
| Alerts configured | `infra/deployment/prometheus/rules.yml` |
| Audit logging works | `tests/integration/test_audit.py` (append-only + hash chain) |

## RELEASE
| Gate | Evidence |
|---|---|
| Artifact immutable | image tagged with commit SHA; `cd.yml` never retags |
| Version tagged | `v1.0.0` on `main` |
| Deployment verified | `scripts/verify_release.py` exit 0 |
| Post-release monitoring active | `docs/sre/incident-response.md` watch window |

## Honest status of the automated portion

`scripts/verify_release.py` performs the **live** checks (health, readiness, metrics,
SLO series, incident-creation smoke, agent-run smoke, audit chain). The gates that
require infrastructure this repository does not provision (a real registry scan, a real
production cluster) are implemented as CI jobs and documented as **not verified locally**
— see `docs/planning/verification-report.md` for the exact wording.
