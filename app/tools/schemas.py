"""Parameter models for every registered tool.

Every tool validates its parameters against a strict pydantic model before doing anything.
``extra="forbid"`` matters here: it means a model (or a compromised upstream) cannot smuggle
additional fields past policy, because policy evaluates the *validated* payload.

These models are the concrete realisation of "the LLM cannot choose what it is allowed to
do": the shape of every possible call is fixed in code, and anything outside it is a
validation error, not an action.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolParams(BaseModel):
    """Strict base: no unknown fields, no coercion surprises."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class KnowledgeBaseExampleParams(ToolParams):
    """Deterministic reference lookup — proves the tool path without touching a real system."""

    topic: str = Field(min_length=1, max_length=120)


class GitHubReadCommitsParams(ToolParams):
    repository: str | None = Field(default=None, max_length=200)
    since_minutes: int = Field(default=60, ge=1, le=1440)
    limit: int = Field(default=10, ge=1, le=50)
    branch: str | None = Field(default=None, max_length=120)


class MetricsQueryParams(ToolParams):
    service: str = Field(min_length=1, max_length=120)
    metric: str = Field(min_length=1, max_length=64)
    window_minutes: int = Field(default=60, ge=1, le=1440)


class LogsQueryParams(ToolParams):
    service: str = Field(min_length=1, max_length=120)
    window_minutes: int = Field(default=30, ge=1, le=1440)
    severity: str | None = Field(default=None, max_length=16)
    contains: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


class JiraCreateIncidentParams(ToolParams):
    incident_id: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=250)
    description: str = Field(default="", max_length=8000)
    priority: str = Field(default="P2", pattern=r"^P[0-4]$")
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    labels: list[str] = Field(default_factory=list, max_length=10)


class SlackNotifyParams(ToolParams):
    incident_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=3000)
    severity: str = Field(default="SEV3", pattern=r"^SEV[1-4]$")
    channel: str | None = Field(default=None, max_length=80, pattern=r"^#[a-z0-9-_]+$")
    action_url: str | None = Field(default=None, max_length=500)
    evidence_count: int | None = Field(default=None, ge=0, le=100_000)


class DeploymentRollbackParams(ToolParams):
    """The only high-risk tool in the MVP. Everything about it is constrained."""

    incident_id: str = Field(min_length=1, max_length=64)
    target_release: str = Field(min_length=1, max_length=64, pattern=r"^release-[0-9]+$")
    reason: str = Field(min_length=3, max_length=1000)
    current_release: str | None = Field(default=None, max_length=64)

    @field_validator("target_release")
    @classmethod
    def _reject_placeholder_targets(cls, value: str) -> str:
        # "latest" is not a release: rolling back to an unversioned target is how a rollback
        # becomes an outage.
        if value in {"release-latest", "release-0"}:
            raise ValueError("target_release must be a concrete, previously released version")
        return value


class IncidentEscalateParams(ToolParams):
    incident_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=3, max_length=500)
    notify_channel: str | None = Field(default=None, max_length=80)


PARAMS_MODELS: dict[str, type[ToolParams]] = {
    "knowledge.base_example": KnowledgeBaseExampleParams,
    "github.read_commits": GitHubReadCommitsParams,
    "metrics.query": MetricsQueryParams,
    "logs.query": LogsQueryParams,
    "jira.create_incident": JiraCreateIncidentParams,
    "slack.notify": SlackNotifyParams,
    "deployment.rollback_simulation": DeploymentRollbackParams,
    "incident.escalate": IncidentEscalateParams,
}

__all__ = [
    "PARAMS_MODELS",
    "DeploymentRollbackParams",
    "GitHubReadCommitsParams",
    "IncidentEscalateParams",
    "JiraCreateIncidentParams",
    "KnowledgeBaseExampleParams",
    "LogsQueryParams",
    "MetricsQueryParams",
    "SlackNotifyParams",
    "ToolParams",
]
