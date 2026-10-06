"""Planning tests — the plan is a deliverable, so it is tested like one.

Story: OPS-001, OPS-002 (EPIC-01), DEV-002 (plan contract enforced in CI)
"""

from __future__ import annotations

import csv
import io
from itertools import pairwise
from pathlib import Path

import pytest
import yaml

from app.planning.backlog import (
    CRITICAL_PATH,
    FIBONACCI_POINTS,
    MAX_STORY_POINTS,
    MVP_SCENARIOS,
    SPRINT_CAPACITY,
    BacklogError,
    Story,
    cached_backlog,
    jira_rows,
    load_backlog,
    sprint_points,
)

pytestmark = pytest.mark.story("OPS-001")

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def backlog():
    return cached_backlog()


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #


def test_backlog_loads_and_covers_all_epics(backlog) -> None:
    assert backlog.version >= 2
    assert len(backlog.epics) == 12, "the master plan defines EPIC-01..EPIC-12"
    assert len(backlog.stories) == 40
    assert len(backlog.sprints) == 9, "the corrected plan is 9 sprints, not 8"


def test_every_story_has_a_real_acceptance_criteria_set(backlog) -> None:
    for story in backlog.stories:
        assert len(story.acceptance_criteria) >= 2, f"{story.id} needs real AC"


def test_definition_of_ready_fields_are_present(backlog) -> None:
    """Section 23 — security, observability and test approach are part of readiness."""
    for story in backlog.stories:
        assert story.security.strip(), f"{story.id} has no security implication recorded"
        assert story.observability.strip(), f"{story.id} has no observability requirement"
        assert story.test_approach.strip(), f"{story.id} has no test approach"
        assert story.implemented_by, f"{story.id} does not name its artefacts"
        for artefact in story.implemented_by:
            assert not artefact.startswith("/"), f"{story.id}: {artefact} must be repo-relative"


def test_point_model_is_fibonacci_and_13_is_split(backlog) -> None:
    for story in backlog.stories:
        assert story.points in FIBONACCI_POINTS, f"{story.id} uses {story.points} points"
        assert story.points <= MAX_STORY_POINTS


def test_priorities_are_valid(backlog) -> None:
    for story in backlog.stories:
        assert story.priority in {"P0", "P1", "P2"}
    p0 = [s for s in backlog.stories if s.priority == "P0"]
    assert len(p0) >= 35, "the first release should be mostly P0 (section 20)"


# --------------------------------------------------------------------------- #
# Dependency graph
# --------------------------------------------------------------------------- #


def test_dependency_graph_is_acyclic_and_ordered(backlog) -> None:
    order = backlog.topological_order()
    assert len(order) == len(backlog.stories)
    positions = {story_id: index for index, story_id in enumerate(order)}
    for story in backlog.stories:
        for dependency in story.dependencies:
            assert positions[dependency] < positions[story.id]


def test_dependencies_are_never_scheduled_later(backlog) -> None:
    for story in backlog.stories:
        for dependency in story.dependencies:
            assert backlog.story(dependency).sprint <= story.sprint


def test_critical_path_is_preserved(backlog) -> None:
    """Section 18 — the documented spine must still be a connected dependency chain."""
    assert backlog.spine_is_intact(), "the critical-path spine is broken"
    for earlier, later in pairwise(CRITICAL_PATH):
        assert backlog.reaches(later, earlier), f"{later} no longer depends on {earlier}"
    realised = backlog.critical_path()
    assert len(realised) >= len(CRITICAL_PATH)
    assert realised[0] in backlog.topological_order()


# --------------------------------------------------------------------------- #
# Sprint board
# --------------------------------------------------------------------------- #


def test_sprint_loads_match_the_plan(backlog) -> None:
    totals = sprint_points(backlog)
    assert totals == {1: 24, 2: 21, 3: 26, 4: 24, 5: 23, 6: 21, 7: 26, 8: 23, 9: 18}


def test_no_sprint_exceeds_capacity(backlog) -> None:
    for sprint, points in sprint_points(backlog).items():
        assert points <= SPRINT_CAPACITY, f"sprint {sprint} carries {points} points"


def test_every_story_is_scheduled_exactly_once(backlog) -> None:
    scheduled = [sid for sprint in backlog.sprints for sid in sprint.stories]
    assert len(scheduled) == len(set(scheduled)) == len(backlog.stories)


def test_sprint_8_was_corrected(backlog) -> None:
    """The master plan's original Sprint 8 was 36 points and had to be split."""
    sprint_eight = next(s for s in backlog.sprints if s.number == 8)
    assert "UI-001" not in sprint_eight.stories
    assert "UI-001" in next(s for s in backlog.sprints if s.number == 9).stories


# --------------------------------------------------------------------------- #
# Traceability and export
# --------------------------------------------------------------------------- #


def test_traceability_matrix_stories_exist(backlog) -> None:
    for story_id in CRITICAL_PATH:
        story: Story = backlog.story(story_id)
        assert story.test_approach
        assert story.implemented_by


def test_jira_export_has_one_row_per_issue(backlog) -> None:
    rows = jira_rows(backlog)
    expected = sum(1 + len(s.subtasks) for s in backlog.stories)
    assert len(rows) == expected
    types = {row["Issue Type"] for row in rows}
    assert types == {"Story", "Subtask"}
    for row in rows:
        assert row["Summary"], "Jira will reject an empty summary"
        assert row["Priority"] in {"P0", "P1", "P2"}


def test_jira_export_round_trips_through_csv(backlog) -> None:
    rows = jira_rows(backlog)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    reader = csv.DictReader(io.StringIO(buffer.getvalue()))
    reloaded = list(reader)
    assert len(reloaded) == len(rows)
    assert reloaded[0]["Issue Key"] == rows[0]["Issue Key"]


def test_export_script_main_runs(tmp_path: Path) -> None:
    """The documented export path actually works end to end."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "export_backlog", REPO_ROOT / "scripts" / "export_backlog.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    csv_path = tmp_path / "jira.csv"
    md_path = tmp_path / "board.md"
    exit_code = module.main(["--csv", str(csv_path), "--markdown", str(md_path), "--check"])
    assert exit_code == 0
    assert csv_path.read_text(encoding="utf-8").startswith("Issue Type,")
    board = md_path.read_text(encoding="utf-8")
    assert "SPRINT 1" in board and "Critical path" in board


# --------------------------------------------------------------------------- #
# Negative tests — the validator must actually reject bad plans
# --------------------------------------------------------------------------- #


def _minimal_document() -> dict:
    return {
        "version": 2,
        "project": {"key": "AIOPS"},
        "epics": [
            {"id": "EPIC-01", "summary": "E", "priority": "P0", "outcome": "o"},
        ],
        "stories": [
            {
                "id": "OPS-001",
                "epic": "EPIC-01",
                "summary": "s",
                "description": "d",
                "acceptance_criteria": ["a", "b"],
                "priority": "P0",
                "points": 3,
                "sprint": 1,
                "security": "none",
                "observability": "counter",
                "test_approach": "unit",
                "implemented_by": ["app/x.py"],
            }
        ],
        "sprints": [{"number": 1, "name": "S1", "outcome": "o", "stories": ["OPS-001"]}],
    }


def _write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "backlog.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["stories"][0].update(points=13), "must be split"),
        (lambda d: d["stories"][0].update(points=4), "not a Fibonacci"),
        (lambda d: d["stories"][0].update(dependencies=["OPS-999"]), "unknown dependency"),
        (lambda d: d["stories"][0].update(dependencies=["OPS-001"]), "depends on itself"),
        (lambda d: d["stories"][0].pop("security"), "security"),
        (lambda d: d["stories"][0].update(priority="P9"), "invalid priority"),
        (lambda d: d["stories"][0].update(epic="EPIC-99"), "unknown epic"),
        (lambda d: d["sprints"][0].update(stories=[]), "no stories assigned"),
        (
            lambda d: d["sprints"].append({"number": 2, "stories": ["OPS-001"]}),
            "scheduled in two sprints",
        ),
    ],
)
def test_invalid_backlogs_are_rejected(tmp_path: Path, mutate, message: str) -> None:
    document = _minimal_document()
    mutate(document)
    with pytest.raises(BacklogError) as excinfo:
        load_backlog(_write(tmp_path, document), require_spine=False)
    assert message in str(excinfo.value)


def test_missing_backlog_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(BacklogError, match="not found"):
        load_backlog(tmp_path / "nope.yaml")


def test_full_backlog_spine_is_enforced(tmp_path: Path) -> None:
    """A plan missing its delivery spine must be rejected outright."""
    document = _minimal_document()
    with pytest.raises(BacklogError, match="critical-path spine"):
        load_backlog(_write(tmp_path, document))


# --------------------------------------------------------------------------- #
# Scenario coverage — the MVP promises three scenarios
# --------------------------------------------------------------------------- #


def test_all_three_mvp_scenarios_are_reachable(backlog) -> None:
    assert set(MVP_SCENARIOS) == {"A", "B", "C"}
    for story_id in ("OPS-030", "OPS-031", "OPS-041", "OPS-062", "OPS-063"):
        assert backlog.has_story(story_id)
