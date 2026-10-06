"""Planning-surface: the backlog is validated like code, not like prose.

This module turns ``docs/backlog/backlog.yaml`` into typed objects and provides the
checks that keep the plan honest:

* structural validation (epics, stories, sprints, references)
* the Fibonacci point model with the "split any 13" rule (section 19)
* Definition of Ready enforcement (section 23) per story
* dependency-graph validation (existence, acyclicity, sprint ordering)
* realised sprint loads, so a sprint cannot silently grow past its capacity
* the traceability map used by ``@pytest.mark.story`` markers

Keeping it in ``app/`` (rather than a throwaway script) means it is type-checked,
unit-tested and importable from CI exactly like production code.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

# --------------------------------------------------------------------------- #
# Constants derived from the master plan
# --------------------------------------------------------------------------- #

FIBONACCI_POINTS: Final[tuple[int, ...]] = (1, 2, 3, 5, 8, 13)
MAX_STORY_POINTS: Final[int] = 13
"""A story of MAX_STORY_POINTS or more must be split (section 19)."""

SPRINT_CAPACITY: Final[int] = 26
"""Largest sprint in the board (Sprint 7 — Security). A sprint above this is a smell."""

VALID_PRIORITIES: Final[frozenset[str]] = frozenset({"P0", "P1", "P2"})
VALID_TYPES: Final[frozenset[str]] = frozenset({"story", "spike", "bug"})

REQUIRED_PLAN_FIELDS: Final[tuple[str, ...]] = (
    "acceptance_criteria",
    "security",
    "observability",
    "test_approach",
    "implemented_by",
)
"""Definition of Ready (section 23): a story cannot enter a sprint without these."""

CRITICAL_PATH: Final[tuple[str, ...]] = (
    "OPS-001",
    "OPS-010",
    "OPS-011",
    "OPS-040",
    "OPS-041",
    "OPS-050",
    "OPS-051",
    "OPS-060",
    "OPS-061",
    "OPS-062",
    "OPS-063",
)
"""Section 18: the dependency spine every vertical slice depends on."""

MVP_SCENARIOS: Final[dict[str, str]] = {
    "A": "API error spike -> deployment-correlated rollback -> verified recovery",
    "B": "API latency spike -> endpoint/infra bottleneck -> remediation -> verified recovery",
    "C": "Subscription/payment anomaly -> affected customers -> incident + notification + recovery tracking",
}


def default_backlog_path() -> Path:
    """Return the repository-relative backlog path, independent of CWD."""
    return Path(__file__).resolve().parents[2] / "docs" / "backlog" / "backlog.yaml"


# --------------------------------------------------------------------------- #
# Typed model
# --------------------------------------------------------------------------- #


class BacklogError(ValueError):
    """Raised when the backlog violates the planning contract."""


@dataclass(frozen=True)
class Epic:
    id: str
    key: str
    summary: str
    priority: str
    outcome: str


@dataclass(frozen=True)
class Story:
    id: str
    type: str
    epic: str
    summary: str
    description: str
    acceptance_criteria: tuple[str, ...]
    subtasks: tuple[str, ...]
    priority: str
    points: int
    sprint: int
    dependencies: tuple[str, ...]
    labels: tuple[str, ...]
    security: str
    observability: str
    test_approach: str
    implemented_by: tuple[str, ...]
    epic_summary: str = field(default="", compare=False)


@dataclass(frozen=True)
class Sprint:
    number: int
    name: str
    outcome: str
    stories: tuple[str, ...]
    points: int


@dataclass(frozen=True)
class Backlog:
    project: dict[str, Any]
    epics: tuple[Epic, ...]
    stories: tuple[Story, ...]
    sprints: tuple[Sprint, ...]
    version: int

    # -- lookups ----------------------------------------------------------- #
    def story(self, story_id: str) -> Story:
        for item in self.stories:
            if item.id == story_id:
                return item
        raise BacklogError(f"unknown story id: {story_id}")

    def has_story(self, story_id: str) -> bool:
        return any(item.id == story_id for item in self.stories)

    @property
    def total_points(self) -> int:
        return sum(item.points for item in self.stories)

    def by_epic(self) -> dict[str, list[Story]]:
        grouped: dict[str, list[Story]] = defaultdict(list)
        for item in self.stories:
            grouped[item.epic].append(item)
        return dict(grouped)

    # -- graph ------------------------------------------------------------- #
    def dependencies(self, story_id: str) -> tuple[str, ...]:
        return self.story(story_id).dependencies

    def topological_order(self) -> tuple[str, ...]:
        """Return stories in dependency order (stable by id for determinism)."""
        incoming: dict[str, set[str]] = {s.id: set(s.dependencies) for s in self.stories}
        ready = deque(sorted(k for k, v in incoming.items() if not v))
        order: list[str] = []
        while ready:
            node = ready.popleft()
            order.append(node)
            for candidate, deps in incoming.items():
                if node in deps:
                    deps.discard(node)
                    if not deps and candidate not in order and candidate not in ready:
                        ready.append(candidate)
        if len(order) != len(self.stories):
            unresolved = sorted(set(incoming) - set(order))
            raise BacklogError(f"dependency cycle detected among: {', '.join(unresolved)}")
        return tuple(order)

    def reaches(self, source: str, target: str) -> bool:
        """True when ``target`` is reachable from ``source`` through dependency edges."""
        seen: set[str] = set()
        queue = deque([source])
        while queue:
            node = queue.popleft()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            queue.extend(self.story(node).dependencies)
        return False

    def spine_is_intact(self) -> bool:
        """Section 18: every consecutive pair of the documented spine must be connected."""
        return all(
            self.reaches(later, earlier)
            for earlier, later in zip(CRITICAL_PATH, CRITICAL_PATH[1:], strict=False)
        )

    def critical_path(self) -> tuple[str, ...]:
        """Longest dependency chain by story count — the delivery spine."""
        memo: dict[str, tuple[str, ...]] = {}

        def walk(story_id: str) -> tuple[str, ...]:
            if story_id in memo:
                return memo[story_id]
            story = self.story(story_id)
            if not story.dependencies:
                memo[story_id] = (story_id,)
                return memo[story_id]
            best = max((walk(dep) for dep in story.dependencies), key=len, default=())
            memo[story_id] = (*best, story_id)
            return memo[story_id]

        return max((walk(s.id) for s in self.stories), key=len, default=())


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load_backlog(path: Path | str | None = None, *, require_spine: bool = True) -> Backlog:
    """Load and validate the backlog.

    Raises:
        BacklogError: if the document is structurally invalid or violates the
            planning contract (graph, points, Definition of Ready).
    """
    target = Path(path) if path is not None else default_backlog_path()
    if not target.exists():
        raise BacklogError(f"backlog not found: {target}")

    raw: dict[str, Any] = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    version = int(raw.get("version", 1))
    project = dict(raw.get("project") or {})

    epics = tuple(_parse_epic(item) for item in _require_list(raw, "epics"))
    epic_summaries = {epic.id: epic.summary for epic in epics}

    stories = tuple(
        _parse_story(item, epic_summaries) for item in _require_list(raw, "stories")
    )
    sprints = tuple(_parse_sprint(item) for item in _require_list(raw, "sprints"))

    backlog = Backlog(
        project=project,
        epics=epics,
        stories=stories,
        sprints=sprints,
        version=version,
    )
    validate_backlog(backlog)
    if require_spine:
        validate_plan_completeness(backlog)
    return backlog


@lru_cache(maxsize=1)
def cached_backlog() -> Backlog:
    """Process-wide cache; tests that need a fresh read call :func:`load_backlog`."""
    return load_backlog()


def _require_list(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise BacklogError(f"backlog is missing a non-empty '{key}' list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise BacklogError(f"'{key}[{index}]' is not a mapping")
    return list(value)


def _parse_epic(raw: dict[str, Any]) -> Epic:
    for required in ("id", "summary", "priority", "outcome"):
        if not raw.get(required):
            raise BacklogError(f"epic entry is missing '{required}': {raw!r}")
    return Epic(
        id=str(raw["id"]),
        key=str(raw.get("key", "OPS")),
        summary=str(raw["summary"]),
        priority=str(raw["priority"]),
        outcome=str(raw["outcome"]),
    )


def _parse_story(raw: dict[str, Any], epic_summaries: dict[str, str]) -> Story:
    story_id = str(raw.get("id") or "").strip()
    if not story_id:
        raise BacklogError(f"story entry is missing 'id': {raw!r}")

    epic = str(raw.get("epic") or "").strip()
    if not epic:
        raise BacklogError(f"{story_id}: 'epic' is required")
    if epic not in epic_summaries:
        raise BacklogError(f"{story_id}: unknown epic '{epic}'")

    for required in ("summary", "description", "priority", "points", "sprint", *REQUIRED_PLAN_FIELDS):
        if raw.get(required) in (None, "", [], {}):
            raise BacklogError(f"{story_id}: '{required}' is required (Definition of Ready)")

    story_type = str(raw.get("type", "story"))
    if story_type not in VALID_TYPES:
        raise BacklogError(f"{story_id}: invalid type '{story_type}'")

    priority = str(raw["priority"])
    if priority not in VALID_PRIORITIES:
        raise BacklogError(f"{story_id}: invalid priority '{priority}'")

    points = int(raw["points"])
    if points not in FIBONACCI_POINTS:
        raise BacklogError(f"{story_id}: {points} is not a Fibonacci point value")
    if points >= MAX_STORY_POINTS:
        raise BacklogError(f"{story_id}: {points} points must be split (section 19)")

    criteria = tuple(str(item) for item in raw["acceptance_criteria"])
    if len(criteria) < 2:
        raise BacklogError(f"{story_id}: at least two acceptance criteria are required")

    return Story(
        id=story_id,
        type=story_type,
        epic=epic,
        summary=str(raw["summary"]),
        description=" ".join(str(raw["description"]).split()),
        acceptance_criteria=criteria,
        subtasks=tuple(str(item) for item in raw.get("subtasks") or ()),
        priority=priority,
        points=points,
        sprint=int(raw["sprint"]),
        dependencies=tuple(str(item) for item in raw.get("dependencies") or ()),
        labels=tuple(str(item) for item in raw.get("labels") or ()),
        security=str(raw["security"]),
        observability=str(raw["observability"]),
        test_approach=str(raw["test_approach"]),
        implemented_by=tuple(str(item) for item in raw["implemented_by"]),
        epic_summary=epic_summaries.get(epic, ""),
    )


def _parse_sprint(raw: dict[str, Any]) -> Sprint:
    number = int(raw.get("number", 0))
    if number <= 0:
        raise BacklogError("sprint entries require a positive 'number'")
    stories = tuple(str(item) for item in raw.get("stories") or ())
    if not stories:
        raise BacklogError(f"sprint {number}: no stories assigned")
    return Sprint(
        number=number,
        name=str(raw.get("name", f"Sprint {number}")),
        outcome=str(raw.get("outcome", "")),
        stories=stories,
        points=0,
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate_backlog(backlog: Backlog) -> None:
    """Validate the whole planning contract. Raises :class:`BacklogError`."""
    _validate_identity(backlog)
    _validate_points_and_priority(backlog)
    _validate_dependency_graph(backlog)
    _validate_sprints(backlog)
    _validate_traceability(backlog)


def validate_plan_completeness(backlog: Backlog) -> None:
    """Full-project checks that a partial document (used in negative tests) skips.

    The documented critical path (section 18) must be present and must remain a
    connected dependency chain — otherwise the plan no longer describes a
    deliverable order.
    """
    missing = [story_id for story_id in CRITICAL_PATH if not backlog.has_story(story_id)]
    if missing:
        raise BacklogError(f"critical-path spine incomplete: missing {', '.join(missing)}")
    if not backlog.spine_is_intact():
        broken = [
            f"{earlier}->{later}"
            for earlier, later in zip(CRITICAL_PATH, CRITICAL_PATH[1:], strict=False)
            if not backlog.reaches(later, earlier)
        ]
        raise BacklogError(f"critical-path spine incomplete: broken edges {', '.join(broken)}")


def _validate_identity(backlog: Backlog) -> None:
    epic_ids = [epic.id for epic in backlog.epics]
    if len(set(epic_ids)) != len(epic_ids):
        raise BacklogError("duplicate epic ids")

    story_ids = [story.id for story in backlog.stories]
    duplicates = {sid for sid in story_ids if story_ids.count(sid) > 1}
    if duplicates:
        raise BacklogError(f"duplicate story ids: {', '.join(sorted(duplicates))}")


def _validate_points_and_priority(backlog: Backlog) -> None:
    epic_by_id = {epic.id: epic for epic in backlog.epics}
    for story in backlog.stories:
        epic = epic_by_id[story.epic]
        if story.priority not in VALID_PRIORITIES:
            raise BacklogError(f"{story.id}: invalid priority {story.priority}")
        if story.priority not in {epic.priority, *VALID_PRIORITIES}:
            raise BacklogError(f"{story.id}: priority inconsistent with epic")
        if story.priority == "P0" and epic.priority == "P2":
            raise BacklogError(f"{story.id}: P0 story inside a P2 epic")


def _validate_dependency_graph(backlog: Backlog) -> None:
    for story in backlog.stories:
        if story.id in story.dependencies:
            raise BacklogError(f"{story.id}: depends on itself")
        for dependency in story.dependencies:
            if not backlog.has_story(dependency):
                raise BacklogError(f"{story.id}: unknown dependency '{dependency}'")
            dep_story = backlog.story(dependency)
            if dep_story.sprint > story.sprint:
                raise BacklogError(
                    f"{story.id} (sprint {story.sprint}) depends on {dependency} "
                    f"scheduled later in sprint {dep_story.sprint}"
                )
    # Raises on cycles.
    backlog.topological_order()


def _validate_sprints(backlog: Backlog) -> None:
    assignable = {story.id for story in backlog.stories}
    seen: set[str] = set()
    for sprint in backlog.sprints:
        points = 0
        for story_id in sprint.stories:
            if story_id not in assignable:
                raise BacklogError(f"sprint {sprint.number}: unknown story '{story_id}'")
            if story_id in seen:
                raise BacklogError(f"story '{story_id}' is scheduled in two sprints")
            seen.add(story_id)
            story = backlog.story(story_id)
            if story.sprint != sprint.number:  # pragma: no cover - guarded above
                raise BacklogError(
                    f"{story_id}: sprint {story.sprint} disagrees with sprint {sprint.number} board entry"
                )
            points += story.points
        if points > SPRINT_CAPACITY:
            raise BacklogError(
                f"sprint {sprint.number} ({sprint.name}) is over capacity: {points} > {SPRINT_CAPACITY}"
            )

    unassigned = sorted(assignable - seen)
    if unassigned:
        raise BacklogError(f"stories missing from the sprint board: {', '.join(unassigned)}")


def _validate_traceability(backlog: Backlog) -> None:
    """Every story must say how it will be proven and what it will produce."""
    for story in backlog.stories:
        if not story.test_approach.strip():
            raise BacklogError(f"{story.id}: missing test approach")
        if not story.implemented_by:
            raise BacklogError(f"{story.id}: missing 'implemented_by' artefacts")
        if not story.acceptance_criteria:
            raise BacklogError(f"{story.id}: missing acceptance criteria")


def sprint_points(backlog: Backlog) -> dict[int, int]:
    """Realised points per sprint (validated against the board entries)."""
    totals: dict[int, int] = {}
    for sprint in backlog.sprints:
        totals[sprint.number] = sum(backlog.story(sid).points for sid in sprint.stories)
    return totals


def jira_rows(backlog: Backlog) -> list[dict[str, str]]:
    """Flatten the backlog into Jira-import-ready rows (one row per issue)."""
    rows: list[dict[str, str]] = []
    for story in backlog.stories:
        rows.append(
            {
                "Issue Type": "Story",
                "Issue Key": story.id,
                "Epic": f"{story.epic} — {story.epic_summary}",
                "Summary": story.summary,
                "Description": story.description,
                "Acceptance Criteria": "\n".join(f"- {c}" for c in story.acceptance_criteria),
                "Priority": story.priority,
                "Story Points": str(story.points),
                "Sprint": str(story.sprint),
                "Dependencies": ",".join(story.dependencies),
                "Labels": ",".join(story.labels),
                "Security": story.security,
                "Observability": story.observability,
                "Test Approach": story.test_approach,
                "Implemented By": ",".join(story.implemented_by),
            }
        )
        for subtask in story.subtasks:
            rows.append(
                {
                    "Issue Type": "Subtask",
                    "Issue Key": f"{story.id}-ST",
                    "Epic": f"{story.epic} — {story.epic_summary}",
                    "Summary": f"{story.summary}: {subtask}",
                    "Description": f"Subtask of {story.id}.",
                    "Acceptance Criteria": "",
                    "Priority": story.priority,
                    "Story Points": "",
                    "Sprint": str(story.sprint),
                    "Dependencies": story.id,
                    "Labels": "subtask",
                    "Security": "",
                    "Observability": "",
                    "Test Approach": "",
                    "Implemented By": "",
                }
            )
    return rows
