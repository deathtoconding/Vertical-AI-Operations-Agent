#!/usr/bin/env python3
"""Export the validated backlog to Jira-import CSV and a Markdown board.

Usage::

    python scripts/export_backlog.py --csv docs/backlog/jira-import.csv
    python scripts/export_backlog.py --markdown docs/backlog/SPRINT_BOARD.md
    python scripts/export_backlog.py --check        # validate only (used by CI)

The script imports the same validated loader the tests use, so an invalid plan
cannot be exported — planning and code share one contract.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.planning.backlog import (  # noqa: E402
    BacklogError,
    jira_rows,
    load_backlog,
    sprint_points,
)

BOARD_BAR_WIDTH = 24


def render_markdown(backlog_path: str | None = None) -> str:
    backlog = load_backlog(backlog_path)
    totals = sprint_points(backlog)
    lines: list[str] = [
        "<!-- GENERATED FILE — do not edit by hand. -->",
        "<!-- Regenerate with: python scripts/export_backlog.py --markdown docs/backlog/SPRINT_BOARD.md -->",
        "",
        "# Sprint Board — Vertical AI Operations Agent",
        "",
        f"**{len(backlog.stories)} stories · {len(backlog.epics)} epics · "
        f"{len(backlog.sprints)} sprints · {backlog.total_points} points**",
        "",
        "## Capacity view",
        "",
        "```text",
    ]
    peak = max(totals.values()) if totals else 1
    for sprint in backlog.sprints:
        points = totals[sprint.number]
        filled = max(1, round(BOARD_BAR_WIDTH * points / peak))
        bar = "█" * filled
        lines.append(f"SPRINT {sprint.number} {sprint.name:<28} {bar} {points}")
    lines += [
        "```",
        "",
        "## Board",
        "",
    ]
    for sprint in backlog.sprints:
        points = totals[sprint.number]
        lines += [
            f"### Sprint {sprint.number} — {sprint.name} ({points} points)",
            "",
            f"*Outcome:* {sprint.outcome}",
            "",
            "| Story | Summary | Priority | Points | Depends on |",
            "|---|---|---|---:|---|",
        ]
        for story_id in sprint.stories:
            story = backlog.story(story_id)
            deps = ", ".join(story.dependencies) or "—"
            lines.append(
                f"| {story.id} | {story.summary} | {story.priority} | {story.points} | {deps} |"
            )
        lines.append("")

    lines += [
        "## Epic rollup",
        "",
        "| Epic | Summary | Priority | Stories | Points |",
        "|---|---|---|---:|---:|",
    ]
    for epic in backlog.epics:
        stories = [s for s in backlog.stories if s.epic == epic.id]
        lines.append(
            f"| {epic.id} | {epic.summary} | {epic.priority} | {len(stories)} | "
            f"{sum(s.points for s in stories)} |"
        )

    lines += [
        "",
        "## Critical path",
        "",
        "```text",
        " → ".join(backlog.critical_path()),
        "```",
        "",
    ]
    return "\n".join(lines)


def export_csv(target: Path, backlog_path: str | None = None) -> int:
    rows = jira_rows(load_backlog(backlog_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=None, help="write Jira-import CSV here")
    parser.add_argument("--markdown", type=Path, default=None, help="write sprint board Markdown here")
    parser.add_argument("--check", action="store_true", help="validate the backlog and exit")
    parser.add_argument("--backlog", type=str, default=None, help="path to backlog.yaml")
    args = parser.parse_args(argv)

    try:
        backlog = load_backlog(args.backlog)
    except BacklogError as exc:
        print(f"BACKLOG INVALID: {exc}", file=sys.stderr)
        return 1

    totals = sprint_points(backlog)
    print(
        f"backlog ok: {len(backlog.stories)} stories, {len(backlog.epics)} epics, "
        f"{backlog.total_points} points, sprints {min(totals)}..{max(totals)}"
    )

    if args.csv:
        count = export_csv(args.csv, args.backlog)
        print(f"wrote {count} rows to {args.csv}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(args.backlog), encoding="utf-8")
        print(f"wrote {args.markdown}")
    if not (args.csv or args.markdown or args.check):
        for number, points in totals.items():
            sprint = next(s for s in backlog.sprints if s.number == number)
            print(f"  sprint {number} ({sprint.name}): {points} points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
