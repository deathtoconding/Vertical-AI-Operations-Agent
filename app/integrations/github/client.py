"""GitHub client.

Stable import path declared by backlog story OPS-020. The implementation lives in
``app/integrations/live.py`` (HTTP) and ``app/integrations/sandbox_providers.py`` (sandbox) — this
module exists so the documented layout and the code cannot drift apart, and so
callers import the system name rather than a transport detail.
"""

from __future__ import annotations

from app.integrations.live import HttpGitHubClient
from app.integrations.sandbox_providers import SandboxGitHubProvider

__all__ = ["HttpGitHubClient", "SandboxGitHubProvider"]
