"""The policy engine: deterministic, LLM-free, and the only source of authority.

Contract (ADR-0002):

* input: a concrete action, an authenticated actor, the tool's declared metadata, the incident, the
configured autonomy level and a little bounded context;
* output: exactly one of ``ALLOW`` / ``DENY`` / ``REQUIRE_APPROVAL`` with the rule name that
produced it;
* the engine never imports the LLM module, and a test asserts that. A model cannot argue its way
past a function that does not read model output.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.errors import PolicyDenied, ToolNotFound
from app.core.logging import get_logger
from app.core.security import Actor
from app.domain.enums import PolicyDecisionType
from app.domain.incidents import Incident
from app.domain.policy import PolicyDecision, RiskAssessment
from app.domain.tools import ToolSpec
from app.policy import risk as risk_module
from app.policy.rules import RULES
from app.tools.registry import ToolRegistry

logger = get_logger(__name__)


class PolicyEngine:
    """Evaluate actions against declared metadata and configured autonomy."""

    def __init__(self, registry: ToolRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings

    def assess_risk(
        self,
        spec: ToolSpec,
        *,
        incident: Incident | None = None,
        params: dict[str, Any] | None = None,
        prior_rollbacks: int = 0,
    ) -> RiskAssessment:
        return risk_module.classify(
            spec,
            incident=incident,
            context={"params": params or {}, "prior_rollbacks": prior_rollbacks},
        )

    def evaluate(
        self,
        tool_name: str,
        *,
        actor: Actor,
        params: dict[str, Any] | None = None,
        incident: Incident | None = None,
        autonomy: Any = None,
        prior_rollbacks: int = 0,
        disabled_tools: frozenset[str] | None = None,
    ) -> PolicyDecision:
        """Return the decision for one action attempt."""
        try:
            definition = self.registry.get(tool_name)
        except ToolNotFound:
            return PolicyDecision(
                decision=PolicyDecisionType.DENY,
                reason=f"Tool '{tool_name}' is not registered, so it cannot be authorised.",
                tool_name=tool_name,
                risk=__import__("app.domain.enums", fromlist=["RiskLevel"]).RiskLevel.CRITICAL,
                permission="none",
                actor=actor.actor_id,
                role=actor.role.value,
                violations=["unknown_tool"],
            )

        spec = definition.spec
        autonomy_level = autonomy or self.settings.autonomy_level
        disabled = (
            disabled_tools if disabled_tools is not None else self.settings.disabled_tool_names
        )

        assessment = self.assess_risk(
            spec, incident=incident, params=params, prior_rollbacks=prior_rollbacks
        )

        decision = PolicyDecision(
            decision=PolicyDecisionType.ALLOW,
            reason="Allowed: no rule objected.",
            tool_name=spec.name,
            risk=assessment.effective_risk,
            permission=spec.permission,
            actor=actor.actor_id,
            role=actor.role.value,
            requires_approval=spec.requires_approval,
            autonomy_level=autonomy_level.value,
            violations=list(assessment.escalators),
        )

        for rule in RULES:
            outcome = rule(
                spec,
                actor=actor,
                incident=incident,
                params=params or {},
                autonomy=autonomy_level,
                effective_risk=assessment.effective_risk,
                prior_rollbacks=prior_rollbacks,
                settings=self.settings,
                disabled=disabled,
            )
            if outcome is None:
                continue
            decision = decision.model_copy(
                update={
                    "decision": outcome.decision,
                    "reason": f"{outcome.rule}: {outcome.reason}",
                    "requires_approval": outcome.decision is PolicyDecisionType.REQUIRE_APPROVAL,
                    "violations": [
                        *decision.violations,
                        *(
                            [outcome.rule]
                            if outcome.decision is not PolicyDecisionType.ALLOW
                            else []
                        ),
                    ],
                }
            )
            break

        if decision.decision is PolicyDecisionType.ALLOW and spec.requires_approval:
            # Defence in depth: a tool that declares approval can never be ALLOWed, even if a #
            # future rule ordering mistake would otherwise permit it.
            decision = decision.model_copy(
                update={
                    "decision": PolicyDecisionType.REQUIRE_APPROVAL,
                    "requires_approval": True,
                    "reason": (
                        f"tool_requires_approval: '{spec.name}' declares approval is required."
                    ),
                }
            )

        logger.info(
            "policy_evaluated",
            tool=spec.name,
            actor=actor.actor_id,
            role=actor.role.value,
            decision=decision.decision.value,
            effective_risk=assessment.effective_risk.value,
            escalators=assessment.escalators,
        )
        return decision

    def evaluate_or_raise(self, *args: Any, **kwargs: Any) -> PolicyDecision:
        """Convenience for call sites where a DENY should abort the operation immediately."""
        decision = self.evaluate(*args, **kwargs)
        if decision.denied:
            raise PolicyDenied(decision.reason, details={"tool": decision.tool_name})
        return decision


def build_policy_engine(registry: ToolRegistry, settings: Settings) -> PolicyEngine:
    return PolicyEngine(registry, settings)


__all__ = ["PolicyEngine", "build_policy_engine"]
