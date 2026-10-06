# Backlog

| File | Purpose |
|---|---|
| `backlog.yaml` | **Source of truth.** Validated by `tests/unit/planning/test_backlog.py` |
| `jira-import.csv` | Generated: one row per Story/Subtask, Jira-import ready |
| `SPRINT_BOARD.md` | Generated: capacity view, board, epic rollup, critical path |

## Regenerate

```bash
python scripts/export_backlog.py --csv docs/backlog/jira-import.csv \
                                --markdown docs/backlog/SPRINT_BOARD.md
python scripts/export_backlog.py --check     # validate only (CI)
```

## Contract enforced by the validator

* unique epic and story ids; every story references a real epic
* Fibonacci points only, and any story ≥ 13 points is rejected (must be split)
* every story has acceptance criteria, security, observability, test approach and artefacts
* dependencies exist, are acyclic, and are never scheduled later than their dependent
* every story is scheduled in exactly one sprint; sprint load ≤ 26 points
* the section-18 critical path is present and remains a connected chain
* the Jira export is generated from the validated model, never hand-edited

## Adding work

1. add the story to `backlog.yaml` (`implemented_by` + `test_approach` are mandatory)
2. schedule it in a sprint, respecting capacity and dependency order
3. regenerate the export, commit both files
4. run `make test-plan`
