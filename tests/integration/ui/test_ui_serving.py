"""Operator console serving (UI-001, UI-002, UI-003).

The console is a *view* over the public API, and these tests hold it to that:

* UI-001 — the assets are served from the same origin, they reference only routes that exist in
  the OpenAPI contract, and the views have explicit empty and error states;
* UI-002 — the approval flow writes only to the approval endpoints, sends the exact payload hash
  it displayed (so a stale tab cannot approve a changed action), and offers approve / reject /
  escalate;
* UI-003 — every rendered value goes through safe DOM APIs: there is no ``innerHTML`` sink, so
  "the builder escapes HTML" holds by construction rather than by remembering to escape.

The JavaScript is asserted statically *and* the assets are fetched over HTTP, so a rename in
either the code or the markup is caught.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest

from app.core.config import Environment, IntegrationsMode, Settings
from app.sandbox.simulator import RECOVERY_SETTLE_SECONDS
from tests.support.app import auth

pytestmark = [
    pytest.mark.story("UI-001"),
    pytest.mark.story("UI-002"),
    pytest.mark.story("UI-003"),
    pytest.mark.integration,
]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
STATIC_DIR = REPO_ROOT / "app" / "ui" / "static"
STATIC_FILES = ("index.html", "app.js", "app.css")

#: Sinks that would bypass the DOM builder's automatic escaping.
UNSAFE_SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
)

#: Endpoints the approval flow is allowed to write to. Nothing else.
APPROVAL_FLOW_WRITES = {"/approvals/{id}/decision", "/agents/runs/{id}/resume", "/agents/runs"}

#: Operational probes the console reads outside the versioned API.
OPERATIONAL_READS = {"/ready", "/health", "/metrics"}

SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
)


@pytest.fixture
def settings(_base_settings: Settings) -> Settings:
    """The console tests drive a real lifecycle, so the verification settle window is short."""
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


def read_asset(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def app_js() -> str:
    return read_asset("app.js")


def normalise(path: str) -> str:
    """``/incidents/${incidentId}`` and ``/incidents/{incident_id}`` describe the same route."""
    return re.sub(r"\$\{[^}]+\}|\{[^}]+\}", "{id}", path).split("?")[0].rstrip("/")


def write_calls(source: str) -> set[str]:
    """Paths called with ``method: "POST"`` (the only writes the console performs)."""
    calls: set[str] = set()
    for match in re.finditer(r"api\(\s*[`\"]([^`\"]+)[`\"]\s*,\s*\{", source):
        window = source[match.end() : match.end() + 200]
        if 'method: "POST"' in window:
            calls.add(normalise(match.group(1)))
    return calls


# --------------------------------------------------------------------------- #
# UI-001 — serving and contract
# --------------------------------------------------------------------------- #


async def test_the_console_and_its_assets_are_served_from_the_same_origin(api: Any) -> None:
    console = await api.client.get("/")
    assert console.status_code == 200
    assert "text/html" in console.headers["content-type"]
    assert "<html" in console.text.lower()

    for name in STATIC_FILES:
        asset = await api.client.get(f"/ui/{name}")
        assert asset.status_code == 200, f"/ui/{name} is not served"
        assert asset.content, f"/ui/{name} is empty"

    script = await api.client.get("/ui/app.js")
    assert "javascript" in script.headers["content-type"]
    stylesheet = await api.client.get("/ui/app.css")
    assert "css" in stylesheet.headers["content-type"]


async def test_the_console_sends_a_restrictive_content_security_policy(api: Any) -> None:
    """No inline and no remote script: the console cannot become an exfiltration path."""
    response = await api.client.get("/")
    policy = response.headers.get("content-security-policy", "")
    assert "default-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "unsafe-eval" not in policy
    assert "'unsafe-inline'" not in policy.split("style-src")[0]
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("x-content-type-options") == "nosniff"


def test_the_console_loads_nothing_from_the_public_internet() -> None:
    """A dashboard that needs a CDN is unavailable exactly when it is needed."""
    html = read_asset("index.html")
    remote = [
        match
        for match in re.findall(r'(?:src|href)="([^"]+)"', html)
        if match.startswith(("http://", "https://", "//"))
    ]
    assert not remote, f"the console references remote assets: {remote}"
    assert re.search(r"<script[^>]*\bsrc=", html), "the script must be an external same-origin file"
    assert not re.findall(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html), "inline script is forbidden"


async def test_every_api_path_the_console_uses_is_in_the_openapi_contract(api: Any) -> None:
    """UI-001's rule, checked mechanically: the console reads only the public API."""
    document = (await api.client.get("/openapi.json")).json()
    documented = {normalise(path) for path in document["paths"]}
    source = app_js()
    prefix = re.search(r'const API = "([^"]+)"', source)
    assert prefix, "the console must declare the versioned API prefix it uses"

    used = {
        normalise(f"{prefix.group(1)}{value}")
        for value in re.findall(r"api\(\s*[`\"]([^`\"]+)[`\"]", source)
    }
    undocumented = {path for path in used if path not in documented}
    assert not undocumented, f"the console calls undocumented routes: {sorted(undocumented)}"
    assert len(used) >= 4, f"expected the console to use several endpoints, saw {sorted(used)}"

    fetched = {
        "/" + normalise(value) for value in re.findall(r"""fetch\(\s*[`"]/([^`"]+)[`"]""", source)
    }
    unexpected = sorted(fetched - OPERATIONAL_READS)
    assert fetched <= OPERATIONAL_READS, f"non-operational routes fetched directly: {unexpected}"


def test_the_console_assets_contain_no_credentials() -> None:
    offenders: list[str] = []
    for name in STATIC_FILES:
        text = read_asset(name)
        for shape in SECRET_SHAPES:
            if shape.search(text):
                offenders.append(f"{name} matches {shape.pattern}")
    assert not offenders, "; ".join(offenders)
    assert "Authorization" not in app_js(), (
        "the browser sends the operator's own credentials; the console must not hold any"
    )


def test_the_views_handle_the_empty_and_error_states_explicitly() -> None:
    source = app_js()
    assert "no incidents" in source.lower()
    assert "No approvals are waiting" in source
    assert source.count("catch (") >= 4, "every loader must handle its own failure"
    assert "banner(`" in source, "errors are surfaced in the banner, not swallowed"
    assert 'className = "banner' in source or "banner ${" in source


# --------------------------------------------------------------------------- #
# UI-002 — the approval interface
# --------------------------------------------------------------------------- #


def test_the_approval_flow_writes_only_to_the_approval_endpoints() -> None:
    source = app_js()
    start = source.index("async function decide(")
    end = source.index("/* ", start)
    writes = write_calls(source[start:end])
    assert writes == APPROVAL_FLOW_WRITES, f"unexpected write endpoints: {sorted(writes)}"

    # And the console as a whole writes nowhere except the approval flow, starting a run and the
    # (sandbox-only) scenario injection — every one of which the API authorises by role.
    assert write_calls(source) <= APPROVAL_FLOW_WRITES | {"/detection/simulate"}


def test_all_three_decisions_are_offered() -> None:
    source = app_js()
    for decision in ("APPROVED", "REJECTED", "ESCALATED"):
        assert f'"{decision}"' in source, f"the console cannot express {decision}"


def test_the_decision_carries_the_payload_hash_that_was_displayed() -> None:
    """Bound to content: an operator's click approves the payload they were shown, or nothing."""
    source = app_js()
    assert "payload_hash: approval.payload_hash" in source
    assert "hash ${approval.payload_hash.slice(0, 12)}" in source, (
        "the operator must be shown the hash they are approving"
    )


def test_authorisation_is_not_implemented_in_the_console() -> None:
    """Authorisation is the API's job; a UI that gates on a client-side role can be bypassed."""
    source = app_js()
    assert "role" not in source.lower(), "no client-side role checks belong in the console"


# --------------------------------------------------------------------------- #
# UI-003 — escaping and the activity view
# --------------------------------------------------------------------------- #


def test_the_builder_has_no_unsafe_html_sink() -> None:
    """Escaping holds by construction: text is set with ``textContent``, never parsed as HTML."""
    source = app_js()
    for sink in UNSAFE_SINKS:
        assert sink not in source, f"the console uses the unsafe sink {sink!r}"
    assert "node.textContent = text" in source, "the builder must set text nodes"


def test_rendered_values_go_through_the_dom_builder_or_textcontent() -> None:
    source = app_js()
    rendered = len(re.findall(r"\bel\(", source)) + len(re.findall(r"\.textContent\s*=", source))
    assert rendered >= 20, "rendering should flow through the safe helpers"
    assert not re.search(r"<\$\{", source), "template literals must not build markup"


async def test_the_activity_view_renders_stages_risks_and_outcomes(api: Any) -> None:
    """UI-003's content is driven by the run payload; assert the shapes it renders exist."""
    source = app_js()
    for field in ("run.state", "transitions", "risk", "outcome"):
        assert field in source, f"the activity view does not render {field}"

    scenario = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": True, "reset": True},
    )
    assert scenario.status_code == 200
    incident_id = str(scenario.json()["incidents_created"][0])
    detail = (await api.client.get(f"/api/v1/incidents/{incident_id}")).json()

    run = detail["run"]
    assert run["state"] in {
        "NEW",
        "DETECTED",
        "INVESTIGATING",
        "PLANNED",
        "WAITING_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "RESOLVED",
        "FAILED",
        "ESCALATED",
    }
    # Approve and resume so the run reaches execution — that is when tool invocations exist.
    pending = await api.client.get("/api/v1/approvals", params={"pending_only": "true"})
    approval = next(
        item for item in pending.json()["approvals"] if item["incident_id"] == incident_id
    )
    await api.client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        json={
            "decision": "APPROVED",
            "payload_hash": approval["payload_hash"],
            "reason": "ui test",
        },
    )
    await api.client.post(f"/api/v1/agents/runs/{run['id']}/resume", json={"reason": "ui test"})

    run_detail = (await api.client.get(f"/api/v1/agents/runs/{run['id']}")).json()
    assert run_detail["run"]["state"] in {"RESOLVED", "FAILED", "ESCALATED"}
    assert run_detail["transitions"], "the activity view needs the transition list"
    first = run_detail["transitions"][0]
    for field in ("from_state", "to_state", "occurred_at"):
        assert field in first, f"a transition without {field} cannot be rendered"

    invocations = run_detail["tool_invocations"]
    assert invocations, "a tool ran during scenario A, so the activity view has something to show"
    assert {"tool_name", "risk", "outcome"} <= set(invocations[0]), (
        "UI-003 requires tool name, risk and result per invocation"
    )


async def test_a_viewer_may_read_the_console_but_the_api_refuses_the_write(
    api: Any, engine: Any
) -> None:
    """Role restrictions are the API's, so the same rule holds no matter which client asks."""
    readable = await api.client.get("/api/v1/incidents", headers=auth("viewer"))
    assert readable.status_code == 200

    refused = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": True, "reset": True},
        headers=auth("viewer"),
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "authorization_denied"
