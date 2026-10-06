"""Integration facade — one object that the rest of the application talks to.

Why a facade rather than injecting six clients everywhere:

* **one place decides sandbox vs live** — the mode is read once and every provider follows it;
* **the tool-facing surface is narrow and explicit** — it implements exactly the
  :class:`app.tools.registry.Integrations` protocol, so a tool physically cannot reach a
  method that is not on it;
* **health is aggregated** — ``/ready`` and the ``integrations/health`` endpoint answer from
  the same object the agent uses, so a green dashboard means the agent's own access is green.
"""

from __future__ import annotations

from typing import Any

from app.core.config import IntegrationsMode, Settings
from app.core.errors import IntegrationUnavailable
from app.core.logging import get_logger
from app.integrations.live import build_live_clients
from app.integrations.sandbox_providers import (
    SandboxDeploymentProvider,
    SandboxGitHubProvider,
    SandboxJiraProvider,
    SandboxLogsProvider,
    SandboxMetricsProvider,
    SandboxPaymentsProvider,
    SandboxSlackProvider,
)
from app.sandbox.simulator import SERVICE, SandboxState, get_sandbox

logger = get_logger(__name__)


class _UnavailableProvider:
    """Stand-in for an unconfigured live provider.

    It raises a typed error rather than returning an empty result, because "no data" and
    "we could not ask" must never be confused — a distinction the investigation layer relies
    on to record honest degradations.
    """

    def __init__(self, system: str) -> None:
        self.system = system

    def _fail(self) -> None:
        raise IntegrationUnavailable(
            self.system,
            f"Integration '{self.system}' is not configured (AIOPS_INTEGRATIONS_MODE=live).",
        )

    async def query_metric(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def list_metrics(self, *_: Any, **__: Any) -> list[str]:
        self._fail()

    async def query_logs(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def list_commits(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def list_deployments(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def create_issue(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def get_issue(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def add_comment(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def notify(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def payment_failures(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def affected_customers(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def current_state(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def history(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()

    async def rollback(self, *_: Any, **__: Any) -> dict[str, Any]:
        self._fail()


class IntegrationFacade:
    """Aggregate access to every external system, in sandbox or live mode."""

    def __init__(
        self,
        settings: Settings,
        *,
        sandbox: SandboxState | None = None,
        service: str = SERVICE,
    ) -> None:
        self.settings = settings
        self.mode: IntegrationsMode = settings.integrations_mode
        self.service = service
        self.sandbox = sandbox or get_sandbox()
        self._live: dict[str, Any] = {}

        if self.mode is IntegrationsMode.SANDBOX:
            self.metrics: Any = SandboxMetricsProvider(self.sandbox, service)
            self.logs: Any = SandboxLogsProvider(self.sandbox, service)
            self.github: Any = SandboxGitHubProvider(self.sandbox, settings.github_repository)
            self.jira: Any = SandboxJiraProvider(self.sandbox, settings.jira_project_key)
            self.slack: Any = SandboxSlackProvider(self.sandbox, settings.slack_channel)
            self.payments: Any = SandboxPaymentsProvider(self.sandbox)
            self.deployment: Any = SandboxDeploymentProvider(self.sandbox)
        else:
            self._live = build_live_clients(settings)
            self.metrics = self._live.get("metrics", _UnavailableProvider("metrics"))
            self.logs = self._live.get("logs", _UnavailableProvider("logs"))
            self.github = self._live.get("github", _UnavailableProvider("github"))
            self.jira = self._live.get("jira", _UnavailableProvider("jira"))
            self.slack = self._live.get("slack", _UnavailableProvider("slack"))
            self.payments = self._live.get("payments", _UnavailableProvider("payments"))
            self.deployment = _UnavailableProvider("deployment")
            missing = sorted(
                system
                for system in ("metrics", "logs", "github", "jira", "slack", "payments")
                if system not in self._live
            )
            if missing:
                logger.warning("integrations_not_configured", missing=missing, mode=self.mode.value)

    # ------------------------------------------------------------------ #
    # Tool-facing surface (implements app.tools.registry.Integrations)
    # ------------------------------------------------------------------ #

    async def metrics_window(
        self, service: str, metric: str, window_minutes: int
    ) -> dict[str, Any]:
        return await self.metrics.query_metric(service, metric, window_minutes)

    async def logs_window(
        self,
        service: str,
        window_minutes: int,
        severity: str | None,
        contains: str | None,
        limit: int,
    ) -> dict[str, Any]:
        return await self.logs.query_logs(
            service=service,
            window_minutes=window_minutes,
            severity=severity,
            contains=contains,
            limit=limit,
        )

    async def github_commits(
        self, repository: str | None, since_minutes: int, limit: int, branch: str | None
    ) -> dict[str, Any]:
        return await self.github.list_commits(repository, since_minutes, limit, branch)

    async def jira_create_issue(
        self, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        return await self.jira.create_issue(payload, idempotency_key)

    async def slack_notify(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.slack.notify(payload)

    async def deployment_state(self) -> dict[str, Any]:
        return await self.deployment.current_state()

    async def deployment_history(self, limit: int) -> dict[str, Any]:
        return await self.deployment.history(limit)

    async def deployment_rollback(self, target_release: str, reason: str) -> dict[str, Any]:
        return await self.deployment.rollback(target_release, reason)

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #

    async def health(self) -> dict[str, dict[str, Any]]:
        """Probe each system with a cheap call; never raise, always report.

        ``/ready`` must be able to answer truthfully while dependencies are down (threat T-31),
        so this method returns status instead of raising.
        """
        from time import perf_counter

        checks: dict[str, tuple[str, Any]] = {
            "metrics": ("query_metric", (self.service, "error_rate", 5)),
            "logs": ("query_logs", ()),
            "github": ("list_commits", (None, 15, 1, None)),
            "deployment": ("current_state", ()),
        }
        report: dict[str, dict[str, Any]] = {}
        for system, (method_name, args) in checks.items():
            provider = getattr(self, system)
            method = getattr(provider, method_name)
            started = perf_counter()
            try:
                if system == "logs":
                    await method(service=self.service, window_minutes=5)
                else:
                    await method(*args)
                report[system] = {
                    "status": "ok",
                    "mode": self.mode.value,
                    "latency_ms": round((perf_counter() - started) * 1000, 2),
                }
            except Exception as exc:
                report[system] = {
                    "status": "unavailable",
                    "mode": self.mode.value,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:200],
                }

        report["jira"] = {
            "status": "ok" if self.mode is IntegrationsMode.SANDBOX else "configured",
            "mode": self.mode.value,
            "detail": "write path is exercised by actions, not by readiness probes",
        }
        report["slack"] = {
            "status": "ok" if self.mode is IntegrationsMode.SANDBOX else "configured",
            "mode": self.mode.value,
        }
        report["payments"] = {
            "status": "ok" if self.mode is IntegrationsMode.SANDBOX else "configured",
            "mode": self.mode.value,
        }
        return report

    async def aclose(self) -> None:
        for client in self._live.values():
            closer = getattr(client, "aclose", None)
            if closer is not None:
                await closer()


def build_integrations(settings: Settings) -> IntegrationFacade:
    return IntegrationFacade(settings)


__all__ = ["IntegrationFacade", "build_integrations"]
