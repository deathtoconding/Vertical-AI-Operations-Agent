# Security Controls (SEC-005, §31 of the master plan)

Every control below names its implementation and the test or CI job that proves it. A
control without a test is a claim, not a control.

| Control | Implementation | Proof |
|---|---|---|
| SAST | `ruff` lint rules incl. flake8-bandit (`S`), `bandit` job | `.github/workflows/security.yml` |
| Dependency scanning | `pip-audit` against pinned requirements; Dependabot | `security.yml`, `.github/dependabot.yml` |
| Secret scanning | `gitleaks` job + pre-commit; redacting log processor | `security.yml`, `tests/unit/core/test_logging.py` |
| Container scanning | Trivy on the built image (CRITICAL/HIGH fail) | `security.yml` |
| SBOM | CycloneDX generated per build, uploaded as an artefact | `cd.yml` |
| RBAC | `app/core/security.py`, `app/policy/authorization.py` | `tests/security/authorization/test_role_matrix.py` |
| Least privilege | Read-only GitHub/payments adapters; per-tool permissions | `tests/unit/tools/test_registry.py` |
| Audit logging | append-only hash-chained `audit_events` | `tests/integration/test_audit.py` |
| Threat modeling | `docs/security/threat-model.md` | `tests/unit/planning/test_documentation.py` |
| Prompt-injection tests | adversarial corpus through every evidence source | `tests/security/prompt_injection/` |
| Input validation | strict Pydantic models, bounds, unknown-field rejection | `tests/security/test_input_validation.py` |
| Rate limiting | per-token/per-IP limiter with 429 + Retry-After | `tests/security/test_rate_limit.py` |
| TLS | terminated by the ingress/proxy; app refuses to start in production without `AIOPS_TRUST_PROXY` set | `configs/production.yaml`, boot validation test |
| Secure secrets | environment/secret manager only; no secrets in source or image | gitleaks, `tests/unit/planning/test_supply_chain.py` |
| Dependency pinning | exact `==` pins for every direct dependency | `tests/unit/planning/test_supply_chain.py` |
| Agent tool boundary | registry + risk + approval, deny-by-default | `tests/unit/tools/test_registry.py`, `tests/security/authorization/` |
| LLM non-authority | policy engine has no LLM dependency | `tests/unit/policy/`, ADR-0002 |

## Enforcement points in code

```text
Request  → rate limit → authentication → role guard → validation → application service
Agent    → registry lookup → schema validation → policy → approval → executor → audit
Verify   → independent observation → SUCCESS | FAILED | UNKNOWN → resolve | escalate
```

## Security events (always audited)

`authentication_failed`, `authorization_denied`, `rate_limit_exceeded`,
`validation_rejected`, `prompt_injection_detected`, `unknown_tool_requested`,
`approval_invalidated`, `autonomy_level_changed`, `audit_chain_broken`.
