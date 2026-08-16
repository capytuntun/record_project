"""Administrator account management: /api/users (spec sections 8 and 28)."""

from __future__ import annotations

from flask import Blueprint, jsonify

from ..errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from ..models import User, db, utcnow
from ..models.audit import (
    CHANGE_ADMIN_FEATURES,
    CHANGE_PASSWORD,
    CHANGE_ROLE,
    CREATE_USER,
    DELETE_USER,
    UPDATE_USER,
)
from ..models.user import ROLES, STATUS_ACTIVE, USER_STATUSES
from ..request_context import require_current_user
from ..security.authn import require_permission
from ..security.passwords import (
    PasswordPolicyError,
    hash_password,
    verify_password,
)
from ..security.rbac import (
    USERS_CREATE,
    USERS_DELETE,
    USERS_READ,
    USERS_UPDATE,
    can_assign_role,
    can_manage_user,
    can_reset_password,
    would_orphan_system,
)
from ..security.tokens import invalidate_all_sessions
from ..services import audit
from .validation import get_str, get_username, json_body, paginated, pagination

bp = Blueprint("users", __name__, url_prefix="/api/users")


def _load_user(user_id: str, *, include_deleted: bool = False) -> User:
    user = db.session.get(User, user_id)
    if user is None or (user.is_deleted and not include_deleted):
        raise NotFoundError("User not found.")
    return user


def _guard_manage(actor: User, target: User) -> None:
    allowed, reason = can_manage_user(actor, target)
    if not allowed:
        audit.record_denied(
            UPDATE_USER, target_type="user", target_id=target.id, reason=reason
        )
        db.session.commit()
        raise AuthorizationError(reason)


@bp.get("")
@require_permission(USERS_READ)
def list_users():
    from flask import request

    offset, limit = pagination()
    query = db.session.query(User)

    if request.args.get("includeDeleted", "").lower() not in {"1", "true"}:
        query = query.filter(User.deleted_at.is_(None))

    role = request.args.get("role", "").strip()
    if role:
        if role not in ROLES:
            raise ValidationError(f"'role' must be one of: {', '.join(ROLES)}.")
        query = query.filter(User.role == role)

    search = request.args.get("search", "").strip()
    if search:
        # Bound parameter; SQLAlchemy escapes it, so no injection surface.
        query = query.filter(User.username.like(f"%{search}%"))

    total = query.count()
    rows = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()
    return jsonify(paginated([u.to_dict() for u in rows], total, offset, limit))


@bp.get("/<user_id>")
@require_permission(USERS_READ)
def get_user(user_id: str):
    return jsonify(_load_user(user_id, include_deleted=True).to_dict())


@bp.post("")
@require_permission(USERS_CREATE)
def create_user():
    actor = require_current_user()
    body = json_body()

    username = get_username(body)
    password = get_str(body, "password", min_length=1, max_length=256, strip=False)
    role = get_str(body, "role", required=False, default="ADMIN", choices=ROLES)

    allowed, reason = can_assign_role(actor, role)
    if not allowed:
        audit.record_denied(CREATE_USER, target_type="user", target_id=username, reason=reason)
        db.session.commit()
        raise AuthorizationError(reason)

    existing = db.session.query(User).filter(User.username == username).one_or_none()
    if existing is not None:
        raise ConflictError("That username is already taken.")

    try:
        password_hash = hash_password(password or "")
    except PasswordPolicyError as exc:
        raise ValidationError(str(exc)) from exc

    user = User(
        username=username,
        password_hash=password_hash,
        role=role,
        status=STATUS_ACTIVE,
        created_by=actor.id,
    )
    db.session.add(user)
    db.session.flush()  # assign the id before it is written to the audit row

    audit.record(
        CREATE_USER,
        target_type="user",
        target_id=user.id,
        metadata={"username": username, "role": role},
    )
    db.session.commit()
    return jsonify(user.to_dict()), 201


@bp.patch("/<user_id>")
@require_permission(USERS_UPDATE)
def update_user(user_id: str):
    actor = require_current_user()
    target = _load_user(user_id)
    _guard_manage(actor, target)

    body = json_body()
    changes: dict = {}

    new_status = get_str(body, "status", required=False, choices=USER_STATUSES)
    if new_status and new_status != target.status:
        if target.id == actor.id:
            raise ValidationError("You cannot change your own account status.")
        if new_status != STATUS_ACTIVE and would_orphan_system(target):
            raise ConflictError("This is the last active SUPER_ADMIN and cannot be suspended.")
        changes["status"] = {"from": target.status, "to": new_status}
        target.status = new_status
        if new_status != STATUS_ACTIVE:
            invalidate_all_sessions(target, "account_suspended")

    new_role = get_str(body, "role", required=False, choices=ROLES)
    if new_role and new_role != target.role:
        allowed, reason = can_assign_role(actor, new_role)
        if not allowed:
            audit.record_denied(
                CHANGE_ROLE, target_type="user", target_id=target.id, reason=reason
            )
            db.session.commit()
            raise AuthorizationError(reason)
        if target.id == actor.id:
            raise ValidationError("You cannot change your own role.")
        if would_orphan_system(target):
            raise ConflictError("This is the last active SUPER_ADMIN and cannot be demoted.")

        changes["role"] = {"from": target.role, "to": new_role}
        audit.record(
            CHANGE_ROLE,
            target_type="user",
            target_id=target.id,
            metadata={"username": target.username, "from": target.role, "to": new_role},
        )
        target.role = new_role
        # Privileges changed: force re-authentication so no stale token keeps
        # the old role's access.
        invalidate_all_sessions(target, "role_changed")

    if not changes:
        raise ValidationError("No supported fields to update. Accepts 'status' and 'role'.")

    audit.record(
        UPDATE_USER,
        target_type="user",
        target_id=target.id,
        metadata={"username": target.username, "changes": changes},
    )
    db.session.commit()
    return jsonify(target.to_dict())


@bp.post("/<user_id>/password")
@require_permission(USERS_READ)
def change_password(user_id: str):
    """Change a password.

    Changing your own requires the current password. Only the SUPER_ADMIN may
    reset *another* account's password, without knowing the old one -- that is
    account takeover, so it is not delegated to the ``users:update`` permission
    a plain ADMIN holds. Either way it is recorded in the audit log and every
    session on the affected account is revoked.
    """
    actor = require_current_user()
    target = _load_user(user_id)
    body = json_body()

    is_self = actor.id == target.id
    allowed, reason = can_reset_password(actor, target)
    if not allowed:
        audit.record_denied(
            CHANGE_PASSWORD, target_type="user", target_id=target.id, reason=reason
        )
        db.session.commit()
        raise AuthorizationError(reason)

    if is_self:
        current = get_str(body, "currentPassword", min_length=1, max_length=256, strip=False)
        if not verify_password(target.password_hash, current or ""):
            audit.record_denied(
                CHANGE_PASSWORD,
                target_type="user",
                target_id=target.id,
                reason="current_password_mismatch",
            )
            db.session.commit()
            raise AuthorizationError("目前密碼不正確。")
    else:
        _guard_manage(actor, target)

    new_password = get_str(body, "newPassword", min_length=1, max_length=256, strip=False)
    try:
        target.password_hash = hash_password(new_password or "")
    except PasswordPolicyError as exc:
        raise ValidationError(str(exc)) from exc

    # Every existing session for that account stops working.
    revoked = invalidate_all_sessions(target, "password_changed")

    audit.record(
        CHANGE_PASSWORD,
        target_type="user",
        target_id=target.id,
        metadata={"username": target.username, "self": is_self, "sessionsRevoked": revoked},
    )
    db.session.commit()
    return jsonify({"status": "password_changed", "sessionsRevoked": revoked})


@bp.delete("/<user_id>")
@require_permission(USERS_DELETE)
def delete_user(user_id: str):
    """Soft delete. The row stays so audit history keeps resolving (section 8)."""
    actor = require_current_user()
    target = _load_user(user_id)
    _guard_manage(actor, target)

    if target.id == actor.id:
        raise ValidationError("You cannot delete your own account.")
    if would_orphan_system(target):
        raise ConflictError("This is the last active SUPER_ADMIN and cannot be deleted.")

    target.deleted_at = utcnow()
    revoked = invalidate_all_sessions(target, "account_deleted")

    audit.record(
        DELETE_USER,
        target_type="user",
        target_id=target.id,
        metadata={"username": target.username, "role": target.role, "sessionsRevoked": revoked},
    )
    db.session.commit()
    return jsonify({"status": "deleted", "id": target.id})


# --- per-admin feature grants (SUPER_ADMIN only) ---------------------------
#
# A SUPER_ADMIN can hand an individual ADMIN features that are hidden by default
# (screen wall, recordings, packages, audit, storage, endpoint disable/delete,
# group management). Managing grants is SUPER-only -- never delegated -- so a
# granted admin can never re-grant, even if granted a powerful feature.

def _require_super_admin() -> User:
    actor = require_current_user()
    if not actor.is_super_admin:
        audit.record_denied(CHANGE_ADMIN_FEATURES, reason="not super admin")
        db.session.commit()
        raise AuthorizationError("只有最高管理員可以調整功能授權。")
    return actor


def _require_grantable_target(user_id: str) -> User:
    from ..models.user import ROLE_ADMIN

    target = _load_user(user_id)
    if target.role != ROLE_ADMIN:
        raise ValidationError("功能授權只適用於一般管理員。")
    return target


@bp.get("/<user_id>/features")
@require_permission(USERS_READ)
def get_user_features(user_id: str):
    from ..models import AdminFeatureGrant
    from ..security.rbac import GRANTABLE_FEATURES, SENSITIVE_FEATURES

    _require_super_admin()
    target = _require_grantable_target(user_id)

    granted = {
        row.feature
        for row in db.session.query(AdminFeatureGrant.feature)
        .filter(AdminFeatureGrant.user_id == target.id)
        .all()
    }
    features = [
        {"key": key, "label": label,
         "granted": key in granted, "sensitive": key in SENSITIVE_FEATURES}
        for key, (label, _perms) in GRANTABLE_FEATURES.items()
    ]
    return jsonify({"userId": target.id, "features": features})


@bp.put("/<user_id>/features")
@require_permission(USERS_READ)
def set_user_features(user_id: str):
    from ..models import AdminFeatureGrant
    from ..security.rbac import GRANTABLE_FEATURES

    actor = _require_super_admin()
    target = _require_grantable_target(user_id)

    body = json_body()
    requested = body.get("features")
    if not isinstance(requested, list) or not all(isinstance(f, str) for f in requested):
        raise ValidationError("features 必須是字串陣列。")
    unknown = [f for f in requested if f not in GRANTABLE_FEATURES]
    if unknown:
        raise ValidationError(f"未知的功能：{', '.join(unknown)}。")
    wanted = set(requested)

    existing = {
        row.feature: row
        for row in db.session.query(AdminFeatureGrant)
        .filter(AdminFeatureGrant.user_id == target.id)
        .all()
    }
    for feature in wanted - set(existing):
        db.session.add(AdminFeatureGrant(user_id=target.id, feature=feature, granted_by=actor.id))
    for feature in set(existing) - wanted:
        db.session.delete(existing[feature])

    audit.record(
        CHANGE_ADMIN_FEATURES, target_type="user", target_id=target.id,
        metadata={"username": target.username, "features": sorted(wanted)},
    )
    db.session.commit()
    return get_user_features(user_id)
