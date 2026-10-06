"""Metrics provider.

Stable import path declared by backlog story OPS-023. The implementation lives in
``app/integrations/live.py`` (Prometheus query_range) and ``app/integrations/sandbox_providers.py``
— this module exists so the documented layout and the code cannot drift apart, and so
callers import the system name rather than a transport detail.
"""

from __future__ import annotations

from app.integrations.live import HttpMetricsProvider
from app.integrations.sandbox_providers import SandboxMetricsProvider

__all__ = ["HttpMetricsProvider", "SandboxMetricsProvider"]
