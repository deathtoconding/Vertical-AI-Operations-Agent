"""FastAPI application entry point (``uvicorn app.main:app``).

Startup order is deliberate:

1. configure logging and tracing (so everything after it is observable);
2. build the container (database engine, integrations, tools, policy);
3. **verify the metric contract** — a missing or renamed SLI is a release blocker, not a
   dashboard surprise for whoever is on call tonight;
4. recover interrupted runs (idempotently, and never re-executing a high-risk action);
5. expose the API and the operator UI.

A degraded dependency never prevents startup: an agent that refuses to boot when Slack is
down cannot investigate the incident that Slack would have told someone about. It reports
degraded instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.errors import install_error_handlers
from app.api.middleware import CorrelationMiddleware, RateLimitMiddleware
from app.api.router import build_api_router, build_root_router
from app.api.routes import ui as ui_routes
from app.application.container import build_container, system_actor
from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationError
from app.core.logging import configure_logging, get_logger
from app.core.telemetry import assert_metric_contract
from app.core.tracing import configure_tracing

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(
        settings.log_level,
        settings.log_json if settings.log_json is not None else settings.env.value != "development",
    )
    configure_tracing(
        enabled=settings.tracing_enabled,
        exporter=settings.tracing_exporter,
        service_name=settings.service_name,
    )

    container = build_container(settings, database=getattr(app.state, "database", None))
    app.state.container = container

    try:
        assert_metric_contract()
    except AssertionError as exc:  # pragma: no cover - guarded by tests, not reachable in CI
        raise ConfigurationError(f"Metric contract violated: {exc}") from exc

    try:
        async with container.transaction() as services:
            from app.agent.recovery import recover_interrupted_runs

            report = await recover_interrupted_runs(
                container, services, actor=system_actor(settings)
            )
            if report["scanned"]:
                logger.warning("runs_recovered_on_startup", **report)
    except Exception as exc:
        logger.warning("startup_recovery_skipped", error=type(exc).__name__, detail=str(exc)[:200])

    logger.info(
        "application_started",
        version=settings.version,
        environment=settings.env.value,
        integrations_mode=settings.integrations_mode.value,
        llm_configured=settings.llm_configured,
    )
    try:
        yield
    finally:
        await container.aclose()
        logger.info("application_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Vertical AI Operations Agent",
        version=settings.version,
        description=(
            "An operations agent for a SaaS service. The model proposes; deterministic code "
            "authorises, executes, verifies and records. Tools are registered and bounded; "
            "high-risk actions require human approval; every consequential step is audited."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.env.value != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.env.value != "production" else None,
    )
    app.state.settings = settings

    app.add_middleware(CorrelationMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        limit_per_minute=settings.rate_limit_requests_per_minute,
        burst=settings.rate_limit_burst,
    )
    origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
        )

    install_error_handlers(app)
    app.include_router(build_root_router())
    app.include_router(build_api_router(), prefix=settings.api_prefix)

    app.include_router(ui_routes.router)
    if ui_routes.UI_ROOT.exists():
        # Mounted last so `/ui/...` serves assets while the API keeps its own paths.
        app.mount("/ui", StaticFiles(directory=str(ui_routes.UI_ROOT), html=True), name="ui")

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "script-src 'self'; connect-src 'self'",
        )
        return response

    return app


app = create_app()


__all__ = ["app", "create_app"]
