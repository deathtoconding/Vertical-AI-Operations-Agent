#!/usr/bin/env python3
"""Fail the build on medium/high bandit findings (SEC-005).

``bandit -q`` exits non-zero on *any* finding, but it does not distinguish "this is a test fixture
using a known-weak hash" from "this is a production path" — and a step that fails on everything
ends the job before this threshold is ever applied. The pipeline therefore runs bandit with
``--exit-zero`` (bandit reports, this checker decides) and prints the offending lines so a
reviewer can see what the pipeline saw.

Usage::

    bandit -q --exit-zero -c pyproject.toml -r app scripts -f json -o bandit.json
    python scripts/check_bandit.py bandit.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BLOCKING = {"MEDIUM", "HIGH"}


def blocking_findings(document: dict[str, Any]) -> list[dict[str, Any]]:
    results = document.get("results", [])
    return [
        finding
        for finding in results
        if str(finding.get("issue_severity", "")).upper() in BLOCKING
        and str(finding.get("issue_confidence", "")).upper() in {"HIGH", "MEDIUM"}
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="bandit JSON report")
    args = parser.parse_args(argv)

    if not args.report.exists():
        print(f"bandit report not found: {args.report}", file=sys.stderr)
        return 2

    document = json.loads(args.report.read_text(encoding="utf-8"))
    findings = blocking_findings(document)
    total = len(document.get("results", []))
    if findings:
        print(f"BLOCKING BANDIT FINDINGS ({len(findings)} of {total}):", file=sys.stderr)
        for finding in findings:
            location = f"{finding.get('filename')}:{finding.get('line_number')}"
            print(
                f"  [{finding.get('issue_severity')}] {finding.get('test_id')} "
                f"{finding.get('issue_text')} ({location})",
                file=sys.stderr,
            )
        return 1

    print(f"bandit clean at medium/high severity ({total} informational findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
