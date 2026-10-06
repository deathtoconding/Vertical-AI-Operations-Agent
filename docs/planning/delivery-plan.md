# Delivery Plan — how this repository was built

**Product:** Vertical AI Operations Agent — SaaS Operations Engineer
**Baseline:** `main` · **Version:** 1.0.0 · **Stories:** 40 · **Points:** 206 · **Sprints:** 9

The plan is machine-readable in [`docs/backlog/backlog.yaml`](../backlog/backlog.yaml) and
is validated by `tests/unit/planning/test_backlog.py` on every CI run — an invalid plan
cannot be merged, and the Jira export cannot be generated from one.

---

## The three delivery stages

The request that produced this repository was *plan → implement → deploy, testing at every
stage*. That maps onto three stages with explicit exit criteria:

| Stage | Goal | Exit criteria (all tested) |
|---|---|---|
| **1. Plan** | Requirements, domain, architecture, ADRs, Jira-ready backlog | Backlog validates (structure, Fibonacci, dependency ordering, sprint capacity, critical path, Definition of Ready); threat model, SLOs and ADRs exist and are cross-checked by tests |
| **2. Implement** | The vertical slice and every P0 story on top of it | `make ci` green: lint, format, types, unit + integration (real PostgreSQL) + security + eval suites |
| **3. Deploy** | Reproducible artifact, gated release, verified release, tested rollback | Container builds, compose stack boots, release verification script passes against a running candidate, rollback drill passes |

**Correction adopted from the master plan (§Sprint 8):** the original Sprint 8 was 36
points and was split into Sprint 8 (Evaluation + CD, 23) and Sprint 9 (UI + Release).
The realised board is therefore 9 sprints. See `SPRINT_BOARD.md`.

---

## Sprint board

| Sprint | Name | Points | Outcome |
|---|---|---:|---|
| 1 | Foundation | 24 | Deployable backend with database and CI foundation |
| 2 | Integrations | 21 | System can collect operational evidence |
| 3 | Detection + SRE | 26 | Anomalies become incidents; metrics, SLOs, API hardening |
| 4 | AI Investigation | 24 | Agent can investigate incidents |
| 5 | Controlled Actions | 23 | Agent can propose and execute approved actions |
| 6 | Verification + Reliability | 21 | Agent verifies its actions; tracing, runbooks, rollback drill |
| 7 | Security | 26 | Security controls become enforceable |
| 8 | Evaluation + CD | 23 | AI regressions detected automatically; releases gated |
| 9 | Operations UI + Release | 18 | Operators run the system; release validated |

Full board with per-sprint stories: [`SPRINT_BOARD.md`](../backlog/SPRINT_BOARD.md).

---

## Critical path

```text
OPS-001 → OPS-010 → OPS-011 → OPS-040 → OPS-041 → OPS-050 → OPS-051
       → OPS-060 → OPS-061 → OPS-062 → OPS-063
```

`Backlog.spine_is_intact()` asserts these remain a connected dependency chain, and
`test_dependencies_are_never_scheduled_later` asserts no story is scheduled before the
work it depends on.

---

## The vertical slice built first

Per the master plan (§23), the first executable artefact is the complete safety loop, not
a horizontal layer:

```text
anomaly → incident → evidence → investigation → action proposal → policy
        → approval → execution → independent verification → RESOLVED | ESCALATED
```

Everything else (UI, extra integrations, evaluation harness) is additive around that
slice. `tests/e2e/test_incident_lifecycle.py` walks exactly this path over the public API
for all three MVP scenarios.

---

## Traceability

Every story declares its artefacts (`implemented_by`) and its test approach
(`test_approach`). Tests carry `@pytest.mark.story("OPS-030")`, so
`pytest --strict-markers -m story` and the CI job summary answer the only question that
matters at release time: *which story is this code, and what proves it works?*

---

## Roadmap applied to this release

| Stage | Shipped in v1.0.0 |
|---|---|
| V0 Platform | API, PostgreSQL, Docker, CI |
| V1 Observer | Metrics/logs, deterministic detection, incident creation |
| V2 AI Analyst | Evidence engine, grounded investigation, root-cause hypotheses |
| V3 Controlled Agent | Action planning, policy, human approval, execution, verification |
| V4 Secure Agent | RBAC, threat model, prompt-injection defence, audit chain, supply chain |
| V5 Production Agent | SLOs, observability, CI/CD, AI regression, runbooks, tested rollback |
| V6 Selective Autonomy | **Designed and gated, not enabled** — `AIOPS_AUTONOMY_LEVEL` supports it; the default is `approval_required` |

V6 is deliberately not switched on: the release ships the *mechanism* (risk-tiered
autonomy) with a conservative default, because a system that can rewrite production is
a system that has to earn that right with measured behaviour.
