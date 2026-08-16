"""Administrator authentication: /api/auth (spec sections 5 and 28)."""

from __future__ import annotations

from flask import Blueprint, jsonify

from ..errors import AuthenticationError, ValidationError
from ..extensions import limiter
from ..models import User, db, utcnow
from ..models.audit import (
    LOGIN,
    LOGIN_FAILED,
    LOGOUT,
    TOKEN_REFRESH,
    TOKEN_REUSE_DETECTED,
    RESULT_FAILURE,
)
from ..models.user import STATUS_ACTIVE
from ..request_context import client_ip, require_current_user, user_agent
from ..security.authn import require_admin_auth
from ..security.passwords import hash_password, needs_rehash, verify_password
from ..security.rbac import permissions_for
from ..security.tokens import (
    RefreshReuseDetected,
    TokenError,
    issue_token_pair,
    revoke_all_for_user,
    revoke_refresh_token,
    rotate_refresh_token,
)
from ..services import audit
from .validation import get_str, json_body

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

# Deliberately strict: credential stuffing is the most likely attack on a
# management console exposed to a corporate network.
LOGIN_RATE_LIMIT = "10 per minute; 50 per hour"


@bp.post("/login")
@limiter.limit(LOGIN_RATE_LIMIT)
def login():
    body = json_body()
    username = (get_str(body, "username", max_length=64) or "").lower()
    password = get_str(body, "password", min_length=1, max_length=256, strip=False)

    user = (
        db.session.query(User)
        .filter(User.username == username, User.deleted_at.is_(None))
        .one_or_none()
    )

    # Always run the verification so timing does not distinguish "no such user"
    # from "wrong password".
    password_ok = verify_password(user.password_hash if user else None, password or "")

    if not user or not password_ok or user.status != STATUS_ACTIVE:
        audit.record(
            LOGIN_FAILED,
            actor=None,
            actor_username=username,
            target_type="user",
            target_id=user.id if user else None,
            result=RESULT_FAILURE,
            metadata={"reason": "invalid_credentials" if not password_ok else "account_not_active"},
        )
        db.session.commit()
        raise AuthenticationError("Invalid username or password.")

    # Opportunistically upgrade hashes made with older parameters.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    tokens = issue_token_pair(user, ip=client_ip(), user_agent=user_agent())
    user.last_login_at = utcnow()

    audit.record(LOGIN, actor=user, target_type="user", target_id=user.id)
    db.session.commit()

    return jsonify(
        {
            "accessToken": tokens.access_token,
            "refreshToken": tokens.refresh_token,
            "tokenType": "Bearer",
            "expiresIn": tokens.expires_in,
            "user": user.to_dict(),
            # Lets the console hide controls it cannot use. The server still
            # re-checks every call (section 25).
            "permissions": sorted(permissions_for(user)),
        }
    )


@bp.post("/refresh")
@limiter.limit("30 per minute")
def refresh():
    body = json_body()
    presented = get_str(body, "refreshToken", min_length=10, max_length=512, strip=False)
    if presented is None:
        raise ValidationError("'refreshToken' is required.")

    try:
        user, tokens = rotate_refresh_token(
            presented, ip=client_ip(), user_agent=user_agent()
        )
    except RefreshReuseDetected as exc:
        # rotate_refresh_token already revoked the family and committed.
        replayed_user = db.session.get(User, exc.user_id)
        audit.record(
            TOKEN_REUSE_DETECTED,
            actor=replayed_user,
            target_type="user",
            target_id=exc.user_id,
            result=RESULT_FAILURE,
            metadata={"familyId": exc.family_id, "action": "family_revoked"},
        )
        db.session.commit()
        raise AuthenticationError("Session ended. Please sign in again.") from exc
    except TokenError as exc:
        raise AuthenticationError("Session ended. Please sign in again.") from exc

    audit.record(TOKEN_REFRESH, actor=user, target_type="user", target_id=user.id)
    db.session.commit()

    return jsonify(
        {
            "accessToken": tokens.access_token,
            "refreshToken": tokens.refresh_token,
            "tokenType": "Bearer",
            "expiresIn": tokens.expires_in,
        }
    )


@bp.post("/logout")
@require_admin_auth
def logout():
    user = require_current_user()
    body = json_body() if _has_json_body() else {}

    all_sessions = bool(body.get("allSessions"))
    if all_sessions:
        revoked = revoke_all_for_user(user.id, "logout_all")
    else:
        presented = get_str(body, "refreshToken", required=False, max_length=512, strip=False)
        revoked = 1 if presented and revoke_refresh_token(presented) else 0

    audit.record(
        LOGOUT,
        actor=user,
        target_type="user",
        target_id=user.id,
        metadata={"allSessions": all_sessions, "sessionsRevoked": revoked},
    )
    db.session.commit()
    return jsonify({"revokedSessions": revoked})


@bp.get("/me")
@require_admin_auth
def me():
    user = require_current_user()
    return jsonify({"user": user.to_dict(), "permissions": sorted(permissions_for(user))})


def _has_json_body() -> bool:
    from flask import request

    return request.is_json and request.get_json(silent=True) is not None
