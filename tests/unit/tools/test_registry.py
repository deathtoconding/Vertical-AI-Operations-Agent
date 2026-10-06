"""Tool registry tests — the security boundary (OPS-051).

The registry is where "the model cannot do more than we allow" becomes mechanical. These tests
assert the *shape* of the boundary, not just its behaviour on one happy path.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.core.errors import ToolNotFound, ValidationFailed
from app.domain.enums import RiskLevel, ToolOutcome
from app.tools.registry import ToolContext, build_default_registry

pytestmark = [pytest.mark.story("OPS-051"), pytest.mark.security, pytest.mark.unit]

FORBIDDEN_TOKENS = ("shell", "exec", "sql", "filesystem", "http.request", "python", "kubectl")


def test_registry_exposes_only_bounded_tools() -> None:
    registry = build_default_registry()
    names = registry.names()
    assert len(names) == 8
    for name in names:
        assert not any(token in name for token in FORBIDDEN_TOKENS), name


def test_every_tool_declares_a_known_permission_and_sane_limits() -> None:
    from app.tools.permissions import PERMISSION_NAMES

    for spec in build_default_registry().specs():
        assert spec.permission in PERMISSION_NAMES, spec.name
        assert spec.timeout_seconds > 0
        assert spec.max_retries >= 0
        assert spec.description, spec.name
        assert spec.input_schema, f"{spec.name} must publish an input schema"


def test_high_risk_tools_always_require_approval() -> None:
    for spec in build_default_registry().specs():
        if spec.is_high_risk:
            assert spec.requires_approval, f"{spec.name} is high risk but does not require approval"


def test_rollback_is_high_risk_and_simulated() -> None:
    spec = build_default_registry().get("deployment.rollback_simulation").spec
    assert spec.risk is RiskLevel.HIGH
    assert spec.requires_approval is True
    # The MVP performs a *simulation*; the spec says so, and so does the tool metadata.
    assert spec.simulated is True


def test_unknown_tool_lookup_raises_and_is_counted() -> None:
    from app.core.telemetry import TOOL_INVOCATIONS, reset_for_tests

    reset_for_tests()
    registry = build_default_registry()
    with pytest.raises(ToolNotFound):
        registry.get("shell.exec")
    assert registry.find("shell.exec") is None
    values = _counter_values(TOOL_INVOCATIONS)
    assert values.get(("shell.exec", "low", ToolOutcome.UNKNOWN_TOOL.value), 0) == 1, values


def test_duplicate_registration_is_rejected() -> None:
    registry = build_default_registry()
    definition = registry.get("slack.notify")
    with pytest.raises(ValueError):
        registry.register(definition)


async def test_invalid_parameters_never_reach_the_handler(settings) -> None:
    """Validation happens before the handler: a malformed call cannot have a side effect."""
    registry = build_default_registry()
    definition = registry.get("deployment.rollback_simulation")
    context = ToolContext(settings=settings, integrations=cast(Any, object()), actor="test")
    with pytest.raises(ValidationFailed) as excinfo:
        await definition.invoke({"target_release": "latest", "reason": "x"}, context)
    assert "errors" in excinfo.value.details


async def test_placeholder_release_targets_are_rejected(settings) -> None:
    registry = build_default_registry()
    definition = registry.get("deployment.rollback_simulation")
    context = ToolContext(settings=settings, integrations=cast(Any, object()), actor="test")
    for bad in ("release-0", "release-latest", "main"):
        with pytest.raises(ValidationFailed):
            await definition.invoke(
                {"target_release": bad, "reason": "x", "incident_id": "INC-1"}, context
            )


def test_tools_cannot_accept_arbitrary_fields(settings) -> None:
    """Strict parameter models: an unexpected field is an error, not a silent ignore."""
    from pydantic import ValidationError

    from app.tools.schemas import SlackNotifyParams

    with pytest.raises(ValidationError):
        # Built from a mapping so the *unknown* field is a runtime validation error rather
        # than a type error in the test itself — that is the behaviour under test.
        SlackNotifyParams.model_validate(
            {"text": "x", "incident_id": "INC-1", "shell_command": "id"}
        )


def _counter_values(counter) -> dict[tuple[str, ...], float]:
    """Map ``(label values in fixed order)`` to the counter value.

    Label order is taken from the metric definition, never from dict iteration order, so this
    helper cannot silently compare the wrong series.
    """
    from app.core.telemetry import _normalise

    values: dict[tuple[str, ...], float] = {}
    names = list(counter._labelnames)
    expected = _normalise(counter._name)
    for metric in counter.collect()[0].samples:
        if not metric.labels or _normalise(metric.name) != expected:
            continue
        values[tuple(str(metric.labels[name]) for name in names)] = metric.value
    return values
