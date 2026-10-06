"""External system adapters.

``integrations`` knows *how to talk* to a system; ``tools`` knows *what the agent may do*
with it. Keeping those apart is what makes least privilege reviewable.
"""

from app.integrations.facade import IntegrationFacade, build_integrations
from app.integrations.protocols import (
    DeploymentProvider,
    GitHubProvider,
    JiraProvider,
    LogsProvider,
    MetricsProvider,
    PaymentsProvider,
    SlackProvider,
)

__all__ = [
    "DeploymentProvider",
    "GitHubProvider",
    "IntegrationFacade",
    "JiraProvider",
    "LogsProvider",
    "MetricsProvider",
    "PaymentsProvider",
    "SlackProvider",
    "build_integrations",
]
