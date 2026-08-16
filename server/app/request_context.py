"""Per-request state: request id, client address, and the authenticated principal.

Kept separate from the security package so both the audit service and the
authentication decorators can use it without a circular import.
"""

from __future__ import annotations

from flask import current_app, g, has_app_context, has_request_context, request

from .models import Endpoint, EndpointCredential, User

# CLI commands audit their actions too, and run with no request in flight.
# These helpers return None there rather than raising.
_CLI_SOURCE = "cli"


def request_id() -> str | None:
    if not has_app_context():
        return None
    return getattr(g, "request_id", None)


def client_ip() -> str | None:
    """The caller's address, trusting proxy headers only when configured to.

    With TRUST_PROXY_HEADERS off, X-Forwarded-For is ignored: an attacker could
    otherwise forge the source IP recorded in the audit log.
    """
    if not has_request_context():
        return _CLI_SOURCE
    if current_app.config.get("TRUST_PROXY_HEADERS"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.remote_addr or None


def user_agent() -> str | None:
    if not has_request_context():
        return None
    value = request.headers.get("User-Agent", "")
    return value[:256] or None


def set_current_user(user: User | None) -> None:
    g.current_user = user


def current_user() -> User | None:
    if not has_app_context():
        return None
    return getattr(g, "current_user", None)


def require_current_user() -> User:
    user = current_user()
    if user is None:
        raise RuntimeError(
            "require_current_user() called outside an authenticated request. "
            "Did the view forget @require_admin_auth?"
        )
    return user


def set_current_endpoint(endpoint: Endpoint | None,
                         credential: EndpointCredential | None = None) -> None:
    g.current_endpoint = endpoint
    g.current_credential = credential


def current_endpoint() -> Endpoint | None:
    if not has_app_context():
        return None
    return getattr(g, "current_endpoint", None)


def current_credential() -> EndpointCredential | None:
    """The credential the agent authenticated with, so a view can report on it."""
    if not has_app_context():
        return None
    return getattr(g, "current_credential", None)
