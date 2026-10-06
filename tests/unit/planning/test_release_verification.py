"""Release verification against a running candidate (DEV-003, OPS-070).

``scripts/verify_release.py`` is the gate that decides whether a deploy is allowed to stand. Its
value depends on it being **executed** — a verification script that has never been run against a
real candidate is a paragraph, not a check — so this test starts the real application on a real
socket and runs the script exactly as the CD pipeline does.

What is asserted is the script's judgement, not merely its exit code: every named check passes,
the report names each one, and the lifecycle check proves a *verified* terminal state rather than
a set of HTTP 200s.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Environment, IntegrationsMode, Settings
from app.sandbox.simulator import RECOVERY_SETTLE_SECONDS
from tests.support.app import TOKENS

pytestmark = [pytest.mark.story("DEV-003"), pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_script(name: str) -> Any:
    """Import ``scripts/<name>.py`` by path, the way ``test_backlog.py`` does for its exporter."""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before executing: dataclasses resolve annotations through ``sys.modules``, so a
    # module that defines one cannot be executed anonymously.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def settings(_base_settings: Settings) -> Settings:
    """The candidate under verification: sandbox integrations, short verification window."""
    return _base_settings.model_copy(
        update={
            "env": Environment.TEST,
            "integrations_mode": IntegrationsMode.SANDBOX,
            # The simulator settles recovery over RECOVERY_SETTLE_SECONDS; a shorter window
            # would have the verifier observe a system that has not finished recovering and
            # (correctly) report FAILED.
            "verification_window_seconds": float(RECOVERY_SETTLE_SECONDS + 1),
            "verification_poll_interval_seconds": 0.5,
        }
    )


def test_the_release_verifier_passes_against_a_real_candidate(
    live_server: Any, tmp_path: Path
) -> None:
    """Exit 0, every check named in the report, and the lifecycle evidence recorded."""
    module = load_script("verify_release")
    report_path = tmp_path / "release-report.json"

    exit_code = module.main(
        [
            "--base-url",
            live_server.base_url,
            "--token",
            TOKENS["sre"],
            "--json",
            str(report_path),
        ]
    )

    assert exit_code == 0, "release verification must pass against the candidate it ships with"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["passed"] is True
    names = {check["name"] for check in report["checks"]}
    assert names == {"health", "readiness", "api_contract", "metrics", "safety", "lifecycle"}
    assert all(check["passed"] for check in report["checks"])

    lifecycle = next(check for check in report["checks"] if check["name"] == "lifecycle")
    assert lifecycle["evidence"]["state"] == "RESOLVED"
    assert lifecycle["evidence"]["verification_outcome"].lower() == "success", (
        "a terminal state is not enough: the rollback must have been verified independently"
    )
    assert lifecycle["evidence"]["audit_chain"]["valid"] is True
    observed = {item["to_state"] for item in lifecycle["evidence"]["transitions"]}
    assert {"DETECTED", "INVESTIGATING", "PLANNED", "WAITING_APPROVAL", "EXECUTING"} <= observed, (
        "the lifecycle check must see the whole state machine, not just the end state"
    )


def test_the_safety_check_finds_the_high_risk_gate(live_server: Any) -> None:
    """A HIGH-risk proposal must be waiting for a human, and the report must say so."""
    module = load_script("verify_release")
    verifier = module.ReleaseVerifier(live_server.base_url, token=TOKENS["sre"])

    result = verifier.check_safety()

    assert result.passed is True
    assert result.evidence["incident_id"]
    assert result.evidence["pending_high_risk"] >= 1
    assert result.evidence["auth_enforced"] is True, (
        "with tokens configured, an unauthenticated read must be refused"
    )
    verifier.client.close()


def test_the_contract_check_reads_the_committed_openapi_document(live_server: Any) -> None:
    """The verifier compares against ``docs/architecture/openapi.json``, not against itself."""
    module = load_script("verify_release")
    committed = json.loads(
        (REPO_ROOT / "docs" / "architecture" / "openapi.json").read_text(encoding="utf-8")
    )
    verifier = module.ReleaseVerifier(live_server.base_url, token=TOKENS["sre"])
    result = verifier.check_metrics()
    verifier.client.close()

    assert result.passed is True
    assert result.evidence["missing"] == []
    assert verifier.expected_paths == len(committed["paths"]), (
        "the verifier must count the contract committed in the repository"
    )


def test_an_unreachable_candidate_fails_loudly() -> None:
    """A verification that cannot reach the candidate exits non-zero — never a pass."""
    module = load_script("verify_release")
    exit_code = module.main(["--base-url", "http://127.0.0.1:1", "--token", "irrelevant"])
    assert exit_code == 2


def test_the_cd_workflow_runs_the_verifier_and_gates_production(tmp_path: Path) -> None:
    """DEV-003's pipeline contract: immutable artifact, staging gates, gated production deploy."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "cd.yml").read_text(encoding="utf-8")

    assert "verify_release.py" in workflow, "the CD pipeline must run the release verifier"
    assert "github.sha" in workflow, "the image is tagged with the commit SHA (immutable artifact)"
    assert (
        "environment: production" in workflow or "environment:\n      name: production" in workflow
    ), "the production deploy must sit behind a protected environment"
    assert "rollback" in workflow.lower()
    staging_gates = {"pytest", "evals/runner.py"}
    missing = {gate for gate in staging_gates if gate not in workflow}
    assert not missing, f"staging must run these before promotion: {sorted(missing)}"
