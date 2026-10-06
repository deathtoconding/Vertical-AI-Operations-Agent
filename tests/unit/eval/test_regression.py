"""The regression gate (EVAL-003).

The gate exists to answer one question: did the agent get *worse*? Its failure modes are the
dangerous part — a gate that trips on improvements gets disabled, and a gate that ignores a
single newly failing case is worse than none. The tests below are written as attacks on those
failure modes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evaluation.schemas import CaseResult, EvalReport, Grade
from evals.regression import (
    DEFAULT_MAX_DROP,
    baseline_document,
    compare_with_baseline,
    load_baseline,
)

pytestmark = [pytest.mark.story("EVAL-003"), pytest.mark.unit, pytest.mark.eval]

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINE_PATH = REPO_ROOT / "evals" / "baselines" / "baseline.json"

DIMENSIONS = (
    "detection",
    "evidence",
    "diagnosis",
    "grounding",
    "action_selection",
    "safety",
    "verification",
)


def report(
    metrics: dict[str, float],
    *,
    cases: list[CaseResult] | None = None,
    hard_failures: list[str] | None = None,
    dataset_version: int = 3,
) -> EvalReport:
    return EvalReport(
        dataset="aiops-mvp-golden",
        dataset_version=dataset_version,
        reasoner="deterministic",
        case_count=len(cases or []),
        passed=not hard_failures,
        hard_failures=hard_failures or [],
        metrics=metrics,
        cases=cases or [],
    )


def case_result(case_id: str, *, passed: bool = True, score: float = 1.0) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        category="simple_anomaly",
        passed=passed,
        grades=[
            Grade(dimension=dimension, score=score, passed=score >= 0.75)
            for dimension in DIMENSIONS
        ],
    )


def clean_metrics(**overrides: float) -> dict[str, float]:
    values = {
        **dict.fromkeys(DIMENSIONS, 1.0),
        "overall": 1.0,
        "pass_rate": 1.0,
    }
    values.update(overrides)
    return values


def baseline(**overrides: float) -> dict:
    return {
        "dataset": "aiops-mvp-golden",
        "dataset_version": 3,
        "reasoner": "deterministic",
        "case_count": 1,
        "metrics": clean_metrics(**overrides),
        "case_results": {"case-a": {"passed": True, "hard_failed": False, "score": 1.0}},
    }


# --------------------------------------------------------------------------- #
# No change, improvement, regression
# --------------------------------------------------------------------------- #


def test_an_identical_run_is_not_a_regression() -> None:
    assert compare_with_baseline(report(clean_metrics()), baseline()) == []


def test_an_improvement_never_blocks() -> None:
    improved = report(clean_metrics(overall=1.0), cases=[case_result("case-a")])
    weaker_baseline = baseline()
    weaker_baseline["metrics"]["overall"] = 0.70
    assert compare_with_baseline(improved, weaker_baseline) == []


def test_a_drop_beyond_tolerance_is_reported() -> None:
    regressed = report(clean_metrics(detection=0.80))
    failures = compare_with_baseline(regressed, baseline())
    assert any("detection regressed" in failure for failure in failures)
    assert DEFAULT_MAX_DROP["detection"] == 0.05


def test_a_drop_inside_tolerance_is_tolerated() -> None:
    # Averages move for benign reasons (one harder case); the gate is not a flake detector.
    slightly_lower = report(clean_metrics(overall=0.98))
    assert compare_with_baseline(slightly_lower, baseline()) == []


def test_a_dimension_missing_from_the_new_report_is_a_regression() -> None:
    metrics = clean_metrics()
    del metrics["grounding"]
    failures = compare_with_baseline(report(metrics), baseline())
    assert any("grounding" in failure and "missing" in failure for failure in failures)


# --------------------------------------------------------------------------- #
# Safety: zero tolerance
# --------------------------------------------------------------------------- #


def test_any_drop_in_safety_fails_the_gate() -> None:
    failures = compare_with_baseline(report(clean_metrics(safety=0.99)), baseline())
    assert any("safety dropped" in failure for failure in failures)


def test_safety_below_a_clean_one_fails_even_without_a_baseline_drop() -> None:
    """A 0.98 safety score means something unsafe happened; that is not an average."""
    strict = baseline(safety=1.0)
    failures = compare_with_baseline(report(clean_metrics(safety=0.999)), strict)
    assert any("not a clean 1.0" in failure for failure in failures)


def test_a_hard_failure_fails_the_gate_regardless_of_scores() -> None:
    failing = report(
        clean_metrics(),
        hard_failures=["case-x: deployment.rollback_simulation allowed without approval"],
    )
    failures = compare_with_baseline(failing, baseline())
    assert any(failure.startswith("SAFETY:") for failure in failures)


# --------------------------------------------------------------------------- #
# Newly failing cases
# --------------------------------------------------------------------------- #


def test_a_case_that_passed_before_and_fails_now_is_a_regression() -> None:
    newly_failing = report(clean_metrics(), cases=[case_result("case-a", passed=False, score=0.4)])
    failures = compare_with_baseline(newly_failing, baseline())
    assert any("case-a passed before and fails now" in failure for failure in failures)


def test_a_new_case_that_was_not_in_the_baseline_cannot_be_compared() -> None:
    # It is not reported as a regression: nothing says it used to pass.
    unknown = report(clean_metrics(), cases=[case_result("brand-new", passed=False, score=0.2)])
    assert compare_with_baseline(unknown, baseline()) == []


def test_a_case_that_was_already_failing_stays_a_known_problem() -> None:
    already_broken = baseline()
    already_broken["case_results"]["case-a"]["passed"] = False
    still_failing = report(clean_metrics(), cases=[case_result("case-a", passed=False, score=0.4)])
    assert compare_with_baseline(still_failing, already_broken) == []


# --------------------------------------------------------------------------- #
# Baseline documents
# --------------------------------------------------------------------------- #


def test_baseline_document_records_scores_recomputed_from_grades() -> None:
    """The serialised ``CaseResult`` has no ``score`` field; the baseline must still store one."""
    document = baseline_document(report(clean_metrics(), cases=[case_result("case-a", score=0.5)]))
    stored = document["case_results"]["case-a"]
    assert stored["score"] == pytest.approx(0.5)
    assert stored["passed"] is True
    assert set(document) >= {"dataset", "dataset_version", "reasoner", "metrics", "case_results"}


def test_a_case_with_no_grades_scores_zero_rather_than_crashing() -> None:
    document = baseline_document(
        report(
            clean_metrics(), cases=[CaseResult(case_id="empty", category="normal", passed=False)]
        )
    )
    assert document["case_results"]["empty"]["score"] == 0.0


def test_the_shipped_baseline_is_a_valid_baseline_document() -> None:
    shipped = load_baseline(BASELINE_PATH)
    assert shipped["dataset"] == "aiops-mvp-golden"
    assert shipped["case_count"] == len(shipped["case_results"]) >= 7
    assert shipped["metrics"]["safety"] == 1.0, "the committed baseline must be clean on safety"


def test_the_shipped_baseline_does_not_contain_a_regression_trap() -> None:
    """Re-running the same scores against the stored baseline must not trip the gate."""
    shipped = load_baseline(BASELINE_PATH)
    cases = [
        case_result(case_id, passed=bool(item["passed"]), score=float(item["score"]))
        for case_id, item in shipped["case_results"].items()
    ]
    assert compare_with_baseline(report(shipped["metrics"], cases=cases), shipped) == []


def test_a_corrupt_baseline_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    corrupt = tmp_path / "baseline.json"
    corrupt.write_text(json.dumps({"nope": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a valid evaluation baseline"):
        load_baseline(corrupt)


def test_a_missing_baseline_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_baseline(tmp_path / "absent.json")
