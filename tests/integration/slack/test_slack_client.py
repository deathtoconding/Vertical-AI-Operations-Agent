"""Slack integration (OPS-022).

A notification is a claim made to a human: "incident INC-…, SEV2, this is what we found, here is
where to approve". Two failure modes matter more than delivery itself:

* a notification that silently fails to send leaves the on-call engineer believing the agent is
  watching — so a failure must surface, loudly, as a typed error;
* a notification carrying untrusted evidence must not become a chat-abuse vector, so the payload
  is sanitised and size-bounded on the way out.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import IntegrationBadResponse, IntegrationUnavailable
from app.core.sanitization import sanitize_for_external
from app.integrations.http import ResilientHttpClient
from app.integrations.slack.client import HttpSlackClient, SandboxSlackProvider
from app.sandbox.simulator import get_sandbox
from app.tools.slack import SPEC as SLACK_SPEC
from app.tools.slack import notify_tool

pytestmark = [pytest.mark.story("OPS-022"), pytest.mark.integration]

BASE = "https://slack.test"
HOOK = "https://hooks.slack.test/services/T000/B000/XXXX"

PAYLOAD: dict[str, Any] = {
    "incident_id": "INC-SLACK-1",
    "severity": "SEV2",
    "text": "checkout-service error_rate 0.184 after release-42; rollback proposed",
    "action_url": "https://aiops.test/ui?approval=APR-1",
    "channel": "#ops",
}


@pytest.fixture(autouse=True)
def _clean_sandbox() -> Iterator[None]:
    sandbox = get_sandbox()
    sandbox.reset(scenario="normal")
    yield
    sandbox.reset(scenario="normal")


def build_client(
    settings: Settings, *, max_retries: int = 0
) -> tuple[HttpSlackClient, httpx.AsyncClient]:
    transport = httpx.AsyncClient()
    resilient = ResilientHttpClient(
        "slack",
        base_url=HOOK,
        timeout_seconds=settings.request_timeout_seconds,
        max_retries=max_retries,
        client=transport,
        sleep=_no_sleep,
        jitter=lambda: 0.0,
    )
    return HttpSlackClient(resilient, default_channel="#ops"), transport


async def _no_sleep(_seconds: float) -> None:  # pragma: no cover - explicit no-op
    return None


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #


async def test_the_sandbox_notification_carries_the_operational_context() -> None:
    """Incident id, severity and the summary line — the fields an on-call human acts on."""
    provider = SandboxSlackProvider(get_sandbox(), default_channel="#ops")

    message = await provider.notify(PAYLOAD)

    assert message["channel"] == "#ops"
    assert message["simulated"] is True
    assert PAYLOAD["incident_id"] in message["text"]
    blocks = message["blocks"]
    assert blocks and blocks[0]["type"] == "section"
    rendered = blocks[0]["text"]["text"]
    assert "SEV2" in rendered
    assert PAYLOAD["incident_id"] in rendered
    assert "rollback proposed" in rendered
    assert message["message_ts"], "a delivery receipt is required for verification"


async def test_the_same_notification_twice_is_recognisable_as_a_replay() -> None:
    provider = SandboxSlackProvider(get_sandbox(), default_channel="#ops")
    first = await provider.notify(PAYLOAD)
    second = await provider.notify(PAYLOAD)
    assert first["message_ts"] == second["message_ts"], "the id is derived from the content"


async def test_the_channel_can_be_read_back_for_independent_verification() -> None:
    """``history`` is what the verification engine reads instead of trusting the send call."""
    provider = SandboxSlackProvider(get_sandbox(), default_channel="#ops")
    await provider.notify(PAYLOAD)

    history = await provider.history(limit=10)
    assert history["total"] == 1
    stored = history["messages"][-1]["text"]
    assert PAYLOAD["text"] in stored, "the summary value survives delivery"
    assert "INC-SLACK-1" in stored and "SEV2" in stored, "the fallback identifies the incident"


async def test_the_notification_schema_is_bounded() -> None:
    """A notification cannot be used to push an unbounded payload at a chat workspace."""
    schema = SLACK_SPEC.input_schema
    assert schema["additionalProperties"] is False
    assert schema["properties"]["text"]["maxLength"] == 3000
    assert schema["properties"]["severity"]["pattern"] == "^SEV[1-4]$"
    assert schema["required"] == ["incident_id", "text"]

    from app.tools.schemas import SlackNotifyParams

    with pytest.raises(ValidationError):
        SlackNotifyParams.model_validate({"incident_id": "INC-1", "text": "x" * 3001})
    with pytest.raises(ValidationError):
        SlackNotifyParams.model_validate({"incident_id": "INC-1", "text": "hi", "severity": "SEV9"})
    with pytest.raises(ValidationError):
        SlackNotifyParams.model_validate({"incident_id": "INC-1", "text": "hi", "extra": "nope"})


def test_untrusted_text_is_sanitised_before_it_leaves_the_process() -> None:
    """Mentions and markup from evidence become inert text, and the size stays bounded."""
    hostile = "<!channel> <script>alert(1)</script> " + "x" * 4000
    clean = sanitize_for_external(hostile, max_chars=3000)

    assert len(clean) <= 3000
    assert "<!channel>" not in clean
    assert "[mention:channel]" in clean


# --------------------------------------------------------------------------- #
# Failure surfacing
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_rejected_notification_raises_instead_of_reporting_success(
    settings: Settings,
) -> None:
    """Slack answers HTTP 200 with ``ok: false`` — that is a failure and must be typed as one."""
    respx.post(HOOK).mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
    )

    client, transport = build_client(settings)
    with pytest.raises(IntegrationBadResponse) as excinfo:
        await client.notify(PAYLOAD)
    await transport.aclose()

    assert "rejected the notification" in str(excinfo.value)


@respx.mock
async def test_an_unreachable_slack_is_unavailable(settings: Settings) -> None:
    respx.post(HOOK).mock(side_effect=httpx.ConnectError("no route to host"))

    client, transport = build_client(settings)
    with pytest.raises(IntegrationUnavailable):
        await client.notify(PAYLOAD)
    await transport.aclose()


@respx.mock
async def test_a_successful_send_returns_the_delivery_receipt(settings: Settings) -> None:
    respx.post(HOOK).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "ts": "1759752000.000100", "channel": "C123"}
        )
    )

    client, transport = build_client(settings)
    result = await client.notify(PAYLOAD)
    await transport.aclose()

    assert result == {"message_ts": "1759752000.000100", "channel": "C123", "simulated": False}


@respx.mock
async def test_the_tool_reports_failure_when_delivery_is_not_confirmed(settings: Settings) -> None:
    """``slack.notify`` must not turn a missing receipt into a successful tool call (OPS-022)."""
    respx.post(HOOK).mock(return_value=httpx.Response(200, json={"ok": True}))

    from app.domain.enums import ToolOutcome
    from app.tools.registry import ToolContext
    from app.tools.schemas import SlackNotifyParams

    client, transport = build_client(settings)
    context = ToolContext(
        settings=settings,
        integrations=cast(Any, type("I", (), {"slack_notify": staticmethod(client.notify)})()),
        run_id="RUN-1",
    )

    handler = notify_tool().handler
    result = await handler(
        SlackNotifyParams.model_validate({"incident_id": "INC-SLACK-1", "text": "hi"}), context
    )
    await transport.aclose()

    assert result.success is False
    assert result.outcome is ToolOutcome.FAILURE
    assert result.error == "slack did not confirm delivery"
    assert result.external_id is None


async def test_the_tool_marks_a_sandbox_delivery_as_simulated(settings: Settings) -> None:
    """Sandbox traffic is labelled simulated all the way through the tool result."""
    from app.tools.registry import ToolContext
    from app.tools.schemas import SlackNotifyParams

    provider = SandboxSlackProvider(get_sandbox(), default_channel="#ops")
    context = ToolContext(
        settings=settings,
        integrations=cast(Any, type("I", (), {"slack_notify": staticmethod(provider.notify)})()),
        run_id="RUN-1",
    )

    result = await notify_tool().handler(
        SlackNotifyParams.model_validate(
            {"incident_id": "INC-SLACK-1", "text": "investigation complete", "severity": "SEV2"}
        ),
        context,
    )

    assert result.success is True
    assert result.simulated is True
    assert result.external_id
