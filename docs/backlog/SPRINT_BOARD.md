<!-- GENERATED FILE — do not edit by hand. -->
<!-- Regenerate with: python scripts/export_backlog.py --markdown docs/backlog/SPRINT_BOARD.md -->

# Sprint Board — Vertical AI Operations Agent

**40 stories · 12 epics · 9 sprints · 206 points**

## Capacity view

```text
SPRINT 1 Foundation                   ██████████████████████ 24
SPRINT 2 Integrations                 ███████████████████ 21
SPRINT 3 Detection + SRE              ████████████████████████ 26
SPRINT 4 AI Investigation             ██████████████████████ 24
SPRINT 5 Controlled Actions           █████████████████████ 23
SPRINT 6 Verification + Reliability   ███████████████████ 21
SPRINT 7 Security                     ████████████████████████ 26
SPRINT 8 Evaluation + CD              █████████████████████ 23
SPRINT 9 Operations UI + Release      █████████████████ 18
```

## Board

### Sprint 1 — Foundation (24 points)

*Outcome:* A deployable backend with database and CI foundation.

| Story | Summary | Priority | Points | Depends on |
|---|---|---|---:|---|
| OPS-001 | Define SaaS operational domain | P0 | 3 | — |
| OPS-002 | Define agent lifecycle | P0 | 3 | OPS-001 |
| OPS-010 | Create FastAPI service | P0 | 5 | OPS-001 |
| OPS-011 | Implement PostgreSQL persistence | P0 | 5 | OPS-010 |
| DEV-001 | Dockerize application | P0 | 3 | OPS-010 |
| DEV-002 | Build CI pipeline | P0 | 5 | OPS-010 |

### Sprint 2 — Integrations (21 points)

*Outcome:* System can collect operational evidence.

| Story | Summary | Priority | Points | Depends on |
|---|---|---|---:|---|
| OPS-020 | GitHub read integration | P0 | 5 | OPS-011 |
| OPS-021 | Jira integration | P0 | 5 | OPS-011 |
| OPS-022 | Slack integration | P0 | 3 | OPS-011 |
| OPS-023 | Metrics and log integration | P0 | 5 | OPS-011 |
| SRE-002 | Structured logging | P0 | 3 | OPS-010 |

### Sprint 3 — Detection + SRE (26 points)

*Outcome:* Operational anomalies become incidents, with metrics, SLOs and API hardening.

| Story | Summary | Priority | Points | Depends on |
|---|---|---|---:|---|
| OPS-030 | Implement anomaly detector | P0 | 5 | OPS-023 |
| OPS-031 | Create incidents from anomalies | P0 | 3 | OPS-030 |
| SRE-001 | Operational metrics | P0 | 5 | OPS-010 |
| SRE-004 | SLO dashboard | P0 | 5 | SRE-001 |
| SEC-001 | Threat model | P0 | 5 | OPS-010 |
| SEC-006 | API rate limiting and input validation | P0 | 3 | OPS-010 |

### Sprint 4 — AI Investigation (24 points)

*Outcome:* Agent can investigate incidents.

| Story | Summary | Priority | Points | Depends on |
|---|---|---|---:|---|
| OPS-040 | Build evidence retrieval | P0 | 8 | OPS-020, OPS-021, OPS-022, OPS-023 |
| OPS-041 | Implement AI investigation | P0 | 8 | OPS-020, OPS-023, OPS-030, OPS-040 |
| OPS-050 | Implement agent state machine | P0 | 8 | OPS-002, OPS-041 |

### Sprint 5 — Controlled Actions (23 points)

*Outcome:* Agent can propose and execute approved actions.

| Story | Summary | Priority | Points | Depends on |
|---|---|---|---:|---|
| OPS-051 | Implement tool registry | P0 | 5 | OPS-050 |
| OPS-060 | Build structured action planner | P0 | 5 | OPS-051 |
| OPS-061 | Implement approval gateway | P0 | 5 | OPS-060 |
| OPS-062 | Implement action executor | P0 | 8 | OPS-061 |

### Sprint 6 — Verification + Reliability (21 points)

*Outcome:* Agent can verify its actions and operate reliably.

| Story | Summary | Priority | Points | Depends on |
|---|---|---|---:|---|
| OPS-063 | Implement verification engine | P0 | 8 | OPS-062 |
| SRE-003 | Distributed tracing | P0 | 5 | SRE-001 |
| SRE-005 | Runbooks | P0 | 3 | SRE-001 |
| DEV-004 | Implement and test rollback | P0 | 5 | DEV-001, OPS-063 |

### Sprint 7 — Security (26 points)

*Outcome:* Security controls become enforceable rather than aspirational.

| Story | Summary | Priority | Points | Depends on |
|---|---|---|---:|---|
| SEC-002 | RBAC and least privilege | P0 | 8 | OPS-061 |
| SEC-003 | Prompt-injection defenses | P0 | 8 | SEC-002, OPS-041 |
| SEC-004 | Audit trail | P0 | 5 | SEC-002, OPS-062 |
| SEC-005 | Supply-chain controls | P0 | 5 | OPS-010 |

### Sprint 8 — Evaluation + CD (23 points)

*Outcome:* AI regressions are detected automatically and releases are gated.

| Story | Summary | Priority | Points | Depends on |
|---|---|---|---:|---|
| EVAL-001 | Build evaluation dataset | P0 | 5 | OPS-041 |
| EVAL-002 | Build evaluation harness | P0 | 8 | EVAL-001 |
| EVAL-003 | AI regression testing | P0 | 5 | EVAL-002 |
| DEV-003 | Build CD pipeline | P0 | 5 | DEV-002 |

### Sprint 9 — Operations UI + Release (18 points)

*Outcome:* Operators can run the system and the release is validated.

| Story | Summary | Priority | Points | Depends on |
|---|---|---|---:|---|
| UI-001 | Incident dashboard | P1 | 5 | OPS-063, SEC-004 |
| UI-002 | Approval interface | P1 | 5 | UI-001, SEC-002 |
| UI-003 | Agent activity view | P1 | 3 | UI-001, SRE-003 |
| OPS-070 | Final hardening and release validation | P0 | 5 | EVAL-003, DEV-003, UI-002, UI-003 |

## Epic rollup

| Epic | Summary | Priority | Stories | Points |
|---|---|---|---:|---:|
| EPIC-01 | Product & Domain Foundation | P0 | 2 | 6 |
| EPIC-02 | Core Platform | P0 | 2 | 10 |
| EPIC-03 | Integrations | P0 | 4 | 18 |
| EPIC-04 | Detection | P0 | 2 | 8 |
| EPIC-05 | Investigation | P0 | 2 | 16 |
| EPIC-06 | Agent Orchestration | P0 | 2 | 13 |
| EPIC-07 | Actions & Verification | P0 | 4 | 26 |
| EPIC-08 | Security / SecDevOps | P0 | 6 | 34 |
| EPIC-09 | SRE / Observability | P0 | 5 | 21 |
| EPIC-10 | DevOps / CI/CD | P0 | 4 | 18 |
| EPIC-11 | AI Evaluation | P0 | 3 | 18 |
| EPIC-12 | Operations UI | P1 | 4 | 18 |

## Critical path

```text
OPS-001 → OPS-010 → OPS-011 → OPS-023 → OPS-030 → OPS-041 → OPS-050 → OPS-051 → OPS-060 → OPS-061 → OPS-062 → OPS-063 → UI-001 → UI-002 → OPS-070
```
