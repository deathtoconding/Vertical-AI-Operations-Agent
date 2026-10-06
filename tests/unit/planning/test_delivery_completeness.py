"""Delivery completeness — every story must have produced its artefacts.

Story: OPS-070 (release validation). This is a *release gate* test: it fails while the
backlog still describes work that has not been done, which is exactly the point. It is the
mechanical part of the Definition of Done ("implementation complete, merged to main").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.planning.backlog import Story, load_backlog

pytestmark = pytest.mark.story("OPS-070")

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def backlog():
    return load_backlog()


def test_every_story_artefact_exists(backlog) -> None:
    missing: list[str] = []
    for story in backlog.stories:
        for artefact in story.implemented_by:
            if not (REPO_ROOT / artefact).exists():
                missing.append(f"{story.id} -> {artefact}")
    assert not missing, "stories reference artefacts that do not exist:\n" + "\n".join(missing)


def test_every_story_test_approach_path_exists(backlog) -> None:
    """The declared test approach must point at a real test file or directory."""
    missing: list[str] = []
    for story in backlog.stories:
        for token in story.test_approach.split():
            token = token.strip("(),;`")
            if token.startswith("tests/") or token.startswith("evals/"):
                target = token.split("::")[0]
                if not (REPO_ROOT / target).exists():
                    missing.append(f"{story.id} -> {story.test_approach}")
                    break
    assert not missing, "test approaches reference missing files:\n" + "\n".join(missing)


def test_every_story_is_marked_by_at_least_one_test(backlog) -> None:
    """Traceability: each story id must appear as a @pytest.mark.story marker."""
    corpus = "\n".join(
        path.read_text(encoding="utf-8")
        for path in list((REPO_ROOT / "tests").rglob("*.py")) + list((REPO_ROOT / "evals").rglob("*.py"))
    )
    unmarked = [story.id for story in backlog.stories if f'"{story.id}"' not in corpus]
    assert not unmarked, f"stories with no test marker: {', '.join(unmarked)}"


def test_p0_stories_are_all_implemented(backlog) -> None:
    """No P0 story may remain unimplemented at release."""
    p0: list[Story] = [story for story in backlog.stories if story.priority == "P0"]
    assert len(p0) >= 35
    for story in p0:
        for artefact in story.implemented_by:
            assert (REPO_ROOT / artefact).exists(), f"P0 {story.id} missing {artefact}"
