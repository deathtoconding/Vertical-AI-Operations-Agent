# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Entries are written for
an operator, not for a compiler: *what changed, why it matters, and what to verify after
upgrading*.

## [Unreleased]

Nothing yet.

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
- Incident creation with a dedup window and an anomaly row linked to each incident (`OPS-031`).

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
