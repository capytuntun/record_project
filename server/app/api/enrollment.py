"""Enrollment token issuance: /api/enrollment-tokens (spec section 18).

A token carries a revocation flag, a scope, a usage limit and -- optionally --
an expiry. Validity is set in calendar units (years / months / days) because an
installer package is planned against a calendar, not a clock: "this image is
good for one year" is the real-world unit, and a month is not a fixed number of
hours.

Expiry may be omitted entirely for a package that must keep working
indefinitely. See EnrollmentToken's docstring for what compensates.

The plaintext is shown exactly once, at creation, and only its SHA-256 is
stored. A token grants nothing except the right to enroll an endpoint -- it is
never an administrator credential.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import EnrollmentToken, add_period, db, describe_period, iso, utcnow
from ..models.audit import CREATE_ENROLLMENT_TOKEN, REVOKE_ENROLLMENT_TOKEN
from ..request_context import require_current_user
from ..security.authn import require_permission
from ..security.passwords import generate_secret, sha256_hex
from ..security.rbac import (
    ENROLLMENT_TOKENS_CREATE,
    ENROLLMENT_TOKENS_READ,
    ENROLLMENT_TOKENS_REVOKE,
)
from ..services import audit
from .validation import get_int, get_str, json_body, paginated, pagination

bp = Blueprint("enrollment", __name__, url_prefix="/api/enrollment-tokens")

MAX_YEARS = 10
MAX_MONTHS = 120
MAX_DAYS = 3650
MAX_USES = 10_000

# Default validity for a new package: one year.
DEFAULT_PERIOD = {"years": 1, "months": 0, "days": 0}


def _read_validity(body: dict) -> tuple[object, str]:
    """Return (expires_at, human description) from the request body.

    Accepts either ``neverExpires: true`` or any combination of
    ``years`` / ``months`` / ``days``. Omitting all of them uses the default.
    """
    if body.get("neverExpires") is True:
        return None, "永不過期"

    supplied = any(key in body for key in ("years", "months", "days"))
    source = body if supplied else DEFAULT_PERIOD

    years = get_int(source, "years", default=0, minimum=0, maximum=MAX_YEARS) or 0
    months = get_int(source, "months", default=0, minimum=0, maximum=MAX_MONTHS) or 0
    days = get_int(source, "days", default=0, minimum=0, maximum=MAX_DAYS) or 0

    if years == 0 and months == 0 and days == 0:
        raise ValidationError(
            "有效期間至少要設定 1 天，或改用 neverExpires 建立永不過期的憑證。"
        )

    return add_period(utcnow(), years=years, months=months, days=days), describe_period(
        years, months, days
    )


@bp.post("")
@require_permission(ENROLLMENT_TOKENS_CREATE)
def create_token():
    actor = require_current_user()
    body = json_body()

    label = get_str(body, "label", max_length=128)
    organization_id = get_str(body, "organizationId", required=False, max_length=64)
    policy_id = get_str(body, "policyId", required=False, max_length=36)
    notes = get_str(body, "notes", required=False, max_length=1000)

    expires_at, validity = _read_validity(body)

    # 0 means unlimited; the default stays a single use.
    max_uses = get_int(body, "maxUses", default=1, minimum=0, maximum=MAX_USES)

    plaintext = generate_secret(48)
    token = EnrollmentToken(
        token_hash=sha256_hex(plaintext),
        label=label,
        organization_id=organization_id,
        policy_id=policy_id,
        expires_at=expires_at,
        max_uses=max_uses,
        created_by=actor.id,
        notes=notes,
    )
    db.session.add(token)
    db.session.flush()

    audit.record(
        CREATE_ENROLLMENT_TOKEN,
        target_type="enrollment_token",
        target_id=token.id,
        # The token itself is never written to the audit trail. A token with no
        # expiry is flagged so it stands out in a compliance review.
        metadata={
            "label": label,
            "organizationId": organization_id,
            "validity": validity,
            "expiresAt": iso(token.expires_at),
            "neverExpires": token.never_expires,
            "maxUses": max_uses,
            "unlimitedUses": token.unlimited_uses,
        },
    )
    db.session.commit()

    payload = token.to_dict()
    # The one and only time the plaintext leaves the server.
    payload["token"] = plaintext
    payload["validity"] = validity
    payload["warning"] = (
        "請妥善保存此憑證，離開此頁面後無法再次取得。"
        + ("此憑證永不過期，只能以手動撤銷停用。" if token.never_expires
           else f"此憑證有效期間為 {validity}，逾期後將無法用於註冊。")
    )
    return jsonify(payload), 201


@bp.get("")
@require_permission(ENROLLMENT_TOKENS_READ)
def list_tokens():
    offset, limit = pagination()
    query = db.session.query(EnrollmentToken)

    if request.args.get("usableOnly", "").lower() in {"1", "true"}:
        from sqlalchemy import or_

        query = query.filter(
            EnrollmentToken.revoked_at.is_(None),
            # NULL expiry = never expires; 0 max_uses = unlimited.
            or_(EnrollmentToken.expires_at.is_(None), EnrollmentToken.expires_at > utcnow()),
            or_(EnrollmentToken.max_uses == 0,
                EnrollmentToken.use_count < EnrollmentToken.max_uses),
        )

    total = query.count()
    rows = (
        query.order_by(EnrollmentToken.created_at.desc()).offset(offset).limit(limit).all()
    )
    return jsonify(paginated([row.to_dict() for row in rows], total, offset, limit))


@bp.post("/<token_id>/revoke")
@require_permission(ENROLLMENT_TOKENS_REVOKE)
def revoke_token(token_id: str):
    token = db.session.get(EnrollmentToken, token_id)
    if token is None:
        raise NotFoundError("Enrollment token not found.")
    if token.revoked_at is not None:
        raise ConflictError("Token is already revoked.")

    body = json_body() if request.is_json else {}
    reason = get_str(body, "reason", required=False, max_length=64) or "manual_revocation"

    token.revoked_at = utcnow()
    token.revoked_reason = reason

    audit.record(
        REVOKE_ENROLLMENT_TOKEN,
        target_type="enrollment_token",
        target_id=token.id,
        metadata={"label": token.label, "reason": reason, "useCount": token.use_count},
    )
    db.session.commit()
    return jsonify(token.to_dict())
