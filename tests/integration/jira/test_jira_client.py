"""Jira integration (OPS-021).

Two properties matter here. The first is **idempotency**: a retry after a timeout must return the
original issue rather than creating a second one — a tracker that accumulates duplicate tickets
for one incident is a tracker nobody trusts. The second is that context travels with the issue:
evidence summary, priority, labels and a link back to the run.

Idempotency is asserted at three levels, because each one can be defeated independently: the
derived key (``app.integrations.idempotency``), the provider contract (sandbox and live), and the
tool that the agent actually calls.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.errors import IntegrationBadResponse
from app.integrations.http import ResilientHttpClient
from app.integrations.idempotency import MAX_KEY_LENGTH, jira_issue_key
from app.integrations.jira.client import HttpJiraClient, SandboxJiraProvider
from app.sandbox.simulator import get_sandbox

pytestmark = [pytest.mark.story("OPS-021"), pytest.mark.integration]

BASE = "https://jira.test"

PAYLOAD: dict[str, Any] = {
    "incident_id": "INC-JIRA-1",
    "summary": "API error spike on checkout-service",
    "description": "Evidence summary: error_rate 0.184 (baseline 0.012) after release-42.",
    "priority": "P1",
    "evidence_ids": ["EVD-1", "EVD-2"],
    "labels": ["api_error_spike", "SEV2"],
}


@pytest.fixture(autouse=True)
def _clean_sandbox() -> Iterator[None]:
    sandbox = get_sandbox()
    sandbox.reset(scenario="normal")
    yield
    sandbox.reset(scenario="normal")


def build_client(settings: Settings) -> tuple[HttpJiraClient, httpx.AsyncClient]:
    transport = httpx.AsyncClient()
    resilient = ResilientHttpClient(
        "jira",
        base_url=BASE,
        timeout_seconds=settings.request_timeout_seconds,
        max_retries=0,
        headers={"Authorization": "Bearer test-token"},
        client=transport,
        sleep=_no_sleep,
        jitter=lambda: 0.0,
    )
    return HttpJiraClient(resilient, project_key="AIOPS"), transport


async def _no_sleep(_seconds: float) -> None:  # pragma: no cover - made explicit for clarity
    return None


# --------------------------------------------------------------------------- #
# The key is the contract
# --------------------------------------------------------------------------- #


def test_the_key_is_deterministic_and_payload_sensitive() -> None:
    first = jira_issue_key("INC-JIRA-1", PAYLOAD)
    assert first == jira_issue_key("INC-JIRA-1", PAYLOAD), "a retry must derive the same key"

    changed = jira_issue_key("INC-JIRA-1", {**PAYLOAD, "summary": "something else"})
    assert changed != first, "a different issue must not replay the old one"

    other = jira_issue_key("INC-JIRA-2", PAYLOAD)
    assert other != first, "keys are per incident"

    assert len(first) <= MAX_KEY_LENGTH
    assert first.startswith("jira.create_issue:INC-JIRA-1:")


def test_volatile_attempt_context_does_not_change_the_key() -> None:
    """``run_id`` differs between attempts by design and must not create a second ticket."""
    assert jira_issue_key("INC-JIRA-1", {**PAYLOAD, "run_id": "RUN-1"}) == jira_issue_key(
        "INC-JIRA-1", {**PAYLOAD, "run_id": "RUN-2"}
    )


# --------------------------------------------------------------------------- #
# The sandbox provider: replay-safe by construction
# --------------------------------------------------------------------------- #


async def test_a_duplicate_create_returns_the_original_issue() -> None:
    """Two calls with the same key produce exactly one issue."""
    provider = SandboxJiraProvider(get_sandbox(), project_key="AIOPS")
    key = jira_issue_key("INC-JIRA-1", PAYLOAD)

    first = await provider.create_issue(PAYLOAD, key)
    second = await provider.create_issue(PAYLOAD, key)

    assert second["issue_key"] == first["issue_key"], "the retry must not open a second ticket"
    assert first["replayed"] is False
    assert second["replayed"] is True, "the caller can tell a replay from a creation"
    assert len(provider.issues) == 1


async def test_a_different_payload_creates_a_second_issue() -> None:
    provider = SandboxJiraProvider(get_sandbox(), project_key="AIOPS")

    first = await provider.create_issue(PAYLOAD, jira_issue_key("INC-JIRA-1", PAYLOAD))
    second = await provider.create_issue(
        {**PAYLOAD, "summary": "escalation for a different incident"},
        jira_issue_key("INC-JIRA-1", {**PAYLOAD, "summary": "escalation for a different incident"}),
    )

    assert second["issue_key"] != first["issue_key"]
    assert len(provider.issues) == 2


async def test_context_and_comments_attached_to_the_issue() -> None:
    provider = SandboxJiraProvider(get_sandbox(), project_key="AIOPS")
    created = await provider.create_issue(PAYLOAD, jira_issue_key("INC-JIRA-1", PAYLOAD))

    fetched = await provider.get_issue(created["issue_key"])
    assert fetched["summary"] == PAYLOAD["summary"]
    assert fetched["priority"] == "P1"
    assert fetched["incident_id"] == "INC-JIRA-1"

    comment = await provider.add_comment(created["issue_key"], "Timeline: https://aiops.test/ui")
    assert comment["comment_count"] == 1

    missing = "AIOPS-9999"
    with pytest.raises(IntegrationBadResponse):
        await provider.get_issue(missing)
    with pytest.raises(IntegrationBadResponse):
        await provider.add_comment(missing, "cannot comment on nothing")


# --------------------------------------------------------------------------- #
# The live client
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_idempotency_key_is_sent_to_jira(settings: Settings) -> None:
    """The live path sends the key as Jira's own ``idempotencyKey`` parameter."""
    route = respx.post(f"{BASE}/rest/api/3/issue").mock(
        return_value=httpx.Response(201, json={"key": "AIOPS-1042"})
    )

    client, transport = build_client(settings)
    key = jira_issue_key("INC-JIRA-1", PAYLOAD)
    result = await client.create_issue(PAYLOAD, key)
    await transport.aclose()

    request = route.calls[-1].request
    assert request.url.params["idempotencyKey"] == key
    body = json.loads(request.content)
    assert body["fields"]["summary"] == PAYLOAD["summary"]
    assert body["fields"]["description"].startswith("Evidence summary:")
    assert "ai-operations-agent" in body["fields"]["labels"]
    assert result == {
        "issue_key": "AIOPS-1042",
        "url": f"{BASE}/browse/AIOPS-1042",
        "replayed": False,
        "simulated": False,
    }


@respx.mock
async def test_a_response_without_an_issue_key_is_a_typed_error(settings: Settings) -> None:
    """A 200 with no key is a failure: the incident is not tracked, and silence would hide it."""
    respx.post(f"{BASE}/rest/api/3/issue").mock(
        return_value=httpx.Response(200, json={"id": "10042", "self": f"{BASE}/rest/api/3/issue/1"})
    )

    client, transport = build_client(settings)
    with pytest.raises(IntegrationBadResponse) as excinfo:
        await client.create_issue(PAYLOAD, jira_issue_key("INC-JIRA-1", PAYLOAD))
    await transport.aclose()

    assert "did not return an issue key" in str(excinfo.value)


@respx.mock
async def test_read_and_comment_paths(settings: Settings) -> None:
    respx.get(f"{BASE}/rest/api/3/issue/AIOPS-1042").mock(
        return_value=httpx.Response(
            200,
            json={"fields": {"status": {"name": "In Progress"}, "summary": PAYLOAD["summary"]}},
        )
    )
    comment_route = respx.post(f"{BASE}/rest/api/3/issue/AIOPS-1042/comment").mock(
        return_value=httpx.Response(201, json={"id": "20001"})
    )

    client, transport = build_client(settings)
    issue = await client.get_issue("AIOPS-1042")
    comment = await client.add_comment("AIOPS-1042", "Timeline: https://aiops.test/ui")
    await transport.aclose()

    assert issue["status"] == "In Progress"
    assert issue["simulated"] is False
    assert comment["issue_key"] == "AIOPS-1042"
    assert json.loads(comment_route.calls[-1].request.content)["body"].startswith("Timeline:")
