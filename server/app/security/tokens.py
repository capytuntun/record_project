"""Access and refresh token issuance (spec section 5).

Access tokens are short-lived stateless JWTs. Refresh tokens are opaque random
strings stored as SHA-256 digests so they can be revoked and rotated, with
reuse of an already-rotated token revoking its whole family.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import jwt
from flask import current_app

from ..models import RefreshToken, User, as_utc, db, utcnow
from .passwords import generate_secret, sha256_hex

ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired or revoked."""


@dataclass
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


def _secret() -> str:
    return current_app.config["SECRET_KEY"]


def issue_access_token(user: User) -> tuple[str, int]:
    """Return (jwt, seconds_until_expiry)."""
    ttl = timedelta(minutes=current_app.config["ACCESS_TOKEN_TTL_MINUTES"])
    now = utcnow()
    payload = {
        "sub": user.id,
        "username": user.username,
        # Role is a convenience for the client. The server always re-reads the
        # authoritative role from the database (section 7).
        "role": user.role,
        "type": ACCESS_TOKEN_TYPE,
        # Invalidation marker: see User.token_epoch.
        "epoch": user.token_epoch,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM), int(ttl.total_seconds())


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid.") from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise TokenError("Token is not an access token.")
    return payload


def issue_refresh_token(
    user: User,
    *,
    family_id: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, RefreshToken]:
    """Create a refresh token. The plaintext is returned once and not stored."""
    plaintext = generate_secret(48)
    record = RefreshToken(
        user_id=user.id,
        token_hash=sha256_hex(plaintext),
        family_id=family_id or str(uuid.uuid4()),
        expires_at=utcnow() + timedelta(days=current_app.config["REFRESH_TOKEN_TTL_DAYS"]),
        created_ip=ip,
        created_user_agent=(user_agent or "")[:256] or None,
    )
    db.session.add(record)
    return plaintext, record


def issue_token_pair(
    user: User, *, ip: str | None = None, user_agent: str | None = None
) -> IssuedTokens:
    access, expires_in = issue_access_token(user)
    refresh, _ = issue_refresh_token(user, ip=ip, user_agent=user_agent)
    return IssuedTokens(access_token=access, refresh_token=refresh, expires_in=expires_in)


def revoke_family(family_id: str, reason: str) -> int:
    """Revoke every live token in a family. Returns how many were revoked."""
    now = utcnow()
    tokens = (
        db.session.query(RefreshToken)
        .filter(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .all()
    )
    for token in tokens:
        token.revoked_at = now
        token.revoked_reason = reason
    return len(tokens)


def revoke_all_for_user(user_id: str, reason: str) -> int:
    now = utcnow()
    tokens = (
        db.session.query(RefreshToken)
        .filter(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .all()
    )
    for token in tokens:
        token.revoked_at = now
        token.revoked_reason = reason
    return len(tokens)


class RefreshReuseDetected(TokenError):
    """An already-rotated refresh token was presented again.

    Treated as credential theft: the entire family is revoked.
    """

    def __init__(self, family_id: str, user_id: str) -> None:
        super().__init__("Refresh token has already been used.")
        self.family_id = family_id
        self.user_id = user_id


def rotate_refresh_token(
    presented: str, *, ip: str | None = None, user_agent: str | None = None
) -> tuple[User, IssuedTokens]:
    """Exchange a refresh token for a new pair, invalidating the old one."""
    record = (
        db.session.query(RefreshToken)
        .filter(RefreshToken.token_hash == sha256_hex(presented))
        .one_or_none()
    )
    if record is None:
        raise TokenError("Refresh token is invalid.")

    if record.used:
        # The token was already exchanged. Either it leaked, or a client is
        # replaying; kill every session descended from that login.
        revoke_family(record.family_id, "reuse_detected")
        db.session.commit()
        raise RefreshReuseDetected(record.family_id, record.user_id)

    if record.revoked_at is not None:
        raise TokenError("Refresh token has been revoked.")

    expires_at = as_utc(record.expires_at)
    if expires_at is None or expires_at <= utcnow():
        raise TokenError("Refresh token has expired.")

    user = db.session.get(User, record.user_id)
    if user is None or not user.is_active:
        raise TokenError("Account is not active.")

    new_plaintext, new_record = issue_refresh_token(
        user, family_id=record.family_id, ip=ip, user_agent=user_agent
    )
    record.used = True
    record.revoked_at = utcnow()
    record.revoked_reason = "rotated"
    record.replaced_by_id = new_record.id

    access, expires_in = issue_access_token(user)
    return user, IssuedTokens(
        access_token=access, refresh_token=new_plaintext, expires_in=expires_in
    )


def invalidate_all_sessions(user: User, reason: str) -> int:
    """End every session for ``user``, access tokens included.

    Bumping the epoch kills outstanding access tokens (which are stateless and
    otherwise valid until they expire); revoking the refresh tokens stops new
    ones being minted. Both are needed -- either alone leaves a gap.
    """
    user.token_epoch = (user.token_epoch or 0) + 1
    return revoke_all_for_user(user.id, reason)


def revoke_refresh_token(presented: str, reason: str = "logout") -> RefreshToken | None:
    record = (
        db.session.query(RefreshToken)
        .filter(RefreshToken.token_hash == sha256_hex(presented))
        .one_or_none()
    )
    if record is None or record.revoked_at is not None:
        return record
    record.revoked_at = utcnow()
    record.revoked_reason = reason
    return record
