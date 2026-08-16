"""Per-admin feature grants: a SUPER_ADMIN hands a plain ADMIN features that are
hidden by default, and only a SUPER_ADMIN may do so (never delegated)."""

from __future__ import annotations

from app.models import AuditLog, db
from app.models.audit import CHANGE_ADMIN_FEATURES
from app.security.rbac import (
    AUDIT_LOGS_READ,
    ENDPOINTS_DELETE,
    GROUPS_MANAGE,
    PACKAGES_CREATE,
    RECORDINGS_MANAGE,
    SCREEN_WALL_VIEW,
    SYSTEM_SETTINGS_MANAGE,
    permissions_for,
)

from .conftest import auth_header

HIDDEN = [SCREEN_WALL_VIEW, RECORDINGS_MANAGE, AUDIT_LOGS_READ,
          SYSTEM_SETTINGS_MANAGE, ENDPOINTS_DELETE, GROUPS_MANAGE, PACKAGES_CREATE]


def _put_features(client, token, user_id, features):
    return client.put(f"/api/users/{user_id}/features", json={"features": features},
                      headers=auth_header(token))


def test_admin_lacks_hidden_features_by_default(app, plain_admin):
    perms = permissions_for(plain_admin)
    for p in HIDDEN:
        assert p not in perms, p
    # ...but keeps the base admin capabilities.
    assert "endpoints:read" in perms
    assert "endpoints:screen:view" in perms


def test_grant_adds_permissions_and_revoke_removes(app, client, super_admin_token, plain_admin):
    r = _put_features(client, super_admin_token, plain_admin.id, ["wall", "recordings"])
    assert r.status_code == 200
    perms = permissions_for(plain_admin)
    assert SCREEN_WALL_VIEW in perms and RECORDINGS_MANAGE in perms
    assert ENDPOINTS_DELETE not in perms          # only what was granted

    # endpoint_admin unlocks disable + delete
    _put_features(client, super_admin_token, plain_admin.id, ["endpoint_admin"])
    perms = permissions_for(plain_admin)
    assert ENDPOINTS_DELETE in perms and "endpoints:disable" in perms
    assert SCREEN_WALL_VIEW not in perms          # wall was dropped

    # revoke everything
    assert _put_features(client, super_admin_token, plain_admin.id, []).status_code == 200
    perms = permissions_for(plain_admin)
    for p in HIDDEN:
        assert p not in perms


def test_me_reflects_grant_live(client, super_admin_token, plain_admin, plain_admin_token):
    before = client.get("/api/auth/me", headers=auth_header(plain_admin_token)).get_json()
    assert SCREEN_WALL_VIEW not in before["permissions"]

    _put_features(client, super_admin_token, plain_admin.id, ["wall"])

    after = client.get("/api/auth/me", headers=auth_header(plain_admin_token)).get_json()
    assert SCREEN_WALL_VIEW in after["permissions"]   # same token, updated perms


def test_granting_is_super_admin_only(client, plain_admin, plain_admin_token):
    assert client.get(f"/api/users/{plain_admin.id}/features",
                      headers=auth_header(plain_admin_token)).status_code == 403
    assert _put_features(client, plain_admin_token, plain_admin.id, ["wall"]).status_code == 403


def test_granted_admin_cannot_regrant(client, super_admin_token, plain_admin, plain_admin_token):
    # Give the admin the storage feature (system:settings:manage).
    _put_features(client, super_admin_token, plain_admin.id, ["storage"])
    assert SYSTEM_SETTINGS_MANAGE in permissions_for(plain_admin)
    # Even so, they cannot manage feature grants -- that is SUPER-only, never delegated.
    assert _put_features(client, plain_admin_token, plain_admin.id, ["wall"]).status_code == 403


def test_unknown_feature_rejected(client, super_admin_token, plain_admin):
    assert _put_features(client, super_admin_token, plain_admin.id, ["nonsense"]).status_code == 400


def test_target_must_be_plain_admin(client, super_admin_token, super_admin):
    # Cannot set feature grants on a SUPER_ADMIN (they already have everything).
    assert _put_features(client, super_admin_token, super_admin.id, ["wall"]).status_code == 400


def test_grant_is_audited(client, super_admin_token, plain_admin):
    _put_features(client, super_admin_token, plain_admin.id, ["wall", "audit"])
    assert db.session.query(AuditLog).filter(
        AuditLog.action == CHANGE_ADMIN_FEATURES,
        AuditLog.target_id == plain_admin.id).count() == 1
