"""Jira client.

Stable import path declared by backlog story OPS-021. The implementation lives in
``app/integrations/live.py`` and ``app/integrations/sandbox_providers.py`` — this module exists so
the documented layout and the code cannot drift apart, and so
callers import the system name rather than a transport detail.
"""

from __future__ import annotations

from app.integrations.live import HttpJiraClient
from app.integrations.sandbox_providers import SandboxJiraProvider

__all__ = ["HttpJiraClient", "SandboxJiraProvider"]
