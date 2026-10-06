"""Action execution: proposals become bounded, approved, idempotent side effects."""

from app.actions.executor import IDEMPOTENCY_SCOPE, ActionExecutor, ExecutionOutcome

__all__ = ["IDEMPOTENCY_SCOPE", "ActionExecutor", "ExecutionOutcome"]
