"""Integration-suite notes.

The application fixtures live in ``tests/conftest.py`` so the integration, security and
end-to-end suites all exercise the same application object (and the same absence of stubs).
This module only guards that assumption for this directory.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests.support.app import auth


async def test_api_is_the_real_application(api: SimpleNamespace) -> None:
    """A guard against the shared fixture quietly becoming a stub."""
    assert api.app.state.container is not None
    response = await api.client.get("/api/v1/admin/tools")
    assert response.status_code == 200
    assert response.json()["count"] >= 5


def test_auth_roles_are_usable() -> None:
    """The matrix tests need every role's token to be a well-formed header."""
    for role in ("viewer", "operator", "sre", "admin"):
        assert auth(role)["Authorization"].startswith("Bearer ")
