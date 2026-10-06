# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Entries are written for
an operator, not for a compiler: *what changed, why it matters, and what to verify after
upgrading*.

## [Unreleased]

### Fixed

Defects the v1.0.0 release gate found once the pipelines were run against the release branch. They
are recorded because each one made a control either fail for the wrong reason or never run at all.

- **Secret scanning reported a shell reference as a leak.** The project's own
  `aiops-deploy-credential` rule treated `${DEPLOY_TOKEN:?DEPLOY_TOKEN is required …}` as an
  assignment, so the gitleaks job failed on every pull-request run — and it scans the whole branch
  history, so the finding could not be fixed by editing the tip. The rule now captures the value
  (`secretGroup`), excludes the shell conditional-expansion markers (`:?`, `:-`, `:+`), and
  allowlists references and placeholders per rule. `tests/unit/planning/test_secret_rules.py`
  re-implements the subset of gitleaks' semantics these rules rely on and fails if a custom rule
  reports a non-secret; it fails against the previous ruleset.
- **The container scan never ran.** `aquasecurity/trivy-action@0.29.0` is not a tag (the action
  publishes `vX.Y.Z`), so the job died at *Set up job* before a single step executed, taking the
  SBOM step with it. Pinning to `v0.29.0` was not enough — that release references
  `aquasecurity/setup-trivy@v0.2.2`, which does not exist either. The job is pinned to `v0.36.0`,
  whose `setup-trivy` and `actions/cache` references resolve by SHA and whose inputs were checked
  against its `action.yaml`; the pinning test now rejects a ref that is not a release tag or a SHA.
- **Findings were only visible in a log file.** The secret-scanning job failed with "see job
  summary for details", leaving the findings in a summary and a log, and the image scan reported
  through its exit code alone. `scripts/report_sarif_findings.py` reads the SARIF report each
  scanner already writes and re-emits every result as a check annotation (rule, file, line, and —
  for gitleaks — the commit and `.gitleaksignore` fingerprint; never the secret), so a finding is
  visible on the check run and readable through the API. The annotation steps run with
  `--no-fail`: the scanner is the gate, and a second red step for the same finding only obscures
  which one is which.
- **The database suites were skipped in CI, and the job stayed green.** The suites refuse to fake
  the system of record, and no test database was configured for them, so every schema-dependent
  test skipped and `pytest` exited 0; only the coverage floor noticed (190 skipped on the release
  commit, coverage below the threshold). The test job configures the service database, asserts the
  schema is at `head` before the suites start, and a *configured* database that does not answer is
  now a failure instead of a skip.
- **A red `ci` run did not say what failed.** The test job stopped with a step name and an exit
  code; the failing test and its assertion were only in the log. Every pytest invocation now
  writes JUnit XML and `scripts/report_test_failures.py` re-emits the failures as check
  annotations (test id, file, line, assertion message), with the reports kept as an artefact.
- **The image could not start, and the gate that exists to catch it was broken too.** Three
  defects stacked up behind one red step:
  * the probe curled a container that had already exited — `scripts/entrypoint.sh` refuses to start
    without `AIOPS_DATABASE_URL`, and the smoke test passed none;
  * the dependencies were installed with `pip install --prefix=/install`, a location the
    interpreter never adds to `sys.path`, so even a started container died importing `alembic`:
    the image built cleanly and could never serve a request;
  * `alembic upgrade head` ignored `AIOPS_DATABASE_URL` — `resolve_database_url()` fell through to
    *localhost* whenever there was no local `.pgdata`, so migrations in a container were aimed at a
    database that does not exist there, while the application used the configured one.
  The build installs into a virtual environment that is on `PATH`, the URL resolver prefers the
  configured database (the local socket is now a fallback, and an explicit `-x url` still wins),
  and the smoke test starts a throwaway PostgreSQL, waits for the image to migrate it, and checks
  liveness, readiness and `alembic_version` — annotating the container's own output when it fails.
  Rehearsed outside Docker against a real PostgreSQL: migrations applied, `/health` and `/ready`
  answered 200 with `database.status=ok`.
- **Nine HIGH/CRITICAL advisories that no dependency change could patch.** The image scan reported
  `pip`, `setuptools` and the copies of `wheel`/`jaraco.context` vendored inside setuptools —
  bootstrapping tools that arrive with the base image and with `venv`, are not in this project's
  dependency set, and cannot be upgraded by editing `pyproject.toml`. The runtime stage now removes
  them from both the base interpreter and the application environment: production needs no package
  manager, and the image carries neither the advisories nor the ability to install anything.
- **The CD pipeline could never publish the artifact it promises.** `cd.yml` built
  `ghcr.io/${{ github.repository }}/aiops-agent:${{ github.sha }}`, and `github.repository` is
  `deathtoconding/Vertical-AI-Operations-Agent` — mixed case, which is not a valid Docker image
  reference, so `docker build -t` failed with "invalid reference format" before an image existed
  (the local sandbox has no Docker, which is why the step had never run). The image path is
  lowercased once in the `meta` step the deploy jobs already consume, and build and push are now
  separate steps so "the image does not build" and "the registry refused the push" are told apart.
- **A contract test that passed locally and failed on the runner.** `test_documentation.py`
  asserted that ADR numbers are ordered by reading `Path.glob("*.md")`, which yields directory
  order — a filesystem accident, not a property of the repository. The paths are sorted now and the
  numbers must run `0001..N` with no gap or reuse.
- **The README's own examples were reported as credentials.** The default `curl-auth-header` rule
  reads `curl … -H 'Authorization: Bearer <value>'` as a leaked token, and the README documents the
  sandbox tokens exactly that way — four findings on the release commit, because the README is not
  under `docs/`. The global allowlist now recognises the repository's documented demo token *names*
  (`dev-…`/`test-…`/`ci-…` + role + `-token`); any other value still fails the scan.
- **Strict type checking failed on the CI install profile.** `scripts/pg_server.py` used a static
  `from pgserver import get_server  # type: ignore[attr-defined]`. `pgserver` is the optional
  `localdb` extra and its stubs do not re-export the function, so on the `.[dev]` profile CI
  installs the suppression counted as unused and `mypy --strict` failed the build. The import is
  resolved dynamically now, and a missing extra prints an actionable message instead of a traceback.
- **Bandit failed before its threshold was applied.** The job ran bandit without `--exit-zero`, so
  any finding ended the step before `check_bandit.py` could decide what medium/high means. The two
  real findings are cleared as well: the bind-all-interfaces warning in `app/cli.py` and the
  RNG-for-jitter warning in `app/integrations/http.py` carry bandit's own `# nosec` markers with a
  justification, with ruff's equivalent suppressed per file.

**Verify after upgrading:** `make ci` and `make security` are green; the pinned
`aquasecurity/trivy-action@v0.29.0` tag resolves in the upstream registry; the gitleaks job is green
on a pull request (it scans the branch history, not only the tip).

## [1.0.0] — 2026-10-06

The first release. Scope is the MVP defined in `docs/planning/product-requirements.md`: a
modular-monolith service that detects a SaaS incident, investigates it with an LLM whose authority
is bounded, proposes registered actions, gates high-risk ones behind human approval, executes them
idempotently, and verifies the outcome independently of the executor.

### Added

**Domain and lifecycle**

- Incident domain model with severity (SEV1–SEV4), response targets, and a deterministic
  lifecycle (`NEW → DETECTED → INVESTIGATING → PLANNED → WAITING_APPROVAL → EXECUTING →
  VERIFYING → RESOLVED`, plus `FAILED` and `ESCALATED`). Invalid transitions are rejected with a
  typed error and every transition is written to the audit trail (`OPS-001`, `OPS-002`, `OPS-050`).
- Anomaly detection that is statistical, not model-based: robust-z baselines with a minimum
  sample size and a relative-deviation guard, plus an explicit `insufficient_data` verdict instead
  of a false anomaly (`OPS-030`).
- Incident creation with a dedup window and an anomaly row linked to each incident (`OPS-031`). Deduplication applies to *open* incidents only: once an incident is resolved or escalated, the same deviation recurring opens a new incident (with a derived dedup key), because a fix that did not hold must not be hidden by the window that suppressed the original alert.

**Investigation**

- Evidence collection across metrics, logs, GitHub, deployments, payments and application events,
  with per-source degradation recorded rather than silently swallowed (`OPS-040`).
- LLM investigation that must return a strict schema (hypothesis, evidence ids, counter-evidence,
  confidence, recommended actions). Evidence ids are validated; ungrounded claims are rejected and
  confidence is capped by the evidence that exists (`OPS-041`).
- A deterministic offline reasoner used when no LLM is configured: labelled as such in every
  response, and the reasoner the AI evaluation suite grades by default (`ADR-0006`).

**Policy, approval and execution**

- Policy engine returning exactly one of `ALLOW` / `DENY` / `REQUIRE_APPROVAL`, with the rule name
  that produced the decision. High and critical risk actions can never be allowed, whatever the
  actor (`OPS-060`-adjacent, `SEC-002`).
- Approval gateway bound to the action's payload hash: an approval for one payload cannot authorise
  a different one, decisions are single-shot, and expiry is enforced (`OPS-061`).
- Bounded tool registry (eight tools, no shell, SQL, arbitrary HTTP or filesystem capability) with
  declared risk, permission, timeout and retry policy; unknown tools and malformed parameters are
  refused before any handler runs (`OPS-062`).
- Action executor with per-intent idempotency keys, replayed requests that do not double-execute,
  per-tool timeouts, and an audit record for every attempt (`OPS-062`).

**Verification**

- Verification engine independent of the executor: expectations are declared *before* execution,
  observations come from the providers, and the outcome is `SUCCESS` / `FAILED` / `UNKNOWN` — an
  HTTP 200 is never treated as evidence of success (`OPS-063`).

**Security**

- Static bearer-token authentication with role bindings, per-endpoint permission checks and an
  authorization-denial audit trail (`SEC-002`, `SEC-004`).
- Prompt-injection defence: untrusted evidence is delimited, sanitised, capped in size, scanned for
  instruction-like content, and reported as an observation rather than followed (`SEC-003`).
- Hash-chained audit log with a verification endpoint that reports the first divergence (`SEC-004`).
- Rate limiting per token/IP with `429` and `Retry-After`, strict request models that reject unknown
  fields, and bounded string/collection sizes (`SEC-006`).
- Supply-chain controls: pinned dependencies, a resolved lock file, `pip-audit`, bandit, gitleaks,
  Trivy image scanning, SBOM generation and Dependabot configuration (`SEC-005`).

**Operations**

- 48 Prometheus metrics covering the four golden signals and the agent-specific signals required by
  the SLO document, with a scrape endpoint that excludes high-cardinality labels (`SRE-001`).
- Structured JSON logging with request/trace/incident/run context and secret redaction (`SRE-002`).
- OpenTelemetry tracing with an in-memory exporter for tests (`SRE-003`).
- SLO document, alert rules and a Grafana dashboard checked against the emitted metric names
  (`SRE-004`), and runbooks for the six documented failure modes (`SRE-005`).

**Delivery**

- Multi-stage Dockerfile running as a non-root user with a healthcheck and migrations at startup,
  a compose stack with PostgreSQL/Prometheus/Grafana, and deployment profiles (`DEV-001`).
- CI pipeline (lint, format, types, unit, integration against real PostgreSQL, security, end-to-end,
  coverage floor, container build) runnable locally with `make ci` (`DEV-002`).
- CD pipeline with an immutable SHA-tagged artifact, staging deploy with smoke tests and the AI
  regression suite, and an approval-gated production deploy (`DEV-003`).
- Rollback procedure as a script plus a runbook, exercised by an automated drill (`DEV-004`).
- AI evaluation suite: a versioned golden dataset covering the seven required categories,
  deterministic graders, a regression gate against a stored baseline, and a CI workflow that
  treats a safety violation as a hard failure (`EVAL-001`, `EVAL-002`, `EVAL-003`).
- Operator console (read-only dashboard, approval interface, agent activity view) that talks only
  to the public API (`UI-001`, `UI-002`, `UI-003`).
- Release verification script (`OPS-070`) that checks liveness, readiness, the API contract, the
  required metric families, the approval gate, and drives a full lifecycle to a verified resolution.

### Security notes

- Rollback is executed against a **simulated deployment provider** by default
  (`deployment.rollback_simulation`), which is labelled `simulated: true` in every response, action
  row and API payload. Live mode requires a deliberate configuration change
  (`AIOPS_INTEGRATIONS_MODE=live`) and configured credentials.
- No secret is stored in the repository. `.env.example` contains development placeholders only;
  `scripts/scan_secrets.sh` fails the build if credential-shaped content appears, and production
  boot validation refuses to start with sandbox integrations, an empty token set, or `trust_proxy`
  unset.
- The agent cannot approve, execute or verify its own work: those are separate components with
  separate authorities, and the policy engine is a pure function of declared metadata.

### Known limitations

These are deliberate MVP exclusions, documented in `docs/planning/product-requirements.md` rather
than left implicit:

- Remediation is limited to the simulated rollback plus notification and ticket creation. No
  live Kubernetes, no multi-cluster, no arbitrary infrastructure mutation.
- Background workers, event sourcing, Kafka and multi-agent orchestration are out of scope; the
  agent runs in-process and persists a run state machine.
- Anomaly detection is statistical (robust z-scores) rather than learned; seasonality beyond the
  daily traffic curve is not modelled.
- `verification UNKNOWN` is expected whenever the observation window is too short or a source is
  unreachable — the correct behaviour, but it means not every incident ends with a verdict.
- Alert rules reference initial target thresholds; they have not been calibrated against production
  traffic (see `docs/sre/slos.md`).

[Unreleased]: https://github.com/deathtoconding/Vertical-AI-Operations-Agent/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/deathtoconding/Vertical-AI-Operations-Agent/releases/tag/v1.0.0
