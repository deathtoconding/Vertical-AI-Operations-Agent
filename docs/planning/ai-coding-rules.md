# AI Coding Rules (project-level instructions)

These rules are binding for any AI or human contributor. They are enforced by CI where
mechanically checkable, and by review where they are not.

## Rule 1 — Never silently change architecture
No new framework, database, service or dependency without explicit justification.
Justifications live in `docs/adr/`. `tests/unit/planning/test_architecture_rules.py`
asserts the enforced subset (no second web framework, no second DB driver, no shell/SQL
tool, no secrets in source).

## Rule 2 — Inspect before modifying
Understand the code, identify its dependents, plan the change, then implement. The
dependency direction in `docs/architecture/overview.md` is the map; `app/domain/` must
never import FastAPI, SQLAlchemy, or an LLM SDK.

## Rule 3 — Small changes
One story → one coherent implementation → tests → verification. A pull request that
cannot be described by a single story key is too large.

## Rule 4 — Tests accompany code
Every behavioural change ships tests in the same commit. `make ci` fails otherwise.

## Rule 5 — No fake implementations
No `return {"status": "success"}` stubs presented as functionality. Mocks and fixtures
live under `tests/fixtures/` and are labelled as such. The sandbox SaaS simulator is a
*test double with a documented contract*, not a lie: it is isolated in `app/sandbox/`,
selected only via `AIOPS_INTEGRATIONS_MODE=sandbox`, and its data is explicitly marked
`simulated: true`.

## Rule 6 — No secrets
Never commit API keys, passwords or tokens. `.env.example` documents required values with
placeholders; CI runs gitleaks; `app/core/logging.py` redacts secrets from logs.

## Rule 7 — No unrestricted agent tools
No shell, arbitrary SQL, arbitrary HTTP or filesystem tool is ever exposed to the LLM.
The tool registry is the only door, and it is deny-by-default.

## Rule 8 — Verify claims
Every statement about status must be one of **implemented**, **tested**, **assumed** or
**not verified**. This repository's `docs/planning/verification-report.md` uses those
exact words, and CI evidence is quoted rather than paraphrased.

## Rule 9 — The LLM is not an authority
The model reasons, plans and interprets. It never authorises. Policy, approval,
execution and verification are deterministic and testable (ADR-0002).
