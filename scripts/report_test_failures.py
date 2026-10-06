#!/usr/bin/env python3
"""Re-emit failing tests from a JUnit XML report as check annotations (DEV-002).

Why this exists: on the first v1.0.0 pipelines the *only* thing a reader of a red `ci` run could
see was "Process completed with exit code 1" — the step that failed was identifiable, the test
that failed was not, because the traceback lives in a log file. A failure that has to be hunted
through a log is a failure that gets retried instead of fixed.

The pipeline already writes JUnit XML for every pytest invocation (``--junitxml``). This script
turns each failure into a GitHub check annotation carrying the test id, the source file and line
when the traceback names one, and the assertion message — the same visibility the scanner
annotations give the security job.

Usage::

    python scripts/report_test_failures.py "artifacts/junit-*.xml" --label ci

Exit code: 1 when the report contains failures or errors (so the step is blocking on its own),
0 when everything passed or when there is no report to read — a *reporting* failure must never
masquerade as a test failure.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

# A GitHub annotation is rendered in one line; keep the message, not the essay.
MAX_MESSAGE = 220
# Tracebacks point at the failing frame with `path/to/file.py:12:`. The last match is the
# assertion site (pytest prints the test's own file last), so read the report backwards.
FRAME = re.compile(r"([\w./\\-]+\.py):(\d+)")

# `::error file=…,line=…,title=…::message` — GitHub requires these escapes or the annotation is
# dropped (and a raw newline truncates the rest of the report).
_PROPERTY = {"%": "%25", "\r": "%0D", "\n": "%0A", ":": "%3A", ",": "%2C"}
_MESSAGE = {"%": "%25", "\r": "%0D", "\n": "%0A"}


@dataclass(frozen=True)
class Failure:
    """One failing test, with as much location as the report carries."""

    test: str
    path: str
    line: int | None
    message: str


def escape_property(value: str) -> str:
    return "".join(_PROPERTY.get(character, character) for character in value)


def escape_message(value: str) -> str:
    return "".join(_MESSAGE.get(character, character) for character in value)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _classname_path(classname: str) -> str:
    """``tests.unit.planning.test_backlog`` -> ``tests/unit/planning/test_backlog.py``."""
    parts = [part for part in classname.split(".") if part]
    if not parts:
        return ""
    parts[-1] = f"{parts[-1]}.py"
    return "/".join(parts)


def failures_in(root: ElementTree.Element, source: str) -> list[Failure]:
    """Every ``<failure>``/``<error>`` test case in one report."""
    found: list[Failure] = []
    for case in root.iter("testcase"):
        node = case.find("failure")
        if node is None:
            node = case.find("error")
        if node is None:
            continue
        body = (node.text or "").strip()
        frames = FRAME.findall(body)
        path, line = (frames[-1][0], int(frames[-1][1])) if frames else ("", None)
        if not path:
            path = _classname_path(case.get("classname", ""))
        message = _first_line(node.get("message") or "") or _last_line(body) or source
        found.append(
            Failure(
                test=case.get("name", "unknown"),
                path=path,
                line=line,
                message=message[:MAX_MESSAGE],
            )
        )
    return found


def annotate(failure: Failure, label: str) -> str:
    """One ``::error`` line naming the test, its location and what it asserted."""
    location = f"file={escape_property(failure.path)}," if failure.path else ""
    if failure.line:
        location += f"line={failure.line},"
    return (
        f"::error {location}title={escape_property(failure.test)}::"
        f"{escape_message(f'{label}: {failure.test} — {failure.message}')}"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="*", help="JUnit XML files or glob patterns")
    parser.add_argument("--label", default="ci", help="prefix for every annotation")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    paths: list[Path] = []
    for pattern in args.reports:
        candidate = Path(pattern)
        if any(character in pattern for character in "*?["):
            # A wildcard pattern is matched through its parent; a plain path is taken as given
            # only when it exists, which is how "the step that writes it never ran" is detected.
            paths.extend(sorted(candidate.parent.glob(candidate.name)))
        elif candidate.exists():
            paths.append(candidate)

    failures: list[Failure] = []
    cases = 0
    skipped = 0
    for path in paths:
        try:
            # The report is written by this pipeline a moment earlier, not by an outside party.
            root = ElementTree.parse(path).getroot()  # noqa: S314
        except (ElementTree.ParseError, OSError) as error:  # pragma: no cover - defensive
            print(f"::warning::cannot read {path}: {error}", flush=True)
            continue
        cases += sum(1 for _ in root.iter("testcase"))
        skipped += sum(1 for case in root.iter("testcase") if case.find("skipped") is not None)
        failures.extend(failures_in(root, path.name))

    for failure in failures:
        print(annotate(failure, args.label), flush=True)

    if not paths:
        print(f"::notice::{args.label}: no test report to read — nothing to annotate", flush=True)
        return 0

    summary = f"{args.label}: {cases - len(failures)} passed, {skipped} skipped"
    if failures:
        print(f"::error::{summary}, {len(failures)} failed", flush=True)
        return 1
    print(f"::notice::{summary}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through ``main``
    raise SystemExit(main())
