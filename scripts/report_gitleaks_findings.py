#!/usr/bin/env python3
"""Turn a gitleaks SARIF report into check annotations (SEC-005).

The gitleaks job fails with "🛑 Leaks detected, see job summary for details" — and then the
findings live in a job summary and a log file that only some people can open. This script reads
the SARIF report the action already writes (`report-path=results.sarif`) and re-emits every result
as a GitHub *annotation*: the finding shows up on the check run, next to the file and line in the
diff, and it is readable through the API.

Annotations carry the rule id, the location, the commit and the `.gitleaksignore` fingerprint —
never the secret itself, because the report is generated with `--redact` and a re-printed secret
would undo that. `--fingerprint-only` turns the step into a reporting aid that never fails the job;
by default a report with results exits 1, so the gate stays blocking even if the scanner step is
re-run for reporting alone.

Usage::

    python scripts/report_gitleaks_findings.py results.sarif
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _escape_data(value: str) -> str:
    """Escape a workflow-command *message* (GitHub Actions command syntax)."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    """Escape a workflow-command *property* value: the message escapes plus ``:`` and ``,``."""
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def results_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    runs = document.get("runs") or []
    return [result for run in runs for result in (run.get("results") or [])]


def annotate(result: dict[str, Any]) -> str:
    """Build the annotation line for one SARIF result."""
    rule_id = str(result.get("ruleId") or "unknown-rule")
    locations = result.get("locations") or [{}]
    physical = (locations[0] or {}).get("physicalLocation") or {}
    file_path = str((physical.get("artifactLocation") or {}).get("uri") or "")
    region = physical.get("region") or {}
    line = region.get("startLine")
    commit = str((result.get("partialFingerprints") or {}).get("commitSha") or "")
    fingerprint = ":".join([commit, file_path, rule_id, str(line or 1)])

    properties = f"file={_escape_property(file_path)}"
    if line:
        properties += f",line={_escape_property(str(line))}"
    message = f"{rule_id} at {commit[:8] or 'unknown commit'} — fingerprint {fingerprint}"
    return f"::error {properties}::{_escape_data(message)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="SARIF report written by gitleaks")
    parser.add_argument(
        "--fingerprint-only",
        action="store_true",
        help="report and annotate, but never fail the step (the scanner step owns the verdict)",
    )
    args = parser.parse_args(argv)

    if not args.report.exists():
        print(f"::notice::no gitleaks report at {args.report} — nothing to annotate")
        return 0

    document = json.loads(args.report.read_text(encoding="utf-8"))
    results = results_of(document)
    if not results:
        print("no gitleaks findings to annotate")
        return 0

    print(f"::error::{len(results)} gitleaks finding(s) — see the annotations on this check")
    for result in results:
        print(annotate(result))
        print(
            "  → if the finding is a false positive, add its fingerprint to .gitleaksignore "
            "in the same commit that explains why"
        )
    return 0 if args.fingerprint_only else 1


if __name__ == "__main__":
    sys.exit(main())
