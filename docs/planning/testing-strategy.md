# Testing Strategy

## Pyramid

```text
                 ┌───────────────┐
                 │      E2E      │  tests/e2e — three MVP scenarios over HTTP
                 └───────┬───────┘
             ┌───────────┴───────────┐
             │      Integration      │  tests/integration — real PostgreSQL, HTTP adapters
             └───────────┬───────────┘
      ┌──────────────────┴──────────────────┐
      │        Security / AI Eval           │  tests/security, evals/
      └──────────────────┬──────────────────┘
      ┌──────────────────┴──────────────────┐
      │             Unit tests              │  tests/unit — pure, deterministic
      └─────────────────────────────────────┘
```

## Layers

| Layer | Path | Runs against | Marker |
|---|---|---|---|
| Unit | `tests/unit/**` | nothing external | `unit` |
| Integration | `tests/integration/**` | real PostgreSQL (pgserver), HTTP via `respx` | `integration` |
| E2E | `tests/e2e/**` | full ASGI app + real DB | `e2e` |
| Security | `tests/security/**` | RBAC, tool abuse, prompt injection, secrets | `security` |
| AI evaluation | `evals/**` | golden dataset + graders | `eval` |

## What each layer must cover

**Unit** — state transitions, policy decisions, risk classification, anomaly maths,
verification outcomes, schema validation, idempotency keys, sanitisers.

**Integration** — PostgreSQL persistence across a restart, migrations applied twice,
GitHub/Jira/Slack/Payments adapters against mocked transports (timeouts, retries,
rate limits, error mapping), metrics/logs window semantics, tracing propagation.

**E2E** — the complete lifecycle for each MVP scenario, including the approval gate and
the failure path that must escalate rather than claim success.

**Security** — role × endpoint × tool authorization matrix, approval-tampering rejection,
prompt-injection corpus through every evidence source, secret-exposure scan of logs and
API responses.

**AI evaluation** — see `evals/README.md`. Safety violations are a hard fail, not a score.

## Local commands

```bash
make test-unit          # fast loop
make test-integration   # boots a local PostgreSQL via pgserver
make test-security
make test-e2e
make eval               # AI evaluation harness
make ci                 # everything CI runs
```

## Conventions

* Every test declares the story it proves: `pytestmark = pytest.mark.story("OPS-030")`.
* Integration tests that need PostgreSQL use the `postgres_url` fixture; if no server is
  available they **skip with a reason** rather than pretending to pass.
* No test may call the public internet. Adapters are exercised through `respx`.
* Flaky tests are defects: they are fixed or deleted, never re-run.
