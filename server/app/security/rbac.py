"""Role-based access control, enforced server-side (spec sections 6 and 7).

The permission matrix lives in code rather than in a database table: with a
fixed two-role model, keeping it here means a write to the database cannot
grant privileges, and the rules are reviewable in diff.
"""

from __future__ import annotations

from ..models import AdminEndpointScope, Endpoint, User, db
from ..models.user import ROLE_ADMIN, ROLE_SUPER_ADMIN

# --- Permissions -----------------------------------------------------------

USERS_READ = "users:read"
USERS_CREATE = "users:create"
USERS_UPDATE = "users:update"
USERS_DELETE = "users:delete"

ENDPOINTS_READ = "endpoints:read"
ENDPOINTS_UPDATE = "endpoints:update"
ENDPOINTS_DISABLE = "endpoints:disable"
ENDPOINTS_DELETE = "endpoints:delete"
ENDPOINTS_SCREEN_VIEW = "endpoints:screen:view"
ENDPOINTS_REMOTE_EXECUTE = "endpoints:remote:execute"

# Seeing the aggregate screen wall is separate from viewing one endpoint's screen,
# so it can be hidden from a plain admin (and granted back) independently.
SCREEN_WALL_VIEW = "screen:wall:view"
# Managing recording policies -- distinct from group management so it can be
# granted on its own.
RECORDINGS_MANAGE = "recordings:manage"

NETWORK_LOGS_READ = "network_logs:read"
AUDIT_LOGS_READ = "audit_logs:read"

ENROLLMENT_TOKENS_READ = "enrollment_tokens:read"
ENROLLMENT_TOKENS_CREATE = "enrollment_tokens:create"
ENROLLMENT_TOKENS_REVOKE = "enrollment_tokens:revoke"

PACKAGES_READ = "packages:read"
PACKAGES_CREATE = "packages:create"
PACKAGES_DOWNLOAD = "packages:download"

# Managing groups and the visibility assignments built on them is a privilege
# change, so it is SUPER_ADMIN only -- a regular admin must not be able to widen
# anyone's (or their own) visibility.
GROUPS_READ = "groups:read"
GROUPS_MANAGE = "groups:manage"

POLICIES_MANAGE = "policies:manage"
SYSTEM_SETTINGS_MANAGE = "system:settings:manage"

ALERTS_READ = "alerts:read"
ALERTS_MANAGE = "alerts:manage"

_ADMIN_PERMISSIONS = frozenset(
    {
        USERS_READ,
        USERS_CREATE,
        USERS_UPDATE,
        ENDPOINTS_READ,
        ENDPOINTS_SCREEN_VIEW,
        NETWORK_LOGS_READ,
        # Admins see and acknowledge alerts about the endpoints they manage;
        # configuring notification channels stays super-admin (ALERTS_MANAGE).
        ALERTS_READ,
    }
)

_SUPER_ADMIN_PERMISSIONS = frozenset(
    _ADMIN_PERMISSIONS
    | {
        USERS_DELETE,
        ENDPOINTS_UPDATE,
        ENDPOINTS_DISABLE,
        ENDPOINTS_DELETE,
        ENDPOINTS_REMOTE_EXECUTE,
        AUDIT_LOGS_READ,
        ENROLLMENT_TOKENS_READ,
        ENROLLMENT_TOKENS_CREATE,
        ENROLLMENT_TOKENS_REVOKE,
        # A built MSI contains a live enrollment token, so generating and
        # downloading one is as privileged as minting the token itself.
        PACKAGES_READ,
        PACKAGES_CREATE,
        PACKAGES_DOWNLOAD,
        GROUPS_READ,
        GROUPS_MANAGE,
        POLICIES_MANAGE,
        SYSTEM_SETTINGS_MANAGE,
        SCREEN_WALL_VIEW,
        RECORDINGS_MANAGE,
        ALERTS_MANAGE,
    }
)

PERMISSIONS_BY_ROLE: dict[str, frozenset[str]] = {
    ROLE_SUPER_ADMIN: _SUPER_ADMIN_PERMISSIONS,
    ROLE_ADMIN: _ADMIN_PERMISSIONS,
}

# --- Grantable features ----------------------------------------------------
# Things a plain ADMIN does NOT get by default, which a SUPER_ADMIN can hand to
# an individual admin. Each feature key maps to (label, permissions it unlocks).
# Managing these grants is SUPER_ADMIN-only (a granted admin can never re-grant),
# enforced in the API -- never via a grantable permission.
GRANTABLE_FEATURES: dict[str, tuple[str, tuple[str, ...]]] = {
    "wall":        ("螢幕牆", (SCREEN_WALL_VIEW,)),
    "recordings":  ("錄影", (RECORDINGS_MANAGE,)),
    "packages":    ("安裝包", (PACKAGES_READ, PACKAGES_CREATE, PACKAGES_DOWNLOAD,
                              ENROLLMENT_TOKENS_READ, ENROLLMENT_TOKENS_CREATE,
                              ENROLLMENT_TOKENS_REVOKE)),
    "audit":       ("稽核紀錄", (AUDIT_LOGS_READ,)),
    "storage":     ("儲存位置", (SYSTEM_SETTINGS_MANAGE,)),
    "endpoint_admin": ("端點停用／刪除", (ENDPOINTS_DISABLE, ENDPOINTS_DELETE)),
    # Group management lets its holder widen visibility (including their own),
    # so it is the most powerful grant -- surfaced with a warning in the UI.
    "groups":      ("群組管理", (GROUPS_READ, GROUPS_MANAGE)),
}

# Features whose grant lets the admin expand their own reach -- the UI warns.
SENSITIVE_FEATURES = frozenset({"groups", "storage", "packages"})


def granted_permissions_for(user: User) -> frozenset[str]:
    """Extra permissions an ADMIN has been individually granted (empty for others)."""
    if user.role != ROLE_ADMIN:
        return frozenset()
    from ..models import AdminFeatureGrant

    features = {
        row.feature
        for row in db.session.query(AdminFeatureGrant.feature)
        .filter(AdminFeatureGrant.user_id == user.id)
        .all()
    }
    perms: set[str] = set()
    for feature in features:
        entry = GRANTABLE_FEATURES.get(feature)
        if entry:
            perms.update(entry[1])
    return frozenset(perms)


def permissions_for(user: User) -> frozenset[str]:
    base = PERMISSIONS_BY_ROLE.get(user.role, frozenset())
    if user.role != ROLE_ADMIN:
        return base
    return base | granted_permissions_for(user)


def has_permission(user: User, permission: str) -> bool:
    return permission in permissions_for(user)


# --- Rules about acting on other users -------------------------------------


def can_manage_user(actor: User, target: User) -> tuple[bool, str]:
    """Whether ``actor`` may modify or delete ``target``.

    Returns (allowed, reason). Encodes spec section 6: a plain ADMIN can never
    touch a SUPER_ADMIN, and nobody may remove the last SUPER_ADMIN.
    """
    if not actor.is_super_admin and target.is_super_admin:
        return False, "Only a SUPER_ADMIN may modify a SUPER_ADMIN account."
    if not actor.is_super_admin and actor.id != target.id and not has_permission(actor, USERS_UPDATE):
        return False, "You may not modify this account."
    return True, ""


def can_assign_role(actor: User, role: str) -> tuple[bool, str]:
    """The SUPER_ADMIN role cannot be assigned through the API at all (section 6).

    There is exactly one SUPER_ADMIN: the account created at install time by
    ``flask bootstrap-super-admin``. Nobody -- including that SUPER_ADMIN -- can
    mint a second one or promote an existing admin, so the blast radius of a
    compromised console session is bounded: an attacker who takes over the
    console cannot manufacture a persistent peer account at the top of the tree.

    Note this is deliberately not "only a SUPER_ADMIN may grant it": the actor's
    role is irrelevant, the role itself is un-assignable.
    """
    if role == ROLE_SUPER_ADMIN:
        return False, "系統只允許一位最高管理員（安裝時建立），無法再指派此角色。"
    if role not in PERMISSIONS_BY_ROLE:
        return False, f"Unknown role: {role}"
    return True, ""


def can_reset_password(actor: User, target: User) -> tuple[bool, str]:
    """Whether ``actor`` may set ``target``'s password.

    Anyone may change their own (which the API additionally gates on knowing the
    current one). Setting *somebody else's* password is account takeover -- the
    holder can then sign in as that account -- so it is SUPER_ADMIN only, and is
    not reachable through the ``users:update`` permission a plain ADMIN holds.
    """
    if actor.id == target.id:
        return True, ""
    if not actor.is_super_admin:
        return False, "只有最高管理員可以重設其他帳號的密碼。"
    return True, ""


def count_active_super_admins(exclude_user_id: str | None = None) -> int:
    query = db.session.query(User).filter(
        User.role == ROLE_SUPER_ADMIN,
        User.deleted_at.is_(None),
        User.status == "ACTIVE",
    )
    if exclude_user_id:
        query = query.filter(User.id != exclude_user_id)
    return query.count()


def would_orphan_system(target: User) -> bool:
    """True when removing or demoting ``target`` leaves zero SUPER_ADMINs."""
    if not target.is_super_admin:
        return False
    return count_active_super_admins(exclude_user_id=target.id) == 0


# --- Endpoint scope --------------------------------------------------------


def endpoint_ids_in_scope(user: User) -> set[str] | None:
    """Endpoint ids this admin may act on. ``None`` means unrestricted.

    Computed as: (endpoints of the groups the admin is assigned to)
                 ∪ individual INCLUDE exceptions
                 − individual EXCLUDE exceptions.
    An EXCLUDE always wins over a group grant or an INCLUDE, so it can carve one
    endpoint out of an otherwise-granted group.
    """
    from ..models import (
        AdminGroupAssignment,
        EndpointGroupMember,
    )
    from ..models.user import SCOPE_EXCLUDE, SCOPE_INCLUDE

    if user.is_super_admin:
        return None

    # Endpoints granted via assigned groups.
    group_endpoints = {
        row[0]
        for row in db.session.query(EndpointGroupMember.endpoint_id)
        .join(AdminGroupAssignment, AdminGroupAssignment.group_id == EndpointGroupMember.group_id)
        .filter(AdminGroupAssignment.user_id == user.id)
        .all()
    }

    includes: set[str] = set()
    excludes: set[str] = set()
    for endpoint_id, mode in (
        db.session.query(AdminEndpointScope.endpoint_id, AdminEndpointScope.mode)
        .filter(AdminEndpointScope.user_id == user.id)
        .all()
    ):
        (excludes if mode == SCOPE_EXCLUDE else includes).add(endpoint_id)

    return (group_endpoints | includes) - excludes


def can_access_endpoint(user: User, endpoint: Endpoint) -> bool:
    scope = endpoint_ids_in_scope(user)
    return scope is None or endpoint.id in scope


def apply_endpoint_scope(query, user: User):
    """Narrow an Endpoint query to what ``user`` is allowed to see."""
    scope = endpoint_ids_in_scope(user)
    if scope is None:
        return query
    if not scope:
        # No scope assigned: return nothing rather than everything.
        return query.filter(Endpoint.id.is_(None))
    return query.filter(Endpoint.id.in_(scope))
