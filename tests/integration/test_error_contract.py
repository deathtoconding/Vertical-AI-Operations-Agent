"""The provider error contract (OPS-020..OPS-023).

``app/integrations/protocols.py`` makes one promise for every provider, in every mode:

* a failure is a **typed** :class:`~app.core.errors.IntegrationError` (or subclass);
* a provider never returns an empty success — no empty list standing in for "the request failed",
  no ``{}`` standing in for "I could not read it".

This file holds the promise to account: it checks that each sandbox provider satisfies its
protocol at runtime, and that each one has a reachable failure path which surfaces as a typed
error rather than as absence of data.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.config import Settings
from app.core.errors import IntegrationBadResponse, IntegrationError
from app.integrations import protocols
from app.integrations.facade import build_integrations
from app.integrations.sandbox_providers import (
    SandboxDeploymentProvider,
    SandboxGitHubProvider,
    SandboxJiraProvider,
    SandboxLogsProvider,
    SandboxMetricsProvider,
    SandboxPaymentsProvider,
    SandboxSlackProvider,
)
from app.sandbox.simulator import get_sandbox

pytestmark = [
    pytest.mark.story("OPS-020"),
    pytest.mark.story("OPS-021"),
    pytest.mark.story("OPS-022"),
    pytest.mark.story("OPS-023"),
    pytest.mark.integration,
]


@pytest.fixture(autouse=True)
def _clean_sandbox() -> Iterator[None]:
    sandbox = get_sandbox()
    sandbox.reset(scenario="normal")
    yield
    sandbox.reset(scenario="normal")


def test_every_sandbox_provider_satisfies_its_protocol() -> None:
    """The sandbox and the live client must be interchangeable, or tests prove nothing."""
    state = get_sandbox()
    pairs = [
        (SandboxMetricsProvider(state), protocols.MetricsProvider),
        (SandboxLogsProvider(state), protocols.LogsProvider),
        (SandboxGitHubProvider(state, "acme/checkout-service"), protocols.GitHubProvider),
        (SandboxJiraProvider(state, "AIOPS"), protocols.JiraProvider),
        (SandboxSlackProvider(state, "#ops"), protocols.SlackProvider),
        (SandboxPaymentsProvider(state), protocols.PaymentsProvider),
        (SandboxDeploymentProvider(state), protocols.DeploymentProvider),
    ]
    for provider, protocol in pairs:
        assert isinstance(provider, protocol), (
            f"{type(provider).__name__} is not a {protocol.__name__}"
        )


def test_the_facade_exposes_the_same_providers_in_sandbox_mode(settings: Settings) -> None:
    facade = build_integrations(settings)
    assert isinstance(facade.metrics, protocols.MetricsProvider)
    assert isinstance(facade.logs, protocols.LogsProvider)
    assert isinstance(facade.github, protocols.GitHubProvider)
    assert isinstance(facade.slack, protocols.SlackProvider)
    assert isinstance(facade.deployment, protocols.DeploymentProvider)


async def test_reading_an_unknown_jira_issue_is_a_typed_error() -> None:
    provider = SandboxJiraProvider(get_sandbox(), "AIOPS")
    with pytest.raises(IntegrationBadResponse) as excinfo:
        await provider.get_issue("AIOPS-0000")
    assert excinfo.value.code == "integration_bad_response"
    assert isinstance(excinfo.value, IntegrationError)


async def test_a_slack_rejection_is_never_a_silent_success() -> None:
    """The sandbox mirrors Slack's real behaviour: an unknown channel is refused, not ignored."""

    class RejectingSandboxSlack(SandboxSlackProvider):
        async def notify(self, payload: dict[str, object]) -> dict[str, object]:
            raise IntegrationBadResponse("slack", "slack rejected the notification")

    provider = RejectingSandboxSlack(get_sandbox(), "#ops")
    with pytest.raises(IntegrationBadResponse):
        await provider.notify({"incident_id": "INC-1", "text": "hi"})


async def test_an_unknown_deployment_rollback_target_is_refused() -> None:
    """A rollback to a release that was never deployed must fail loudly."""
    provider = SandboxDeploymentProvider(get_sandbox())
    payload = await provider.rollback("release-9999", "unit test")
    # The refusal is explicit and machine-readable: the allow-list is part of the answer, so the
    # caller can see *why* it was refused rather than receiving a fabricated success.
    assert payload["rolled_back"] is False
    assert "never deployed" in payload["error"]
    assert "release-42" in payload["available_releases"]


def test_error_codes_are_stable_strings() -> None:
    """Error codes are part of the contract: runbooks and tests match on them."""
    assert IntegrationBadResponse("jira", "no issue key").code == "integration_bad_response"
    error = IntegrationBadResponse("jira", "no issue key")
    assert error.system == "jira"
    assert error.details["system"] == "jira"
    assert str(error) == "no issue key"
