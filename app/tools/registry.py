"""Tool registry — the single door to every side effect (OPS-051).

The registry is where "the LLM cannot do more than we allow" stops being a design
aspiration and becomes an executable fact:

* a tool that is not registered cannot be executed **at all** — the lookup fails, an audit
  record is written and ``unknown_tool`` is counted;
* every tool declares its `permission`, `risk`, `requires_approval`, `timeout` and
  `max_retries`, so policy is a lookup rather than a judgement call;
* parameters are validated against a strict schema *before* anything else happens.

Tool handlers receive a :class:`ToolContext` and return a :class:`ToolResult`. A handler
must never raise for an expected external failure: it returns ``success=False`` with a typed
reason so the run can escalate with the real cause.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import ToolNotFound, ValidationFailed
from app.core.logging import get_logger
from app.core.telemetry import TOOL_INVOCATIONS
from app.domain.enums import RiskLevel, ToolOutcome
from app.domain.tools import ToolResult, ToolSpec
from app.tools import permissions as tool_permissions

logger = get_logger(__name__)


class Integrations(Protocol):
    """The integration surface a tool may use — deliberately narrow."""

    async def metrics_window(
        self, service: str, metric: str, window_minutes: int
    ) -> dict[str, Any]: ...
    async def logs_window(
        self,
        service: str,
        window_minutes: int,
        severity: str | None,
        contains: str | None,
        limit: int,
    ) -> dict[str, Any]: ...
    async def github_commits(
        self, repository: str | None, since_minutes: int, limit: int, branch: str | None
    ) -> dict[str, Any]: ...
    async def jira_create_issue(
        self, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...
    async def slack_notify(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def deployment_state(self) -> dict[str, Any]: ...
    async def deployment_history(self, limit: int) -> dict[str, Any]: ...
    async def deployment_rollback(self, target_release: str, reason: str) -> dict[str, Any]: ...


@dataclass
class ToolContext:
    """Everything a tool may touch. Note what is absent: no shell, no SQL, no filesystem."""

    settings: Settings
    integrations: Integrations
    actor: str = "system"
    incident_id: str | None = None
    run_id: str | None = None
    idempotency: Any | None = None
    extras: dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[BaseModel, ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolDefinition:
    spec: ToolSpec
    params_model: type[BaseModel]
    handler: ToolHandler

    async def invoke(self, raw_params: dict[str, Any], context: ToolContext) -> ToolResult:
        """Validate, then execute. Validation failures never reach the handler."""
        try:
            params = self.params_model.model_validate(raw_params)
        except ValidationError as exc:
            TOOL_INVOCATIONS.labels(
                tool=self.spec.name,
                risk=self.spec.risk.value,
                outcome=ToolOutcome.INVALID_INPUT.value,
            ).inc()
            raise ValidationFailed(
                f"Invalid parameters for tool '{self.spec.name}'.",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        return await self.handler(params, context)


class ToolRegistry:
    """Name -> definition map. Deny by default: absent means forbidden."""

    def __init__(self, definitions: list[ToolDefinition] | None = None) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        name = definition.spec.name
        if name in self._definitions:
            raise ValueError(f"tool '{name}' is already registered")
        if definition.spec.permission not in tool_permissions.PERMISSION_NAMES:
            raise ValueError(
                f"tool '{name}' declares unknown permission '{definition.spec.permission}'"
            )
        self._definitions[name] = definition

    def get(self, name: str) -> ToolDefinition:
        definition = self._definitions.get(name)
        if definition is None:
            TOOL_INVOCATIONS.labels(
                tool=name[:64], risk=RiskLevel.LOW.value, outcome=ToolOutcome.UNKNOWN_TOOL.value
            ).inc()
            raise ToolNotFound(f"Tool '{name}' is not registered.", details={"tool": name})
        return definition

    def find(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def names(self) -> list[str]:
        return sorted(self._definitions)

    def specs(self) -> list[ToolSpec]:
        return [self._definitions[name].spec for name in self.names()]

    def describe(self) -> list[dict[str, Any]]:
        """Registry introspection for the API — the audit answer to 'what can it do?'."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "permission": spec.permission,
                "risk": spec.risk.value,
                "requires_approval": spec.requires_approval,
                "timeout_seconds": spec.timeout_seconds,
                "max_retries": spec.max_retries,
                "idempotent": spec.idempotent,
                "input_schema": spec.input_schema,
            }
            for spec in self.specs()
        ]

    def disabled(self, disabled_names: frozenset[str]) -> list[str]:
        return [name for name in self.names() if name in disabled_names]

    def __len__(self) -> int:
        return len(self._definitions)


def build_default_registry() -> ToolRegistry:
    """Assemble the MVP registry. Import is local to avoid a cycle with the handlers."""
    from app.tools.deployment import rollback_tool
    from app.tools.github import read_commits_tool
    from app.tools.incident import escalate_tool
    from app.tools.jira import create_incident_tool
    from app.tools.knowledge import knowledge_example_tool
    from app.tools.logs import logs_query_tool
    from app.tools.metrics import metrics_query_tool
    from app.tools.slack import notify_tool

    return ToolRegistry(
        [
            knowledge_example_tool(),
            read_commits_tool(),
            metrics_query_tool(),
            logs_query_tool(),
            create_incident_tool(),
            notify_tool(),
            rollback_tool(),
            escalate_tool(),
        ]
    )


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """Process-wide registry. Built once; registration is static by design."""
    global _registry
    if _registry is None:
        _registry = build_default_registry()
    return _registry


def set_registry(registry: ToolRegistry | None) -> None:
    global _registry
    _registry = registry


__all__ = [
    "Integrations",
    "ToolContext",
    "ToolDefinition",
    "ToolHandler",
    "ToolRegistry",
    "build_default_registry",
    "get_registry",
    "set_registry",
]
