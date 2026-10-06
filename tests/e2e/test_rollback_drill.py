"""Rollback drill (DEV-004).

A rollback procedure that has never been executed against a running candidate is a paragraph,
not a procedure. This test runs ``scripts/rollback_drill.py`` end to end against the real
application on a real socket and requires the drill's own verdict:

1. a degraded release is injected (rolling back a healthy system would prove nothing);
2. the agent proposes the HIGH-risk rollback, policy holds it at the approval gate, and a human
   approves with the payload hash — the same path production uses;
3. the executor runs the rollback and the **verification engine independently confirms** that the
   release changed back, the deployment is healthy and the metric recovered;
4. the drill records the evidence in a JSON artefact, which is what gets attached to the release.
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

pytestmark = [pytest.mark.story("DEV-004"), pytest.mark.e2e, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def settings(_base_settings: Settings, request: pytest.FixtureRequest) -> Settings:
    """A candidate whose recovery settle time matches the simulator's, not the production 90 s.

    Individual tests can override single fields with ``pytest.mark.parametrize`` + ``indirect``.
    """
    overrides: dict[str, Any] = getattr(request, "param", None) or {}
    return _base_settings.model_copy(
        update={
            "env": Environment.TEST,
            "integrations_mode": IntegrationsMode.SANDBOX,
            "verification_window_seconds": float(RECOVERY_SETTLE_SECONDS + 1),
            "verification_poll_interval_seconds": 0.5,
            **overrides,
        }
    )


def test_the_rollback_drill_verifies_recovery_end_to_end(live_server: Any, tmp_path: Path) -> None:
    module = load_script("rollback_drill")
    evidence_path = tmp_path / "rollback-drill.json"

    exit_code = module.main(
        [
            "--base-url",
            live_server.base_url,
            "--token",
            TOKENS["sre"],
            "--json",
            str(evidence_path),
        ]
    )

    assert exit_code == 0, "the documented rollback procedure must work against the candidate"
    report = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["scenarios"][0] == "A"
    steps = {step["name"]: step for step in report["steps"]}
    assert set(steps) == {"baseline", "before", "inject", "rollback", "after"}
    assert all(step["passed"] for step in report["steps"]), steps

    # The drill must have rolled back from a genuinely degraded release, not a healthy one.
    assert steps["before"]["evidence"]["release"]["healthy"] is True
    assert steps["inject"]["evidence"]["release"]["healthy"] is False
    assert steps["inject"]["evidence"]["incident_ids"]

    # The rollback step proves the human gate was engaged and that verification was independent.
    rollback = steps["rollback"]["evidence"]
    assert rollback["tool_name"] == "deployment.rollback_simulation"
    assert rollback["state"] == "RESOLVED"
    assert rollback["verification_outcome"].lower() == "success"
    assert rollback["audit_chain"]["valid"] is True

    # After: the release moved, the system is healthy again, and the rollback was recorded.
    after = steps["after"]["evidence"]
    assert after["release"]["healthy"] is True
    assert (
        after["release"]["active_release"]
        == steps["before"]["evidence"]["release"]["active_release"]
    ), "the drill must restore the pre-drill release, not merely move off it"
    assert after["release"]["active_release"] != after["injected_release"], (
        "the injected (bad) release must no longer be serving traffic"
    )
    assert after["sandbox"]["rollback_count"] >= 1
    assert after["sandbox"]["rollback_log"], "the simulator records what was rolled back, and why"


def test_the_drill_refuses_to_claim_a_rollback_that_never_happened(live_server: Any) -> None:
    """With no approval to consume, the drill fails instead of reporting a successful drill."""
    module = load_script("rollback_drill")
    drill = module.RollbackDrill(live_server.base_url, token=TOKENS["sre"])

    step = drill.run_rollback("INC-DOES-NOT-EXIST", timeout_seconds=1.0)

    assert step.passed is False
    assert "no approval was requested" in step.detail
    drill.client.close()


@pytest.mark.parametrize("settings", [{"max_rollbacks_per_hour": 0}], indirect=True)
def test_a_guardrail_refusal_fails_the_drill_with_a_diagnosis(
    live_server: Any, tmp_path: Path
) -> None:
    """The rollback rate limit is a guardrail, not a bug — and the drill must say so.

    With the hourly rollback budget spent, policy refuses the proposal. The drill has to fail
    (recovery was not exercised) *and* explain that policy refused it, so an operator does not go
    hunting for a broken approval flow.
    """
    module = load_script("rollback_drill")
    evidence_path = tmp_path / "rollback-drill.json"

    exit_code = module.main(
        [
            "--base-url",
            live_server.base_url,
            "--token",
            TOKENS["sre"],
            "--json",
            str(evidence_path),
            "--approval-timeout",
            "3",
        ]
    )

    assert exit_code == 1, "a guardrail refusal is a failed drill, never a quiet pass"
    report = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert report["passed"] is False
    rollback = next(step for step in report["steps"] if step["name"] == "rollback")
    assert rollback["passed"] is False
    assert "no approval was requested" in rollback["detail"]
    diagnosis = rollback["evidence"]["diagnosis"]
    assert diagnosis["rejected"], "the drill must report the policy reason it was refused with"
    assert "deployment.rollback_simulation" in diagnosis["rejected"][0]
    assert "guardrail" in diagnosis["hint"]
