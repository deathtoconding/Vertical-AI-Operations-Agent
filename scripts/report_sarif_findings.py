#!/usr/bin/env python3
"""Turn a scanner's SARIF report into check annotations (SEC-005).

A scanner that fails with "see job summary for details" leaves its findings in a job summary and a
log file that only some people can open, and the trivy step reports through its exit code alone.
This script reads the SARIF report these scanners already write (`results.sarif` from gitleaks,
`trivy.sarif` from the image scan) and re-emits every result as a GitHub *annotation*: the finding
shows up on the check run, next to the file and line, and it is readable through the API.

Annotations carry the rule id, the location and — for scanners that scan commits, such as
gitleaks — the commit and the `.gitleaksignore` fingerprint. They never carry the secret itself:
the report is generated with `--redact`, and re-printing a value would undo that. A report with
results exits 1, so the reporting step is itself blocking; `--fingerprint-only` turns it into a
reporting aid that never fails the job.

Usage::

    python scripts/report_sarif_findings.py results.sarif --label gitleaks
    python scripts/report_sarif_findings.py trivy.sarif --label trivy
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


def annotate(result: dict[str, Any], label: str = "scanner") -> str:
    """Build the annotation line for one SARIF result."""
    rule_id = str(result.get("ruleId") or "unknown-rule")
    locations = result.get("locations") or [{}]
    physical = (locations[0] or {}).get("physicalLocation") or {}
    file_path = str((physical.get("artifactLocation") or {}).get("uri") or "")
    region = physical.get("region") or {}
    line = region.get("startLine")
    commit = str((result.get("partialFingerprints") or {}).get("commitSha") or "")

    properties = f"file={_escape_property(file_path)}"
    if line:
        properties += f",line={_escape_property(str(line))}"
    message = f"{label}: {rule_id} in {file_path or 'the image'}"
    if commit:
        fingerprint = ":".join([commit, file_path, rule_id, str(line or 1)])
        message += f" at {commit[:8]} — fingerprint {fingerprint}"
    return f"::error {properties}::{_escape_data(message)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="SARIF report written by the scanner")
    parser.add_argument("--label", default="scanner", help="tool name used in the annotation")
    parser.add_argument(
        "--fingerprint-only",
        action="store_true",
        help="report and annotate, but never fail the step (the scanner step owns the verdict)",
    )
    args = parser.parse_args(argv)

    if not args.report.exists():
        print(f"::notice::no {args.label} report at {args.report} — nothing to annotate")
        return 0

    document = json.loads(args.report.read_text(encoding="utf-8"))
    results = results_of(document)
    if not results:
        print(f"no {args.label} findings to annotate")
        return 0

    print(f"::error::{len(results)} {args.label} finding(s) — see the annotations on this check")
    for result in results:
        print(annotate(result, args.label))
        print(
            "  → if the finding is a false positive, record why where the fix lands: a rule "
            "allowlist in configs/gitleaks.toml, or the fingerprint in .gitleaksignore"
        )
    return 0 if args.fingerprint_only else 1


if __name__ == "__main__":
    sys.exit(main())
