#!/usr/bin/env python3
"""Assert every direct dependency is pinned to an exact version (SEC-005).

The workflow calls this before ``pip-audit``: auditing a floating requirement set tells you
nothing, because the audited resolution is not the resolution you ship.

Usage::

    python scripts/check_pins.py            # exits non-zero on any unpinned requirement
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXACT = "=="


def requirement_strings(pyproject: Path) -> list[tuple[str, str]]:
    """Return ``(group, requirement)`` for every declared dependency."""
    document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = document.get("project", {})
    entries: list[tuple[str, str]] = [
        ("dependencies", item) for item in project.get("dependencies", [])
    ]
    for group, items in (project.get("optional-dependencies") or {}).items():
        entries.extend((f"optional-dependencies.{group}", item) for item in items)
    return entries


def unpinned(requirement: str) -> bool:
    """A requirement is pinned when its first version specifier is ``==`` and it has no ranges."""
    for marker in (";", " "):
        requirement = requirement.split(marker, 1)[0]
    _, _, specifier = requirement.partition("[")
    specifier = specifier.partition("]")[2] if specifier else ""
    if not specifier:
        # No extras: find the specifier after the package name.
        specifier = requirement[len(requirement.split("=")[0].split("<")[0].split(">")[0]) :]
    specifier = specifier.strip()
    if specifier == "":
        return True
    # Reject ranges, exclusions and compatible-release operators: only exact pins count.
    if any(operator in specifier for operator in (">=", "<=", "~=", ">", "<", "!=")):
        return True
    return not specifier.startswith(EXACT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=REPO_ROOT / "pyproject.toml")
    args = parser.parse_args(argv)

    failures = [
        f"{group}: {requirement}"
        for group, requirement in requirement_strings(args.pyproject)
        if unpinned(requirement)
    ]
    if failures:
        print("UNPINNED DEPENDENCIES:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    total = len(requirement_strings(args.pyproject))
    print(f"all {total} declared dependencies are pinned to exact versions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
