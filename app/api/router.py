"""API router aggregation.

The versioned surface lives under ``settings.api_prefix``; health, readiness and metrics stay
at the root because orchestrators, load balancers and Prometheus scrape fixed paths.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import admin, agents, approvals, audit, detection, health, incidents


def build_api_router() -> APIRouter:
    router = APIRouter()
    router.include_router(incidents.router)
    router.include_router(agents.router)
    router.include_router(approvals.router)
    router.include_router(detection.router)
    router.include_router(admin.router)
    router.include_router(audit.router)
    return router


def build_root_router() -> APIRouter:
    router = APIRouter()
    router.include_router(health.router)
    return router


__all__ = ["build_api_router", "build_root_router"]
