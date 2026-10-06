"""Structured logging: correlation and redaction (SRE-002).

Two guarantees are tested here, and both are security-relevant:

* an incident can be reconstructed from logs alone (``incident_id``/``run_id`` on every line);
* a secret that reaches a log call is redacted by a *processor*, so forgetting to redact at
  the call site cannot leak it (threat T-27).

The second guarantee is why these tests run the **configured processor chain** rather than
calling the helpers directly: the helpers can be correct while the chain never invokes them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
import structlog

from app.core.logging import (
    CORRELATION_KEYS,
    REDACTED,
    _redaction_processor,
    bind_correlation,
    clear_correlation,
    configure_logging,
    current_correlation,
    redact_mapping,
    redact_value,
    reset_logging_for_tests,
)

pytestmark = [pytest.mark.story("SRE-002"), pytest.mark.unit]


#: Synthetic credentials used to prove the redaction processor works.
#:
#: They are assembled at run time on purpose: a complete provider-shaped literal in the source
#: trips secret-scanning push protection, and a scanner that cries wolf on our own fixtures is a
#: scanner people learn to ignore (SEC-005). The values still match the value-shape patterns the
#: redactor is built on, so the assertions remain meaningful.
FAKE_GITHUB_TOKEN = "gh" + "p_" + "a" * 36
FAKE_SLACK_TOKEN = "xox" + "b-" + "0" * 12 + "-" + "b" * 20
FAKE_API_KEY = "sk-" + "a" * 24
FAKE_JWT = "Bearer " + "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"


@pytest.fixture(autouse=True)
def _clean_logging_state():
    reset_logging_for_tests()
    yield
    reset_logging_for_tests()


def render(event: dict[str, Any]) -> dict[str, Any]:
    """Push one event through the chain ``configure_logging`` installed, and parse the result."""
    configure_logging("DEBUG", json_output=True)
    payload: Any = dict(event)
    for processor in structlog.get_config()["processors"]:
        payload = processor(None, "info", payload)
        if isinstance(payload, str):
            return json.loads(payload)
    raise AssertionError("the configured chain has no renderer")


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #


def test_correlation_ids_round_trip_and_clear() -> None:
    bind_correlation(request_id="req-1", incident_id="INC-1", agent_run_id="RUN-1")
    assert current_correlation() == {
        "request_id": "req-1",
        "incident_id": "INC-1",
        "agent_run_id": "RUN-1",
    }

    clear_correlation()
    assert current_correlation() == {}


def test_unset_correlation_keys_are_omitted_rather_than_null() -> None:
    bind_correlation(incident_id="INC-1")
    bound = current_correlation()
    assert bound == {"incident_id": "INC-1"}
    assert set(bound) <= set(CORRELATION_KEYS)


def test_correlation_is_preserved_across_an_await() -> None:
    bind_correlation(incident_id="INC-async")

    async def inner() -> dict[str, str]:
        await asyncio.sleep(0)
        return current_correlation()

    assert asyncio.run(inner()) == {"incident_id": "INC-async"}


def test_correlation_is_scoped_to_the_task_that_binds_it() -> None:
    async def worker() -> dict[str, str]:
        bind_correlation(incident_id="INC-worker")
        return current_correlation()

    assert asyncio.run(worker()) == {"incident_id": "INC-worker"}
    assert current_correlation() == {}


def test_correlation_ids_are_attached_to_rendered_lines() -> None:
    bind_correlation(request_id="req-42", incident_id="INC-42")
    line = render({"event": "investigation_started"})
    assert line["request_id"] == "req-42"
    assert line["incident_id"] == "INC-42"


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "key",
    ["api_token", "github_token", "password", "llm_api_key", "authorization", "session_id"],
)
def test_sensitive_keys_are_redacted_regardless_of_value(key: str) -> None:
    assert redact_value(key, "some-value") == REDACTED


@pytest.mark.parametrize(
    "value",
    [
        f"bearer {FAKE_GITHUB_TOKEN}",
        FAKE_API_KEY,
        FAKE_SLACK_TOKEN,
        FAKE_JWT,
    ],
)
def test_credential_shaped_values_are_redacted_even_under_a_harmless_key(value: str) -> None:
    assert REDACTED in str(redact_value("note", value))


def test_secrets_nested_in_structures_are_redacted() -> None:
    payload = {
        "tool": "jira.create_incident",
        "headers": {"Authorization": "Bearer super-secret-value"},
        "attempts": [{"token": "abc"}, {"code": 200}],
    }
    redacted = redact_mapping(payload)
    assert redacted["headers"]["Authorization"] == REDACTED
    assert redacted["attempts"][0]["token"] == REDACTED
    assert redacted["attempts"][1]["code"] == 200
    assert redacted["tool"] == "jira.create_incident", "non-secrets are untouched"


def test_oversized_values_are_truncated_with_a_marker() -> None:
    redacted = redact_value("message", "x" * 5000)
    assert isinstance(redacted, str)
    assert len(redacted) < 5000
    assert "truncated" in redacted


def test_ordinary_values_pass_through_unchanged() -> None:
    assert redact_value("metric", "error_rate") == "error_rate"
    assert redact_value("z_score", 121.0) == 121.0
    assert redact_value("healthy", True) is True


def test_the_processor_redacts_a_credential_however_it_is_named() -> None:
    event = _redaction_processor(
        None, "info", {"event": "integration_call", "auth": FAKE_GITHUB_TOKEN}
    )
    assert event["auth"] == REDACTED


def test_a_secret_reaching_a_log_call_is_redacted_by_the_configured_chain() -> None:
    line = render({"event": "integration_call", "token": FAKE_GITHUB_TOKEN, "status": 200})
    assert line["token"] == REDACTED, "the chain must redact, not just the helper"
    assert line["status"] == 200
    assert line["event"] == "integration_call"


def test_logging_configuration_is_idempotent() -> None:
    """The first call wins: a second call must not swap the renderer or the level filter."""
    configure_logging("INFO", json_output=True)
    first = structlog.get_config()
    before_level = logging.getLogger().level

    configure_logging("DEBUG", json_output=False)  # ignored: already configured
    second = structlog.get_config()

    assert second["processors"] == first["processors"]
    assert any(isinstance(item, structlog.processors.JSONRenderer) for item in second["processors"])
    assert logging.getLogger().level == before_level


def test_reset_allows_deliberate_reconfiguration() -> None:
    configure_logging("INFO", json_output=True)
    reset_logging_for_tests()
    configure_logging("WARNING", json_output=False)
    processors = structlog.get_config()["processors"]
    assert not any(isinstance(item, structlog.processors.JSONRenderer) for item in processors)
