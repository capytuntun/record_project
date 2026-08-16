"""API exceptions and sanitized error responses (spec section 26).

Clients receive a stable error code, a safe message and a request id. Stack
traces, SQL text, file paths and secrets stay in the server log.
"""

from __future__ import annotations

import logging

from flask import Flask, g, jsonify
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """Base class for errors that are safe to describe to a client."""

    status_code = 400
    error_code = "bad_request"

    def __init__(self, message: str | None = None, *, details: dict | None = None) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or "Request could not be processed."
        self.details = details


class ValidationError(ApiError):
    status_code = 400
    error_code = "validation_error"


class AuthenticationError(ApiError):
    status_code = 401
    error_code = "unauthenticated"

    def __init__(self, message: str = "Authentication required.", **kwargs) -> None:
        super().__init__(message, **kwargs)


class AuthorizationError(ApiError):
    status_code = 403
    error_code = "forbidden"

    def __init__(self, message: str = "You do not have permission to perform this action.", **kwargs) -> None:
        super().__init__(message, **kwargs)


class NotFoundError(ApiError):
    status_code = 404
    error_code = "not_found"

    def __init__(self, message: str = "Resource not found.", **kwargs) -> None:
        super().__init__(message, **kwargs)


class ConflictError(ApiError):
    status_code = 409
    error_code = "conflict"


class RateLimitError(ApiError):
    status_code = 429
    error_code = "rate_limited"


def _request_id() -> str | None:
    return getattr(g, "request_id", None)


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _handle_api_error(exc: ApiError):
        body = {
            "error": exc.error_code,
            "message": exc.message,
            "requestId": _request_id(),
        }
        if exc.details:
            body["details"] = exc.details
        # 401/403 are routine; log at info so real failures stand out.
        logger.info(
            "api_error code=%s status=%s request_id=%s", exc.error_code, exc.status_code, _request_id()
        )
        return jsonify(body), exc.status_code

    @app.errorhandler(HTTPException)
    def _handle_http_exception(exc: HTTPException):
        # Werkzeug's default description can name internal routes; replace it.
        code_map = {
            400: "bad_request",
            401: "unauthenticated",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            409: "conflict",
            413: "payload_too_large",
            415: "unsupported_media_type",
            429: "rate_limited",
        }
        status = exc.code or 500
        return (
            jsonify(
                {
                    "error": code_map.get(status, "http_error"),
                    "message": exc.name,
                    "requestId": _request_id(),
                }
            ),
            status,
        )

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):
        # Full detail to the server log only.
        logger.exception("unhandled_exception request_id=%s", _request_id())
        return (
            jsonify(
                {
                    "error": "internal_server_error",
                    "message": "Internal server error",
                    "requestId": _request_id(),
                }
            ),
            500,
        )
