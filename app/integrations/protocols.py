"""Provider interfaces for every external system.

These protocols exist so that the *sandbox* and the *live* clients are interchangeable
without the calling code knowing which one it has. That is what makes it possible to test the
entire lifecycle without inventing behaviour: the sandbox implements the same contract, and
everything above it is production code.

Each provider returns plain dictionaries that are already shaped for evidence. A provider
never returns an empty success on failure — it raises a typed :class:`IntegrationError`
(``tests/integration/test_error_contract.py`` asserts this for every implementation).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MetricsProvider(Protocol):
    async def query_metric(
        self, service: str, metric: str, window_minutes: int
    ) -> dict[str, Any]: ...

    async def list_metrics(self, service: str) -> list[str]: ...


@runtime_checkable
class LogsProvider(Protocol):
    async def query_logs(
        self,
        service: str,
        window_minutes: int,
        severity: str | None = None,
        contains: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]: ...


@runtime_checkable
class GitHubProvider(Protocol):
    async def list_commits(
        self, repository: str | None, since_minutes: int, limit: int, branch: str | None
    ) -> dict[str, Any]: ...

    async def list_deployments(self, repository: str | None, limit: int) -> dict[str, Any]: ...


@runtime_checkable
class JiraProvider(Protocol):
    async def create_issue(
        self, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    async def get_issue(self, issue_key: str) -> dict[str, Any]: ...

    async def add_comment(self, issue_key: str, body: str) -> dict[str, Any]: ...


@runtime_checkable
class SlackProvider(Protocol):
    async def notify(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class PaymentsProvider(Protocol):
    async def payment_failures(self, window_minutes: int) -> dict[str, Any]: ...

    async def affected_customers(self, window_minutes: int) -> dict[str, Any]: ...


@runtime_checkable
class DeploymentProvider(Protocol):
    async def current_state(self) -> dict[str, Any]: ...

    async def history(self, limit: int) -> dict[str, Any]: ...

    async def rollback(self, target_release: str, reason: str) -> dict[str, Any]: ...


@runtime_checkable
class IntegrationHealth(Protocol):
    async def health(self) -> dict[str, dict[str, Any]]: ...


__all__ = [
    "DeploymentProvider",
    "GitHubProvider",
    "IntegrationHealth",
    "JiraProvider",
    "LogsProvider",
    "MetricsProvider",
    "PaymentsProvider",
    "SlackProvider",
]
