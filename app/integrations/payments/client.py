"""Payments provider clients — stable import path (OPS-023, scenario C).

The implementation lives in :mod:`app.integrations.live` (HTTP) and
:mod:`app.integrations.sandbox_providers` (simulator). Both satisfy the ``PaymentsProvider``
protocol in :mod:`app.integrations.protocols`, so the investigation collector does not know
which one it is talking to.
"""

from __future__ import annotations

from app.integrations.live import HttpPaymentsProvider
from app.integrations.sandbox_providers import SandboxPaymentsProvider

__all__ = ["HttpPaymentsProvider", "SandboxPaymentsProvider"]
