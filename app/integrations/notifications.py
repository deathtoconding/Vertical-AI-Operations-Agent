"""Slack message construction, shared by the sandbox and the live provider (OPS-022).

Both providers must send the *same structure*: if the sandbox built richer blocks than the live
client, every block-structure test would be testing the simulator rather than the system. The
message is therefore built here, once:

* the top-level ``text`` is the fallback Slack shows in a notification preview — it carries the
  severity and the incident id, so a human who never opens the channel still knows what happened;
* a ``section`` block carries the summary;
* a ``context`` block carries the evidence count and the approval link when they are known.

Nothing here reaches the network, and nothing here trusts its input: the caller passes text that
has already been through :func:`~app.core.sanitization.sanitize_for_external`.
"""

from __future__ import annotations

from typing import Any


def build_slack_message(
    payload: dict[str, Any], *, default_channel: str | None = None
) -> dict[str, Any]:
    """Return the JSON body for one operational notification."""
    incident_id = str(payload.get("incident_id") or "unknown")
    severity = str(payload.get("severity") or "SEV3")
    summary = str(payload.get("text") or "").strip()
    action_url = payload.get("action_url")
    evidence_count = payload.get("evidence_count")
    channel = payload.get("channel") or default_channel

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{severity}* — incident `{incident_id}`\n{summary}",
            },
        }
    ]

    context: list[str] = []
    if evidence_count is not None:
        context.append(f"{int(evidence_count)} evidence item(s) referenced")
    if action_url:
        context.append(f"<{action_url}|review and decide>")
    if context:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " · ".join(context)}],
            }
        )

    body: dict[str, Any] = {
        "text": f"[{severity}] incident {incident_id}: {summary}".strip(),
        "blocks": blocks,
    }
    if channel:
        body["channel"] = channel
    return body


__all__ = ["build_slack_message"]
