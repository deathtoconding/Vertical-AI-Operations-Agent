"""Slack client.

Stable import path declared by backlog story OPS-022. The implementation lives in
``app/integrations/live.py`` and ``app/integrations/sandbox_providers.py`` — this module exists so
the documented layout and the code cannot drift apart, and so
callers import the system name rather than a transport detail.
"""

from __future__ import annotations

from app.integrations.live import HttpSlackClient
from app.integrations.sandbox_providers import SandboxSlackProvider

__all__ = ["HttpSlackClient", "SandboxSlackProvider"]
