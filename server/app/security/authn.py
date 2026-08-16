"""Request authentication for the two kinds of caller: admins and agents.

Admins present a short-lived access JWT. Agents present their per-device
credential. Neither path trusts anything the caller says about its own
privileges -- the authoritative role and endpoint state are re-read from the
database on every request (spec sections 7 and 28.10).
"""

from __future__ import annotations

from functools import wraps

from flask import request

from ..errors import AuthenticationError, AuthorizationError
from ..models import EndpointCredential, User, db, utcnow
from ..models.audit import ACCESS_DENIED
from ..models.endpoint import STATE_DISABLED
from ..request_context import (
    current_user,
    set_current_endpoint,
    set_current_user,
)
from ..services import audit
from .passwords import sha256_hex
from .rbac import has_permission
from .tokens import TokenError, decode_access_token


def _bearer_token() -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("Missing or malformed Authorization header.")
    return token.strip()


def _load_authenticated_user() -> User:
    try:
        payload = decode_access_token(_bearer_token())
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    user = db.session.get(User, payload.get("sub") or "")
    if user is None or not user.is_active:
        # Same message either way: do not confirm which accounts exist.
        raise AuthenticationError("Token is no longer valid.")

    # A password change, role change or forced logout bumps the epoch, which
    # retires every access token minted under the previous one.
    if payload.get("epoch") != user.token_epoch:
        raise AuthenticationError("Token is no longer valid.")

    return user


def require_admin_auth(view):
    """Require a valid admin access token; populates the request's current user."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        user = _load_authenticated_user()
        set_current_user(user)
        return view(*args, **kwargs)

    return wrapper


def require_permission(*permissions: str):
    """Require authentication plus every listed permission.

    Denials are written to the audit log before the 403 is returned.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                user = _load_authenticated_user()
                set_current_user(user)

            missing = [perm for perm in permissions if not has_permission(user, perm)]
            if missing:
                audit.record_denied(
                    ACCESS_DENIED,
                    target_type="permission",
                    target_id=",".join(missing),
                    reason=f"role {user.role} lacks {','.join(missing)}",
                )
                db.session.commit()
                raise AuthorizationError()

            return view(*args, **kwargs)

        return wrapper

    return decorator


def authenticate_agent_credential(presented: str):
    """Resolve a device credential to its (endpoint, credential).

    Shared by the REST decorator and the screen WebSocket handler so both apply
    exactly the same checks. Raises AuthenticationError / AuthorizationError on
    any failure; updates last_used_at on success. The caller commits.
    """
    credential = (
        db.session.query(EndpointCredential)
        .filter(EndpointCredential.secret_hash == sha256_hex(presented or ""))
        .one_or_none()
    )
    if credential is None or not credential.is_usable():
        raise AuthenticationError("Endpoint credential is not valid.")

    endpoint = credential.endpoint
    if endpoint is None or endpoint.is_deleted:
        raise AuthenticationError("Endpoint credential is not valid.")
    if endpoint.state == STATE_DISABLED:
        # Disabled is an administrative decision; say so plainly so the agent
        # can stop retrying and surface it to enterprise IT.
        raise AuthorizationError("This endpoint has been disabled by an administrator.")

    credential.last_used_at = utcnow()
    return endpoint, credential


def require_agent_auth(view):
    """Authenticate an endpoint agent by its per-device credential (section 11)."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            presented = _bearer_token()
        except AuthenticationError:
            raise AuthenticationError("Endpoint credential required.")

        endpoint, credential = authenticate_agent_credential(presented)
        set_current_endpoint(endpoint, credential)
        set_current_user(None)
        return view(*args, **kwargs)

    return wrapper
