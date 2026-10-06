"""Log store provider.

Stable import path declared by backlog story OPS-023. The implementation lives in
``app/integrations/live.py`` (Loki-compatible ``query_range``) and
``app/integrations/sandbox_providers.py`` — this module exists so the documented layout and the
code cannot drift apart, and so callers import the system name rather than a transport detail.

Note for whoever edits ``.gitignore``: the runtime artefact path is ``logs/`` at the repository
root. The pattern must stay anchored, because an unanchored ``logs/`` ignores *this package* —
which is how the module can exist in a working tree and be absent from every clone and container
build. ``tests/unit/planning/test_repo_hygiene.py`` and the CI hygiene script guard against it.
"""

from __future__ import annotations

from app.integrations.live import HttpLogsProvider
from app.integrations.sandbox_providers import SandboxLogsProvider

__all__ = ["HttpLogsProvider", "SandboxLogsProvider"]
