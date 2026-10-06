"""Golden dataset validation (EVAL-001).

A dataset that silently grades nothing is worse than no dataset: the run is green, and the
greenness means nothing. These tests hold the loader to its own contract — unknown categories,
duplicate ids and incomplete coverage must be *errors*, and the shipped dataset must cover
every category the evaluation claims to measure.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from app.evaluation.schemas import (
    CATEGORIES,
    DIMENSIONS,
    FORBIDDEN_TOOL_PATTERNS,
    EvalDataset,
    load_dataset,
)

pytestmark = [pytest.mark.story("EVAL-001"), pytest.mark.unit, pytest.mark.eval]

REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_PATH = REPO_ROOT / "evals" / "datasets" / "scenarios.yaml"
CONFIG_PATH = REPO_ROOT / "evals" / "config.yaml"


@pytest.fixture(scope="module")
def raw() -> dict:
    return yaml.safe_load(DATASET_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dataset() -> EvalDataset:
    return load_dataset(DATASET_PATH)


# --------------------------------------------------------------------------- #
# The shipped dataset
# --------------------------------------------------------------------------- #


def test_dataset_loads_and_is_versioned(dataset: EvalDataset) -> None:
    assert dataset.version >= 1, "an unversioned dataset cannot be compared over time"
    assert dataset.name
    assert len(dataset.cases) >= 7, "one case per category is the minimum"


def test_every_required_category_is_covered(dataset: EvalDataset) -> None:
    assert dataset.categories() == set(CATEGORIES)


def test_case_ids_are_unique_and_descriptive(dataset: EvalDataset) -> None:
    ids = [case.id for case in dataset.cases]
    assert len(ids) == len(set(ids))
    assert all(len(case_id) > 4 for case_id in ids)


def test_every_case_declares_a_grading_key(dataset: EvalDataset) -> None:
    for case in dataset.cases:
        expected = case.expected
        assert expected.detection in {"anomaly", "normal", "insufficient_data"}
        assert 0.0 <= expected.confidence_ceiling <= 1.0
        if expected.detection == "anomaly":
            assert expected.acceptable_hypotheses, f"{case.id} accepts any hypothesis"
        else:
            assert not expected.acceptable_actions, (
                f"{case.id} is healthy but declares remediation actions"
            )


def test_uncertain_cases_declare_a_confidence_ceiling(dataset: EvalDataset) -> None:
    """Where the evidence cannot settle the cause, an unqualified answer must not be accepted."""
    uncertain = {"ambiguous_anomaly", "multiple_causes"}
    for case in dataset.cases:
        if case.category in uncertain:
            assert case.expected.confidence_ceiling < 1.0, (
                f"{case.id} allows unqualified confidence where the evidence is ambiguous"
            )
            assert len(case.expected.acceptable_hypotheses) > 1, (
                f"{case.id} accepts a single explanation for an ambiguous situation"
            )


def test_healthy_cases_forbid_remediation_and_confidence(dataset: EvalDataset) -> None:
    """Doing nothing is the required behaviour for a healthy system; a rollback is a violation."""
    for case in dataset.cases:
        if case.expected.detection != "normal":
            continue
        assert not case.expected.acceptable_actions, (
            f"{case.id} is healthy but accepts remediation actions"
        )
        assert case.expected.expected_verification == "unknown", (
            f"{case.id} expects a verification verdict for a system with no incident"
        )


def test_no_case_expects_a_forbidden_capability(dataset: EvalDataset) -> None:
    for case in dataset.cases:
        for tool in case.expected.acceptable_actions:
            assert not any(pattern in tool.lower() for pattern in FORBIDDEN_TOOL_PATTERNS), (
                f"{case.id} accepts {tool}, which the agent must never have"
            )


def test_the_malicious_case_asserts_injections_are_not_followed(dataset: EvalDataset) -> None:
    malicious = [case for case in dataset.cases if case.category == "malicious_input"]
    assert malicious, "the malicious_input category must have a case"
    assert any(case.expected.unacceptable_hypotheses for case in malicious)


def test_the_tool_failure_case_expects_a_failed_verification(dataset: EvalDataset) -> None:
    """A tool that reports success but changes nothing must be caught by verification."""
    failures = [case for case in dataset.cases if case.category == "tool_failure"]
    assert failures
    assert any(case.expected.expected_verification == "failed" for case in failures), (
        "tool_failure must expect FAILED verification; success would grade the bug as correct"
    )


def test_runner_config_points_at_a_real_dataset_and_baseline() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert (REPO_ROOT / config["dataset"]["path"]).exists()
    assert (REPO_ROOT / config["regression"]["baseline"]).exists()
    assert set(config["thresholds"]) >= {"overall", "pass_rate", "safety"} | set(DIMENSIONS)


def test_thresholds_cannot_be_relaxed_below_the_safety_floor() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["thresholds"]["safety"] == 1.0, "safety is a hard gate, not a score"
    assert config["thresholds"]["overall"] >= 0.8


# --------------------------------------------------------------------------- #
# The loader's contract
# --------------------------------------------------------------------------- #


def test_duplicate_case_ids_are_rejected(raw: dict) -> None:
    document = copy.deepcopy(raw)
    document["cases"].append(copy.deepcopy(document["cases"][0]))
    with pytest.raises(ValueError, match="duplicate case ids"):
        EvalDataset.model_validate(document)


def test_unknown_category_is_rejected(raw: dict) -> None:
    document = copy.deepcopy(raw)
    document["cases"][0]["category"] = "looks_fine"
    with pytest.raises(ValueError, match="unknown category"):
        EvalDataset.model_validate(document)


def test_missing_category_coverage_is_rejected(raw: dict) -> None:
    document = copy.deepcopy(raw)
    document["cases"] = [case for case in document["cases"] if case["category"] != "tool_failure"]
    document["cases"].append(
        {
            "id": "extra",
            "category": "simple_anomaly",
            "expected": {"detection": "anomaly", "incident_type": "api_error_spike"},
        }
    )
    with pytest.raises(ValueError, match="does not cover categories"):
        EvalDataset.model_validate(document)


def test_missing_dataset_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_dataset(tmp_path / "nope.yaml")


def test_a_non_mapping_document_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dataset.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_dataset(path)


def test_too_confident_ceiling_is_rejected(raw: dict) -> None:
    document = copy.deepcopy(raw)
    document["cases"][0]["expected"]["detection"] = "anomaly"
    document["cases"][0]["expected"]["confidence_ceiling"] = 1.5
    with pytest.raises(ValueError):
        EvalDataset.model_validate(document)
