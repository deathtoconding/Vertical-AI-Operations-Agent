#!/usr/bin/env python3
"""AI evaluation runner (EVAL-002).

Runs the golden dataset through the **real** agent pipeline (detector, collector, investigator,
planner, policy engine, verification checks) and grades the result, then writes a
machine-readable report.

Usage::

    python evals/runner.py --config evals/config.yaml
    python evals/runner.py --config evals/config.yaml --report evals/results/report.json
    python evals/runner.py --config evals/config.yaml --update-baseline
    python evals/runner.py --config evals/config.yaml --check-regression

Exit codes: ``0`` all thresholds met, ``1`` a threshold was missed or a safety violation was
recorded, ``2`` the run could not be executed (bad configuration, missing dataset).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from pydantic import SecretStr  # noqa: E402

from app.core.config import Environment, IntegrationsMode, Settings  # noqa: E402
from app.evaluation.harness import Harness  # noqa: E402
from app.evaluation.schemas import DIMENSIONS, EvalReport, load_dataset  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "evals" / "config.yaml"


def load_config(path: Path) -> dict[str, Any]:
    """Read and sanity-check the evaluation configuration."""
    if not path.exists():
        raise FileNotFoundError(f"evaluation config not found: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    for key in ("dataset", "reasoner", "report", "thresholds", "regression"):
        if key not in document:
            raise ValueError(f"{path} is missing the required section '{key}'")
    unknown_dimensions = (
        set(document["thresholds"])
        - set(DIMENSIONS)
        - {
            "overall",
            "pass_rate",
        }
    )
    if unknown_dimensions:
        raise ValueError(f"thresholds reference unknown dimensions: {sorted(unknown_dimensions)}")
    return document


def build_settings(mode: str, *, allow_live: bool) -> Settings:
    """Sandbox settings for an evaluation run.

    A live reasoner is never used unless the configuration explicitly allows it *and* a key is
    present: an evaluation that silently depends on a paid endpoint is an evaluation that stops
    running when the key expires.
    """
    import os

    api_key = os.environ.get("AIOPS_LLM_API_KEY", "") if allow_live else ""
    return Settings(
        env=Environment.TEST,
        log_level="WARNING",
        integrations_mode=IntegrationsMode.SANDBOX,
        llm_api_key=SecretStr(api_key),
        tracing_enabled=False,
        rate_limit_requests_per_minute=100_000,
        detection_min_samples=6,
        detection_window_minutes=30,
    )


def threshold_failures(report: EvalReport, thresholds: dict[str, float]) -> list[str]:
    failures: list[str] = []
    for dimension, floor in thresholds.items():
        observed = report.metrics.get(dimension)
        if observed is None:
            failures.append(f"{dimension}: no measurement in the report (floor {floor})")
            continue
        if observed < floor:
            failures.append(f"{dimension}: {observed:.4f} < floor {floor:.2f}")
    if report.hard_failures:
        failures.extend(f"SAFETY: {detail}" for detail in report.hard_failures)
    return failures


def render_summary(report: EvalReport) -> str:
    lines = [
        "",
        f"AI evaluation — {report.dataset} v{report.dataset_version} (reasoner: {report.reasoner})",
        f"cases: {report.case_count}   passed: {'yes' if report.passed else 'no'}",
        "",
    ]
    for dimension in DIMENSIONS:
        score = report.metrics.get(dimension)
        if score is None:
            continue
        lines.append(f"  {dimension:<18} {score:.4f}  {'OK' if score >= 0.85 else 'below'}")
    for key in ("overall", "pass_rate"):
        if key in report.metrics:
            lines.append(f"  {key:<18} {report.metrics[key]:.4f}")
    failing = [case for case in report.cases if not case.passed]
    if failing:
        lines.append("")
        lines.append("  failing cases:")
        for case in failing:
            detail = (
                "; ".join(grade.detail for grade in case.grades if not grade.passed) or "see report"
            )
            lines.append(f"    - {case.case_id} [{case.category}]: {detail[:180]}")
    if report.hard_failures:
        lines.append("")
        lines.append("  SAFETY VIOLATIONS (hard failures):")
        for detail in report.hard_failures:
            lines.append(f"    - {detail}")
    lines.append("")
    return "\n".join(lines)


def write_report(report: EvalReport, path: Path, *, include_artefacts: bool) -> None:
    document = report.model_dump(mode="json")
    if not include_artefacts:
        for case in document["cases"]:
            case["artefacts"] = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def run(config_path: Path) -> tuple[EvalReport, dict[str, Any]]:
    config = load_config(config_path)
    dataset_path = REPO_ROOT / config["dataset"]["path"]
    dataset = load_dataset(dataset_path)
    include = config["dataset"].get("include", "all")
    if include != "all":
        wanted = set(include)
        dataset = dataset.model_copy(
            update={"cases": [case for case in dataset.cases if case.category in wanted]}
        )

    settings = build_settings(
        str(config["reasoner"].get("mode", "deterministic")),
        allow_live=bool(config["reasoner"].get("allow_live_calls", False)),
    )
    harness = Harness(settings)
    report = await harness.run_dataset(dataset)
    return report, config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path, default=None, help="override the report path")
    parser.add_argument(
        "--check-regression",
        action="store_true",
        help="compare against the stored baseline (EVAL-003 gate)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="write this run as the new baseline (review like any other change)",
    )
    args = parser.parse_args(argv)

    try:
        report, config = asyncio.run(run(args.config))
    except (FileNotFoundError, ValueError) as exc:
        print(f"EVALUATION NOT RUN: {exc}", file=sys.stderr)
        return 2

    report_path = args.report or (REPO_ROOT / config["report"]["path"])
    write_report(report, report_path, include_artefacts=bool(config["report"]["include_artefacts"]))
    print(render_summary(report))
    print(f"report: {report_path}")

    failures = threshold_failures(report, config["thresholds"])

    if args.check_regression:
        from evals.regression import compare_with_baseline, load_baseline

        baseline_path = REPO_ROOT / config["regression"]["baseline"]
        if baseline_path.exists():
            baseline = load_baseline(baseline_path)
            failures.extend(
                compare_with_baseline(
                    report,
                    baseline,
                    max_drop=config["regression"]["max_drop"],
                    zero_tolerance=bool(config["regression"].get("safety_zero_tolerance", True)),
                    fail_on_newly_failing=bool(
                        config["regression"].get("fail_on_newly_failing_case", True)
                    ),
                )
            )
        else:
            print(f"note: no baseline at {baseline_path} — regression gate skipped")

    if args.update_baseline:
        from evals.regression import baseline_document

        baseline_path = REPO_ROOT / config["regression"]["baseline"]
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline_document(report), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"baseline updated: {baseline_path}")

    if failures:
        print("EVALUATION FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("EVALUATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
