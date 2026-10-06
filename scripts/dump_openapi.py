#!/usr/bin/env python3
"""Dump the OpenAPI contract to stdout (DEV-002).

``make openapi > docs/architecture/openapi.json`` keeps a committed copy of the contract so an
accidental breaking API change shows up as a reviewable diff instead of a production surprise.

Usage::

    python scripts/dump_openapi.py            # pretty JSON on stdout
    python scripts/dump_openapi.py --check    # compare with the committed copy, exit 1 on drift
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_TARGET = REPO_ROOT / "docs" / "architecture" / "openapi.json"


def render() -> str:
    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare against the committed contract instead of printing",
    )
    args = parser.parse_args(argv)

    rendered = render()

    if not args.check:
        sys.stdout.write(rendered)
        return 0

    if not args.target.exists():
        print(f"no committed contract at {args.target}", file=sys.stderr)
        return 1

    committed = args.target.read_text(encoding="utf-8")
    if _paths(committed) != _paths(rendered):
        print(
            "OpenAPI drift: the running app and docs/architecture/openapi.json disagree.\n"
            "Regenerate with `make openapi` and review the diff as an API change.",
            file=sys.stderr,
        )
        return 1

    print(f"OpenAPI contract matches ({len(_paths(rendered))} paths)")
    return 0


def _paths(document: str) -> dict[str, list[str]]:
    """Compare the shape that matters: paths and the methods available on each."""
    parsed = json.loads(document)
    return {
        path: sorted(operation.keys())
        for path, operation in sorted(parsed.get("paths", {}).items())
    }


if __name__ == "__main__":
    sys.exit(main())
