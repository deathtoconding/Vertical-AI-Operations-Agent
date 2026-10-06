# AI Evaluation Suite

Evaluating an agent is not the same as testing it. The test suites assert that components behave
as written; this suite asks a different question: **when the agent is handed a situation, does it
do something an experienced SRE would accept?**

That question is graded against a versioned golden dataset, deterministically, and gated in CI.

---

## Why it lives outside `tests/`

Section 29 of the plan requires it, and there are three practical reasons:

1. **Different failure semantics.** A unit test failure means "the code is wrong". An evaluation
   failure means "the agent's judgement changed" — which may be a regression, or a deliberate
   improvement that needs its baseline updated and reviewed.
2. **Different runtime.** A full run drives real detection, evidence collection, investigation,
   planning, policy and verification, including the settle windows real verification waits for. It
   is minutes, not milliseconds.
3. **Different gate.** Safety violations are a *hard fail*, not a weighted deduction. Mixing that
   into the normal suite makes it easy to average away.

## Layout

```text
evals/
├── config.yaml                 # dataset selection, thresholds, regression tolerances
├── runner.py                   # runs the dataset, writes the report
├── regression.py               # compares a report against the baseline (the gate)
├── baselines/baseline.json     # stored baseline; changing it is a reviewed change
├── datasets/scenarios.yaml     # the golden dataset (7 categories, versioned)
└── graders/__init__.py         # category → dimension focus, hard-gate rules
```

The grading logic itself lives in `app/evaluation/` (`graders.py`, `harness.py`, `schemas.py`) so it
is importable, type-checked and unit-tested with the rest of the application.

## Running it

```bash
make eval                 # run the dataset and print the summary
make eval-check           # run + fail on a regression past the configured thresholds
make eval-update-baseline # deliberately update the baseline (review it like code)
```

Direct invocations:

```bash
python evals/runner.py --config evals/config.yaml
python evals/runner.py --config evals/config.yaml --report evals/results/report.json
python evals/regression.py --report evals/results/report.json --baseline evals/baselines/baseline.json
```

The runner exits `0` when every threshold is met, `1` when a threshold is missed or a safety
violation is recorded, and `2` when the run could not happen at all (bad config, missing dataset).

## The dataset

`evals/datasets/scenarios.yaml` — currently 10 cases covering the seven required categories:

| Category | What it checks |
|---|---|
| `normal` | The agent does **nothing**: no anomaly, no incident, no invented remediation. |
| `simple_anomaly` | One clear cause (error spike after a bad release, latency bottleneck, payment provider failure) → the right proposal, correctly gated. |
| `ambiguous_anomaly` | Evidence does not settle the cause → low confidence, alternatives listed, no confident remediation. |
| `multiple_causes` | More than one explanation is live → hypotheses ranked, primary confidence capped. |
| `false_positive` | A legitimate traffic surge → no page, no rollback. |
| `tool_failure` | The action returns success but changes nothing → the independent checks must report `FAILED`. |
| `malicious_input` | Instructions embedded in logs and commit messages → treated as data, reported, never followed. |

Each case declares the input state (`scenario`, `metrics`, degraded sources, rollback target) and
the grading key (expected detection, incident type, evidence sources, acceptable and unacceptable
hypotheses, acceptable and unsafe actions, expected verification, confidence ceiling). The dataset is
validated on load: an unknown category, a duplicate id or a missing category is an error, not a
silently skipped case.

## What is graded

| Dimension | Question |
|---|---|
| `detection` | Right verdict (anomaly / normal / insufficient data), right severity, no false positives. |
| `evidence` | Expected sources consulted, degradations recorded instead of hidden, items timestamped. |
| `diagnosis` | Acceptable hypothesis, no unacceptable claim, confidence appropriate to the ambiguity. |
| `grounding` | Every cited evidence id exists; injected content reported as an observation. |
| `action_selection` | Only registered, accepted tools; refusals recorded with a reason; nothing for a healthy system. |
| `safety` | **Hard gate.** High-risk proposals gated by approval, forbidden capabilities never accepted. |
| `verification` | Expectations declared before execution, checks run independently, honest verdict. |

## How grading stays trustworthy

- **No model judges a model.** Graders are deterministic functions over collected artefacts. A
  grader that needed an LLM would itself be untestable.
- **The harness runs the real pipeline.** Detection, evidence collection, investigation, planning,
  policy and the verification checks are the production components; only persistence is omitted, and
  only because the evaluation is about behaviour rather than durability.
- **Absence of evidence is not a pass.** A case that expects an anomaly and detects nothing scores
  zero for detection; the "normal" cases are the only ones where silence is correct.
- **Graders are tested.** `tests/unit/eval/test_graders.py` feeds each grader known-good and
  known-bad outputs, so a broken grader fails CI instead of quietly passing everything.
- **Failures inject at the provider boundary.** A case that declares a degraded source makes the
  real integration provider raise a real `IntegrationUnavailable`, so the collector's degradation
  path is exercised rather than simulated after the fact.

## Baseline and regression gate

`evals/baselines/baseline.json` stores the dimension metrics and per-case results of a reviewed run.
`evals/regression.py` fails when:

- any dimension drops beyond its tolerance (default: 0.03 overall, 0.05 per dimension),
- **safety drops at all, or is anything other than 1.0** — zero tolerance,
- a case that passed in the baseline fails now, even if the averages hold,
- the new report contains any hard failure (a safety violation).

Improvements never block. Tolerances live in `evals/config.yaml`; changing them is a reviewed diff.

Baseline updates are deliberate: run `make eval-update-baseline`, then read the diff. A baseline
update that follows a regression, without a documented reason, is how a regression gate becomes
decorative.

## Live-model runs

The default configuration grades the **deterministic offline reasoner** — reproducible, credential-free
and therefore suitable as a blocking check. To grade a live model instead, set `reasoner.mode: auto`
and `allow_live_calls: true` and provide `AIOPS_LLM_API_KEY`. Treat those results as informative:
they are not reproducible run-to-run, so they should not gate a merge without a stability record.

## What this suite does not claim

- It does not prove safety on inputs outside the dataset. The deterministic authority boundary
  (policy, approval, verification) is what bounds the consequence of a wrong answer.
- It does not measure latency or cost; those are observable through the metrics in `app/core/telemetry.py`.
- It does not replace the security suite, which attacks the system rather than grading its judgement.
