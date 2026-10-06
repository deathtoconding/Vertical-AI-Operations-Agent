"""Error translation (API contract).

Every error leaving the API has a stable shape, a machine-readable ``code`` and a correlation
id. Two properties matter operationally:

* **No stack traces or internal paths** in responses (information disclosure, threat T-21);
* **Denials are not 500s.** ``PolicyDenied`` is a 403 with the rule that denied it,
  ``ApprovalRequired`` is a 409 pointing at the approval, and an unconfigured dependency is a
  503 — a client can tell "you may not" from "we cannot right now".
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import (
    AppError,
    ApprovalInvalidated,
    ApprovalRequired,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    DependencyUnavailable,
    IdempotencyConflict,
    InvalidTransition,
    NotFoundError,
    PolicyDenied,
    RateLimitedError,
    UnsafeActionAttempt,
    ValidationFailed,
)
from app.core.logging import current_correlation, get_logger

logger = get_logger(__name__)

STATUS_BY_ERROR: dict[type[Exception], int] = {
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    AuthorizationError: status.HTTP_403_FORBIDDEN,
    PolicyDenied: status.HTTP_403_FORBIDDEN,
    UnsafeActionAttempt: status.HTTP_403_FORBIDDEN,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    ApprovalInvalidated: status.HTTP_409_CONFLICT,
    ApprovalRequired: status.HTTP_409_CONFLICT,
    IdempotencyConflict: status.HTTP_409_CONFLICT,
    InvalidTransition: status.HTTP_409_CONFLICT,
    # `HTTP_422_UNPROCESSABLE_CONTENT` is the current name; the starlette constant it replaces
    # is deprecated and would trip the warnings-as-errors filter.
    ValidationFailed: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RateLimitedError: status.HTTP_429_TOO_MANY_REQUESTS,
    DependencyUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _status_for(error: AppError) -> int:
    for error_type in type(error).__mro__:
        if error_type in STATUS_BY_ERROR:
            return STATUS_BY_ERROR[error_type]
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def error_body(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    status_code: int = 500,
) -> dict[str, Any]:
    correlation = current_correlation()
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "correlation_id": correlation.get("correlation_id"),
            "trace_id": correlation.get("trace_id"),
            "status": status_code,
        }
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        status_code = _status_for(exc)
        if status_code >= 500:
            logger.error("api_error", code=exc.code, path=request.url.path, exc_info=exc)
        else:
            logger.info("api_error", code=exc.code, path=request.url.path, status=status_code)
        return JSONResponse(
            status_code=status_code,
            content=error_body(exc.code, str(exc), details=exc.details, status_code=status_code),
            headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_body(
                "validation_failed",
                "The request did not match the expected schema.",
                details={"errors": exc.errors()},
                status_code=422,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                f"http_{exc.status_code}",
                str(exc.detail),
                status_code=exc.status_code,
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log with the traceback; return nothing that could leak internals.
        logger.error("unhandled_exception", path=request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body(
                "internal_error",
                "An internal error occurred. The correlation id identifies the log entry.",
                status_code=500,
            ),
        )


__all__ = ["STATUS_BY_ERROR", "error_body", "install_error_handlers"]
