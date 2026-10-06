"""Test support: run the real application in-process or on a socket.

Kept in one place so the integration, security and end-to-end suites all exercise the *same*
application object and the same tokens. Nothing here stubs the application: the ASGI transport
runs middleware, dependencies, authorisation and database transactions exactly as production
does, and the socket variant exists because the release scripts are HTTP clients.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from app.core.config import Settings
from app.main import create_app

#: Tokens the shared ``settings`` fixture configures (tests/conftest.py).
TOKENS: dict[str, str] = {
    "viewer": "viewer-token",
    "operator": "operator-token",
    "sre": "sre-token",
    "admin": "admin-token",
}


def auth(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[role]}"}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@asynccontextmanager
async def running_app(settings: Settings) -> AsyncIterator[SimpleNamespace]:
    """Start the application's real lifespan and expose an ASGI client against it."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=auth("sre"),
            timeout=60.0,
        ) as client:
            yield SimpleNamespace(app=app, client=client, settings=settings, auth=auth)


def start_server(settings: Settings) -> SimpleNamespace:
    """Start uvicorn on a free port and wait until ``/health`` answers."""
    port = free_port()
    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="test-server", daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if not thread.is_alive():
            break
        try:
            with httpx.Client(timeout=2.0) as probe:
                if probe.get(f"{base_url}/health").status_code == 200:
                    return SimpleNamespace(
                        base_url=base_url, port=port, server=server, thread=thread
                    )
        except httpx.HTTPError:
            time.sleep(0.1)

    server.should_exit = True
    thread.join(timeout=10)
    pytest.fail(f"test server did not become healthy at {base_url}")


def stop_server(handle: SimpleNamespace) -> None:
    """Ask uvicorn to exit and wait for its thread, so no request outlives the test."""
    handle.server.should_exit = True
    handle.thread.join(timeout=15)
    assert not handle.thread.is_alive(), "the test server did not shut down"


@contextlib.contextmanager
def live_server(settings: Settings) -> Iterator[SimpleNamespace]:
    handle = start_server(settings)
    try:
        yield handle
    finally:
        stop_server(handle)


__all__ = [
    "TOKENS",
    "auth",
    "free_port",
    "live_server",
    "running_app",
    "start_server",
    "stop_server",
]
