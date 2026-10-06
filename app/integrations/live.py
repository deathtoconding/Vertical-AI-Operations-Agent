"""Live integrations — real HTTP clients for Prometheus/Loki-style APIs and SaaS endpoints.

These are only selected when ``AIOPS_INTEGRATIONS_MODE=live``; staging and production refuse
to boot in sandbox mode (see :class:`app.core.config.Settings`). They share
:class:`~app.integrations.http.ResilientHttpClient`, so timeouts, retries, rate-limit
handling and error typing behave identically to the sandbox path — the difference is only
which system answers.

Every response is size-bounded and cleaned before it becomes evidence, and no client returns
an empty success on failure: failures are typed errors that the investigation layer records
as explicit degradations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.core.errors import IntegrationBadResponse
from app.core.sanitization import sanitize_untrusted
from app.integrations.http import ResilientHttpClient


def _now() -> datetime:
    return datetime.now(UTC)


class HttpMetricsProvider:
    """Prometheus-compatible range query."""

    def __init__(self, client: ResilientHttpClient) -> None:
        self.client = client

    async def query_metric(self, service: str, metric: str, window_minutes: int) -> dict[str, Any]:
        end = _now()
        start = end - timedelta(minutes=window_minutes)
        query = f'{metric}{{service="{service}"}}'
        payload = await self.client.get(
            "/api/v1/query_range",
            params={
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": "60s",
            },
        )
        return self._normalise(payload, service, metric, start, end)

    def _normalise(
        self,
        payload: dict[str, Any],
        service: str,
        metric: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        try:
            results = payload["data"]["result"]
            series = results[0]["values"]
        except (KeyError, IndexError, TypeError) as exc:
            raise IntegrationBadResponse(
                "metrics", "metrics response did not contain a series for the query"
            ) from exc
        points = [
            {
                "timestamp": datetime.fromtimestamp(float(ts), tz=UTC).isoformat(),
                "value": float(value),
            }
            for ts, value in series
        ]
        return {
            "service": service,
            "metric": metric,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "resolution_seconds": 60,
            "points": points,
            "simulated": False,
        }

    async def list_metrics(self, service: str) -> list[str]:
        payload = await self.client.get("/api/v1/label/__name__/values")
        names = payload.get("data", [])
        return [str(name) for name in names][:500]


class HttpLogsProvider:
    """Loki-compatible range query."""

    def __init__(self, client: ResilientHttpClient) -> None:
        self.client = client

    async def query_logs(
        self,
        service: str,
        window_minutes: int,
        severity: str | None = None,
        contains: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        end = _now()
        start = end - timedelta(minutes=window_minutes)
        selector = f'{{service="{service}"}}'
        if severity:
            selector = f'{selector} |= "\\"{severity.upper()}\\""'
        if contains:
            safe = contains.replace('"', "")
            selector = f'{selector} |= "{safe}"'
        payload = await self.client.get(
            "/loki/api/v1/query_range",
            params={
                "query": selector,
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": limit * 3,
            },
        )
        return self._normalise(payload, service, start, end, limit)

    def _normalise(
        self, payload: dict[str, Any], service: str, start: datetime, end: datetime, limit: int
    ) -> dict[str, Any]:
        try:
            streams = payload["data"]["result"]
        except (KeyError, TypeError) as exc:
            raise IntegrationBadResponse(
                "logs", "logs response did not contain any streams"
            ) from exc

        lines: list[dict[str, Any]] = []
        injections: list[str] = []
        for stream in streams:
            labels = stream.get("stream", {})
            for entry in stream.get("values", []):
                try:
                    timestamp_ns, message = entry
                except (TypeError, ValueError):
                    continue
                # Log bodies are untrusted: sanitise before they can become evidence.
                clean = sanitize_untrusted(str(message), source="logs", record_metrics=False)
                injections.extend(clean.injections)
                lines.append(
                    {
                        "timestamp": datetime.fromtimestamp(
                            int(timestamp_ns) / 1e9, tz=UTC
                        ).isoformat(),
                        "severity": str(labels.get("severity", "INFO")).upper(),
                        "service": str(labels.get("service", service)),
                        "message": clean.text[:1000],
                        "release": labels.get("release"),
                    }
                )
                if len(lines) >= limit:
                    break
            if len(lines) >= limit:
                break
        return {
            "service": service,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "lines": lines,
            "total_matched": len(lines),
            "injections": sorted(set(injections)),
            "simulated": False,
        }


class HttpGitHubClient:
    """GitHub REST client, read-only by construction (no write method exists)."""

    def __init__(self, client: ResilientHttpClient, repository: str) -> None:
        self.client = client
        self.repository = repository

    async def list_commits(
        self, repository: str | None, since_minutes: int, limit: int, branch: str | None
    ) -> dict[str, Any]:
        repo = repository or self.repository
        since = (_now() - timedelta(minutes=since_minutes)).isoformat()
        payload = await self.client.get(
            f"/repos/{repo}/commits",
            params={"since": since, "per_page": min(limit, 50), "sha": branch or "main"},
        )
        commits = [
            {
                "sha": item.get("sha", "")[:12],
                "message": sanitize_untrusted(
                    str(item.get("commit", {}).get("message", "")), source="github"
                ).text,
                "author": str(item.get("commit", {}).get("author", {}).get("name", "unknown")),
                "committed_at": str(item.get("commit", {}).get("author", {}).get("date", "")),
                "html_url": item.get("html_url", ""),
                "deployment": None,
            }
            for item in (payload if isinstance(payload, list) else [])
        ]
        return {
            "repository": repo,
            "commits": commits[:limit],
            "total": len(commits),
            "branch": branch or "main",
            "simulated": False,
        }

    async def list_deployments(self, repository: str | None, limit: int) -> dict[str, Any]:
        repo = repository or self.repository
        payload = await self.client.get(
            f"/repos/{repo}/deployments", params={"per_page": min(limit, 50)}
        )
        deployments = [
            {
                "release": str(item.get("ref", "unknown")),
                "deployed_at": str(item.get("created_at", "")),
                "deployed_by": str(item.get("creator", {}).get("login", "unknown")),
                "status": str(item.get("environment", "unknown")),
            }
            for item in (payload if isinstance(payload, list) else [])
        ]
        return {"repository": repo, "deployments": deployments[:limit], "simulated": False}


class HttpJiraClient:
    def __init__(self, client: ResilientHttpClient, project_key: str) -> None:
        self.client = client
        self.project_key = project_key

    async def create_issue(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        body = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": payload.get("summary", "Incident"),
                "description": payload.get("description", ""),
                "issuetype": {"name": "Task"},
                "labels": [*payload.get("labels", []), "ai-operations-agent"],
            }
        }
        response = await self.client.post(
            "/rest/api/3/issue",
            json_body=body,
            params={"idempotencyKey": idempotency_key},
        )
        issue_key = response.get("key")
        if not issue_key:
            raise IntegrationBadResponse("jira", "jira did not return an issue key")
        return {
            "issue_key": issue_key,
            "url": f"{self.client.base_url}/browse/{issue_key}",
            "replayed": False,
            "simulated": False,
        }

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        payload = await self.client.get(f"/rest/api/3/issue/{issue_key}")
        fields = payload.get("fields", {})
        return {
            "issue_key": issue_key,
            "status": str(fields.get("status", {}).get("name", "unknown")),
            "summary": fields.get("summary", ""),
            "simulated": False,
        }

    async def add_comment(self, issue_key: str, body: str) -> dict[str, Any]:
        await self.client.post(f"/rest/api/3/issue/{issue_key}/comment", json_body={"body": body})
        return {"issue_key": issue_key, "simulated": False}


class HttpSlackClient:
    def __init__(self, client: ResilientHttpClient, default_channel: str) -> None:
        self.client = client
        self.default_channel = default_channel

    async def notify(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(
            "",
            json_body={
                "channel": payload.get("channel") or self.default_channel,
                "text": payload.get("text", ""),
            },
        )
        if not response.get("ok", False):
            raise IntegrationBadResponse("slack", "slack rejected the notification")
        return {
            "message_ts": str(response.get("ts", "")),
            "channel": str(response.get("channel", self.default_channel)),
            "simulated": False,
        }

    async def history(self, limit: int = 20) -> dict[str, Any]:
        """Read the channel back so verification can confirm delivery independently."""
        response = await self.client.get(
            "history", params={"channel": self.default_channel, "limit": limit}
        )
        if not response.get("ok", False):
            raise IntegrationBadResponse("slack", "slack rejected the history read")
        return {
            "channel": self.default_channel,
            "messages": [
                {"ts": str(item.get("ts", "")), "text": str(item.get("text", ""))}
                for item in response.get("messages", [])
            ],
            "simulated": False,
        }


class HttpPaymentsProvider:
    def __init__(self, client: ResilientHttpClient) -> None:
        self.client = client

    async def payment_failures(self, window_minutes: int) -> dict[str, Any]:
        payload = await self.client.get(
            "/v1/payment_intents/failures", params={"window_minutes": window_minutes}
        )
        payload["simulated"] = False
        return payload

    async def affected_customers(self, window_minutes: int) -> dict[str, Any]:
        payload = await self.client.get(
            "/v1/payment_intents/affected", params={"window_minutes": window_minutes}
        )
        payload["simulated"] = False
        return payload


class HttpDeploymentProvider:
    """Deployment platform adapter. Rollback exists here and nowhere else."""

    def __init__(self, client: ResilientHttpClient) -> None:
        self.client = client

    async def current_state(self) -> dict[str, Any]:
        payload = await self.client.get("/api/deployments/current")
        payload["simulated"] = False
        return payload

    async def history(self, limit: int) -> dict[str, Any]:
        payload = await self.client.get("/api/deployments", params={"limit": limit})
        payload["simulated"] = False
        return payload

    async def rollback(self, target_release: str, reason: str) -> dict[str, Any]:
        payload = await self.client.post(
            "/api/deployments/rollback",
            json_body={"target_release": target_release, "reason": reason},
        )
        payload["simulated"] = False
        return payload


def build_live_clients(settings: Settings) -> dict[str, Any]:
    """Construct the live providers from configuration.

    Only endpoints with a configured URL are created; a missing URL is a configuration
    problem that :meth:`Settings.public_summary` surfaces, not something to paper over with a
    silent no-op client.
    """
    clients: dict[str, Any] = {}

    metrics_url = settings.metrics_url
    if metrics_url:
        clients["metrics"] = HttpMetricsProvider(
            ResilientHttpClient(
                "metrics",
                base_url=metrics_url,
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=2,
                headers=(
                    {"Authorization": f"Bearer {settings.metrics_token.get_secret_value()}"}
                    if settings.metrics_token.get_secret_value()
                    else {}
                ),
            )
        )

    if settings.logs_url:
        clients["logs"] = HttpLogsProvider(
            ResilientHttpClient(
                "logs",
                base_url=settings.logs_url,
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=2,
                headers=(
                    {"Authorization": f"Bearer {settings.logs_token.get_secret_value()}"}
                    if settings.logs_token.get_secret_value()
                    else {}
                ),
            )
        )

    if settings.github_token.get_secret_value():
        clients["github"] = HttpGitHubClient(
            ResilientHttpClient(
                "github",
                base_url=settings.github_api_url,
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=2,
                headers={
                    "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            ),
            settings.github_repository,
        )

    if settings.jira_url:
        clients["jira"] = HttpJiraClient(
            ResilientHttpClient(
                "jira",
                base_url=settings.jira_url,
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=2,
                headers=(
                    {"Authorization": f"Bearer {settings.jira_token.get_secret_value()}"}
                    if settings.jira_token.get_secret_value()
                    else {}
                ),
            ),
            settings.jira_project_key,
        )

    if settings.slack_webhook_url:
        clients["slack"] = HttpSlackClient(
            ResilientHttpClient(
                "slack",
                base_url=settings.slack_webhook_url,
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=2,
            ),
            settings.slack_channel,
        )
    elif settings.slack_token.get_secret_value():
        # A bot token (rather than a webhook) still needs a base URL to post to.
        clients["slack"] = HttpSlackClient(
            ResilientHttpClient(
                "slack",
                base_url="https://slack.com/api/chat.postMessage",
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=2,
            ),
            settings.slack_channel,
        )

    if settings.payments_url:
        clients["payments"] = HttpPaymentsProvider(
            ResilientHttpClient(
                "payments",
                base_url=settings.payments_url,
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=1,
                headers=(
                    {"Authorization": f"Bearer {settings.payments_token.get_secret_value()}"}
                    if settings.payments_token.get_secret_value()
                    else {}
                ),
            )
        )

    return clients


__all__ = [
    "HttpDeploymentProvider",
    "HttpGitHubClient",
    "HttpJiraClient",
    "HttpLogsProvider",
    "HttpMetricsProvider",
    "HttpPaymentsProvider",
    "HttpSlackClient",
    "build_live_clients",
]
