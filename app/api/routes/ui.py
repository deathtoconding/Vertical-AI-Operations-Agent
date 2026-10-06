"""Operator console serving (UI-001..003).

The console is served from the same origin as the API, from files in the repository — no CDN,
no bundler, no build step. During an incident, a dashboard that depends on the public internet
or on a successful frontend build is a dashboard that is unavailable exactly when it is needed.

Security posture:

* it is a **view**: every action it triggers goes through the same authenticated, authorised,
  policy-checked API endpoints as any other client (it gets no private path);
* it holds no credentials — the browser sends the operator's own bearer token;
* a strict Content-Security-Policy (set in ``app.main``) forbids inline and remote script.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

UI_ROOT = Path(__file__).resolve().parents[2] / "ui" / "static"

router = APIRouter(tags=["ui"])


@router.get("/", include_in_schema=False)
async def console() -> Any:
    index = UI_ROOT / "index.html"
    if not index.exists():  # pragma: no cover - defensive
        return JSONResponse({"detail": "UI not installed"}, status_code=404)
    return FileResponse(index)


__all__ = ["UI_ROOT", "router"]
