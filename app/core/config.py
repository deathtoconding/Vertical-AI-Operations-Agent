"""Configuration — environment-driven, validated at boot, fails closed.

OPS-010 acceptance criteria: configuration is environment-driven and no secret lives in
source (rule 6). Everything is read through :class:`Settings`, so a missing or nonsensical
value stops the process at startup instead of at 3am.

Production hardening: boot validation refuses to start with sandbox integrations, an empty
token set, or a missing proxy-trust declaration (T-15, T-31 in the threat model).
"""

from __future__ import annotations

import hashlib
import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.enums import AutonomyLevel, Role

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)


class IntegrationsMode(StrEnum):
    """``sandbox`` uses the labelled simulator (ADR-0007); ``live`` calls real systems."""

    SANDBOX = "sandbox"
    LIVE = "live"


class TokenBinding(BaseSettings):
    """A single API token bound to exactly one role and actor id."""

    model_config = SettingsConfigDict(extra="forbid")

    token: SecretStr
    role: Role
    actor_id: str

    @property
    def token_hash(self) -> str:
        return hashlib.sha256(self.token.get_secret_value().encode()).hexdigest()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIOPS_",
        env_file=os.environ.get("AIOPS_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- core --------------------------------------------------------------- #
    env: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    log_json: bool | None = None
    api_prefix: str = "/api/v1"
    request_timeout_seconds: float = 10.0
    service_name: str = "aiops-agent"
    version: str = "1.0.0"
    trust_proxy: bool = False
    cors_origins: str = ""

    # -- database ----------------------------------------------------------- #
    database_url: str = "postgresql+psycopg://aiops:aiops@localhost:5432/aiops"
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_timeout: float = 30.0
    db_echo: bool = False

    # -- auth --------------------------------------------------------------- #
    api_tokens: str = ""
    default_actor_role: Role = Role.SRE

    # -- integrations ------------------------------------------------------- #
    integrations_mode: IntegrationsMode = IntegrationsMode.SANDBOX
    github_api_url: str = "https://api.github.com"
    github_token: SecretStr = SecretStr("")
    default_service: str = "checkout-service"
    github_repository: str = "acme/checkout-service"
    jira_url: str = ""
    jira_token: SecretStr = SecretStr("")
    jira_email: str = ""
    jira_project_key: str = "OPS"
    slack_webhook_url: str = ""
    slack_token: SecretStr = SecretStr("")
    slack_channel: str = "#ops-incidents"
    metrics_url: str = ""
    metrics_token: SecretStr = SecretStr("")
    logs_url: str = ""
    logs_token: SecretStr = SecretStr("")
    payments_url: str = ""
    payments_token: SecretStr = SecretStr("")

    # -- llm ---------------------------------------------------------------- #
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.0
    llm_max_prompt_chars: int = 24000

    # -- detection ---------------------------------------------------------- #
    detection_window_minutes: int = 60
    detection_min_samples: int = 12
    detection_z_threshold: float = 4.0
    detection_min_relative_deviation: float = 0.5
    detection_dedup_window_minutes: int = 30

    # -- investigation / evidence ------------------------------------------- #
    evidence_max_items_per_source: int = 20
    evidence_concurrency: int = 6
    evidence_max_chars: int = 4000

    # -- policy / autonomy -------------------------------------------------- #
    autonomy_level: AutonomyLevel = AutonomyLevel.APPROVAL_REQUIRED
    disabled_tools: str = ""
    approval_timeout_seconds: float = 900.0
    max_rollbacks_per_hour: int = 2

    # -- actions ------------------------------------------------------------ #
    action_timeout_seconds: float = 30.0
    action_max_attempts: int = 3
    action_retry_backoff_seconds: float = 0.5

    # -- verification ------------------------------------------------------- #
    verification_window_seconds: float = 30.0
    verification_min_samples: int = 3
    verification_poll_interval_seconds: float = 1.0

    # -- rate limiting ------------------------------------------------------ #
    rate_limit_requests_per_minute: int = 600
    rate_limit_burst: int = 60

    # -- tracing ------------------------------------------------------------ #
    tracing_enabled: bool = True
    tracing_exporter: str = "none"  # none | console | otlp

    # -- run recovery ------------------------------------------------------- #
    run_lease_seconds: float = 120.0

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"AIOPS_LOG_LEVEL must be one of {sorted(allowed)}")
        return upper

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value:
            raise ValueError("AIOPS_DATABASE_URL is required")
        if value.startswith("sqlite"):
            raise ValueError(
                "SQLite is not a supported system of record (ADR-0005); "
                "use a postgresql+psycopg:// URL"
            )
        if "+psycopg" not in value and not value.startswith("postgresql://"):
            raise ValueError("AIOPS_DATABASE_URL must use the psycopg driver")
        return value

    @field_validator("api_prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("AIOPS_API_PREFIX must start with '/'")
        return value.rstrip("/") or "/api/v1"

    @model_validator(mode="after")
    def _validate_environment_consistency(self) -> Settings:
        if self.log_json is None:
            object.__setattr__(self, "log_json", self.env.is_production_like)

        if self.env.is_production_like:
            if self.integrations_mode is IntegrationsMode.SANDBOX:
                raise ValueError(
                    "AIOPS_INTEGRATIONS_MODE=sandbox is refused in staging/production: "
                    "a production agent must not act on simulated systems (ADR-0007)"
                )
            if not self.tokens:
                raise ValueError(
                    "AIOPS_API_TOKENS must be configured outside development: "
                    "an unauthenticated agent is not deployable (SEC-002)"
                )
            if not self.trust_proxy:
                raise ValueError(
                    "AIOPS_TRUST_PROXY must be true in staging/production so client IPs and "
                    "TLS termination are explicit rather than assumed (threat T-18)"
                )
        return self

    # ------------------------------------------------------------------ #
    # Derived accessors
    # ------------------------------------------------------------------ #

    @property
    def tokens(self) -> dict[str, TokenBinding]:
        """Map of token hash -> binding. Parsed from ``token:role:actor`` triples."""
        bindings: dict[str, TokenBinding] = {}
        for entry in self.api_tokens.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) != 3:
                raise ValueError(
                    "AIOPS_API_TOKENS entries must be 'token:role:actor_id' "
                    f"(got {len(parts)} fields)"
                )
            token, role, actor = (part.strip() for part in parts)
            if not token or not actor:
                raise ValueError("AIOPS_API_TOKENS entries must not have empty token/actor")
            binding = TokenBinding(token=SecretStr(token), role=Role(role), actor_id=actor)
            bindings[binding.token_hash] = binding
        return bindings

    @property
    def dev_tokens(self) -> dict[str, str]:
        """Development convenience: role -> plaintext token (never logged)."""
        return {b.role.value: b.token.get_secret_value() for b in self.tokens.values()}

    @property
    def disabled_tool_names(self) -> frozenset[str]:
        entries = {item.strip() for item in self.disabled_tools.split(",") if item.strip()}
        return frozenset(entries)

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def llm_configured(self) -> bool:
        """True when a real LLM can be called; otherwise the deterministic reasoner runs."""
        return bool(self.llm_api_key.get_secret_value())

    def public_summary(self) -> dict[str, object]:
        """Safe-to-expose configuration summary for ``/version`` — no secrets."""
        return {
            "env": self.env.value,
            "version": self.version,
            "integrations_mode": self.integrations_mode.value,
            "autonomy_level": self.autonomy_level.value,
            "llm_enabled": self.llm_configured,
            "llm_model": self.llm_model if self.llm_configured else None,
            "tracing_enabled": self.tracing_enabled,
            "disabled_tools": sorted(self.disabled_tool_names),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor (FastAPI dependency friendly)."""
    return Settings()


def override_settings(**overrides: object) -> Settings:
    """Build settings for tests without touching the process environment."""
    return Settings(**overrides)  # type: ignore[arg-type]
