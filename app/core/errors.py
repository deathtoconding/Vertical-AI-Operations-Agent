"""Typed errors and the structured error contract (OPS-010).

Error responses are machine-readable so callers (the UI, runbooks, tests) never have to
parse prose::

    {"error": {"code": "authorization_denied", "message": "...", "details": {...},
               "request_id": "...", "trace_id": "..."}}

Two rules from the architecture document are implemented here:

* a failure from an external system becomes a *typed* error, never an empty success
  (``IntegrationError`` family);
* invalid state transitions and policy denials are errors with their own codes, because
  they are expected, auditable outcomes — not bugs.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every deliberate failure in this application."""

    code: str = "internal_error"
    status_code: int = 500
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.details: dict[str, Any] = dict(details or {})
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


# --------------------------------------------------------------------------- #
# Client errors
# --------------------------------------------------------------------------- #


class ValidationFailed(AppError):
    code = "validation_failed"
    status_code = 422
    message = "The request payload is invalid."


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404
    message = "The requested resource does not exist."


class ConflictError(AppError):
    code = "conflict"
    status_code = 409
    message = "The request conflicts with the current state."


class AuthenticationError(AppError):
    code = "authentication_required"
    status_code = 401
    message = "A valid bearer token is required."


class AuthorizationError(AppError):
    code = "authorization_denied"
    status_code = 403
    message = "This actor is not permitted to perform this operation."


class RateLimitedError(AppError):
    code = "rate_limited"
    status_code = 429
    message = "Too many requests."

    def __init__(self, retry_after_seconds: int, **kwargs: Any) -> None:
        super().__init__(details={"retry_after_seconds": retry_after_seconds}, **kwargs)
        self.retry_after_seconds = retry_after_seconds


# --------------------------------------------------------------------------- #
# State and policy
# --------------------------------------------------------------------------- #


class InvalidTransition(AppError):
    code = "invalid_transition"
    status_code = 409
    message = "This state transition is not allowed."


class PolicyDenied(AppError):
    code = "policy_denied"
    status_code = 403
    message = "Policy denied this action."


class ApprovalRequired(AppError):
    code = "approval_required"
    status_code = 409
    message = "This action requires human approval before execution."


class ApprovalInvalidated(AppError):
    code = "approval_invalidated"
    status_code = 409
    message = "The approval does not match the action payload."


class UnsafeActionAttempt(AppError):
    """A high-risk action was attempted without a valid, matching approval (SEC-002)."""

    code = "unsafe_action_attempted"
    status_code = 403
    message = "A high-risk action was attempted without the required approval."


class ToolNotFound(AppError):
    code = "unknown_tool"
    status_code = 400
    message = "The requested tool is not registered."


class ToolExecutionError(AppError):
    code = "tool_execution_failed"
    status_code = 502
    message = "The tool failed to complete."


class IdempotencyConflict(AppError):
    code = "idempotency_conflict"
    status_code = 409
    message = "A different payload was already processed with this idempotency key."


# --------------------------------------------------------------------------- #
# Dependency errors
# --------------------------------------------------------------------------- #


class DependencyUnavailable(AppError):
    code = "dependency_unavailable"
    status_code = 503
    message = "A required dependency is unavailable."


class IntegrationError(DependencyUnavailable):
    code = "integration_error"

    def __init__(self, system: str, message: str | None = None, **kwargs: Any) -> None:
        self.system = system
        super().__init__(
            message or f"Integration '{system}' failed.",
            details={"system": system, **(kwargs.pop("details", None) or {})},
            **kwargs,
        )


class IntegrationUnavailable(IntegrationError):
    code = "integration_unavailable"


class IntegrationRateLimited(IntegrationError):
    code = "integration_rate_limited"
    status_code = 429


class IntegrationAuthenticationError(IntegrationError):
    code = "integration_authentication_failed"
    status_code = 502


class IntegrationBadResponse(IntegrationError):
    code = "integration_bad_response"
    status_code = 502


class DatabaseUnavailable(DependencyUnavailable):
    code = "database_unavailable"


class LLMUnavailable(DependencyUnavailable):
    code = "llm_unavailable"

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        self.reason = str(kwargs.pop("reason", "unavailable"))
        super().__init__(
            message or "The language model is unavailable.",
            details={"reason": self.reason},
            **kwargs,
        )


# --------------------------------------------------------------------------- #
# Agent / investigation
# --------------------------------------------------------------------------- #


class InsufficientEvidence(AppError):
    code = "evidence_insufficient"
    status_code = 409
    message = "There is not enough evidence to reach a diagnosis."


class UngroundedDiagnosis(AppError):
    """The reasoner cited evidence that does not exist for this incident (OPS-041)."""

    code = "diagnosis_ungrounded"
    status_code = 409
    message = "The diagnosis referenced evidence that could not be verified."


class VerificationError(AppError):
    code = "verification_failed"
    status_code = 409
    message = "The verification could not be completed."


class ConfigurationError(AppError):
    code = "configuration_error"
    status_code = 500
    message = "The application is misconfigured."


__all__ = [
    "AppError",
    "ApprovalInvalidated",
    "ApprovalRequired",
    "AuthenticationError",
    "AuthorizationError",
    "ConfigurationError",
    "ConflictError",
    "DatabaseUnavailable",
    "DependencyUnavailable",
    "IdempotencyConflict",
    "InsufficientEvidence",
    "IntegrationAuthenticationError",
    "IntegrationBadResponse",
    "IntegrationError",
    "IntegrationRateLimited",
    "IntegrationUnavailable",
    "InvalidTransition",
    "LLMUnavailable",
    "NotFoundError",
    "PolicyDenied",
    "RateLimitedError",
    "ToolExecutionError",
    "ToolNotFound",
    "UngroundedDiagnosis",
    "UnsafeActionAttempt",
    "ValidationFailed",
    "VerificationError",
]
