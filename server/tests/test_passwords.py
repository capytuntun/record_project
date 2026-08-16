"""Password changes and the single-SUPER_ADMIN rule (spec sections 5, 6 and 8).

Two rules are enforced here, and both are server-side only -- the console hides
the corresponding buttons, but that is cosmetic:

  * anyone may change their own password, knowing the current one;
  * only the SUPER_ADMIN may set somebody else's, and the SUPER_ADMIN role can
    never be assigned through the API at all.
"""

from __future__ import annotations

from app.models import AuditLog, User, db
from app.models.audit import CHANGE_PASSWORD, RESULT_DENIED
from app.models.user import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.security.passwords import hash_password, verify_password

from .conftest import ADMIN_PASSWORD, SUPER_ADMIN_PASSWORD, auth_header, login

NEW_PASSWORD = "Rotated-Passw0rd!2026"


def _second_admin() -> User:
    user = User(
        username="other.admin",
        password_hash=hash_password("Other-Admin-Passw0rd!"),
        role=ROLE_ADMIN,
    )
    db.session.add(user)
    db.session.commit()
    return user


# --- changing your own password -------------------------------------------


def test_admin_can_change_own_password(client, plain_admin, plain_admin_token):
    response = client.post(
        f"/api/users/{plain_admin.id}/password",
        json={"currentPassword": ADMIN_PASSWORD, "newPassword": NEW_PASSWORD},
        headers=auth_header(plain_admin_token),
    )
    assert response.status_code == 200, response.get_json()

    refreshed = db.session.get(User, plain_admin.id)
    assert verify_password(refreshed.password_hash, NEW_PASSWORD)
    assert not verify_password(refreshed.password_hash, ADMIN_PASSWORD)

    # The new password works for a fresh login; the old one does not.
    login(client, "plain.admin", NEW_PASSWORD)
    rejected = client.post(
        "/api/auth/login", json={"username": "plain.admin", "password": ADMIN_PASSWORD}
    )
    assert rejected.status_code == 401


def test_super_admin_can_change_own_password(client, super_admin, super_admin_token):
    response = client.post(
        f"/api/users/{super_admin.id}/password",
        json={"currentPassword": SUPER_ADMIN_PASSWORD, "newPassword": NEW_PASSWORD},
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 200, response.get_json()
    login(client, "root.admin", NEW_PASSWORD)


def test_changing_own_password_revokes_existing_sessions(client, plain_admin, plain_admin_token):
    """A password change is a credential change: old tokens must stop working."""
    before = client.get("/api/auth/me", headers=auth_header(plain_admin_token))
    assert before.status_code == 200

    client.post(
        f"/api/users/{plain_admin.id}/password",
        json={"currentPassword": ADMIN_PASSWORD, "newPassword": NEW_PASSWORD},
        headers=auth_header(plain_admin_token),
    )

    after = client.get("/api/auth/me", headers=auth_header(plain_admin_token))
    assert after.status_code == 401


def test_wrong_current_password_is_refused_and_audited(client, plain_admin, plain_admin_token):
    response = client.post(
        f"/api/users/{plain_admin.id}/password",
        json={"currentPassword": "not-the-right-one", "newPassword": NEW_PASSWORD},
        headers=auth_header(plain_admin_token),
    )
    assert response.status_code == 403

    refreshed = db.session.get(User, plain_admin.id)
    assert verify_password(refreshed.password_hash, ADMIN_PASSWORD)

    denied = (
        db.session.query(AuditLog)
        .filter(AuditLog.action == CHANGE_PASSWORD, AuditLog.result == RESULT_DENIED)
        .count()
    )
    assert denied == 1


def test_new_password_must_meet_policy(client, plain_admin, plain_admin_token):
    response = client.post(
        f"/api/users/{plain_admin.id}/password",
        json={"currentPassword": ADMIN_PASSWORD, "newPassword": "short"},
        headers=auth_header(plain_admin_token),
    )
    assert response.status_code == 400

    refreshed = db.session.get(User, plain_admin.id)
    assert verify_password(refreshed.password_hash, ADMIN_PASSWORD)


# --- resetting somebody else's password ------------------------------------


def test_super_admin_can_reset_another_password(client, super_admin_token):
    target = _second_admin()

    response = client.post(
        f"/api/users/{target.id}/password",
        json={"newPassword": NEW_PASSWORD},
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 200, response.get_json()

    # No knowledge of the old password was needed, and the target can now log in.
    login(client, "other.admin", NEW_PASSWORD)

    entry = (
        db.session.query(AuditLog)
        .filter(AuditLog.action == CHANGE_PASSWORD, AuditLog.target_id == target.id)
        .one()
    )
    assert entry.result != RESULT_DENIED
    # The password itself must never reach the audit table.
    assert NEW_PASSWORD not in (entry.metadata_json or "")


def test_plain_admin_cannot_reset_another_admins_password(client, plain_admin_token):
    """users:update must not be a route to taking over a peer account."""
    target = _second_admin()

    response = client.post(
        f"/api/users/{target.id}/password",
        json={"newPassword": NEW_PASSWORD},
        headers=auth_header(plain_admin_token),
    )
    assert response.status_code == 403

    refreshed = db.session.get(User, target.id)
    assert not verify_password(refreshed.password_hash, NEW_PASSWORD)


def test_plain_admin_cannot_reset_the_super_admins_password(
    client, super_admin, plain_admin_token
):
    response = client.post(
        f"/api/users/{super_admin.id}/password",
        json={"newPassword": NEW_PASSWORD},
        headers=auth_header(plain_admin_token),
    )
    assert response.status_code == 403

    refreshed = db.session.get(User, super_admin.id)
    assert verify_password(refreshed.password_hash, SUPER_ADMIN_PASSWORD)


# --- exactly one SUPER_ADMIN ------------------------------------------------


def test_super_admin_cannot_create_another_super_admin(client, super_admin_token):
    """The role is un-assignable -- not merely restricted to SUPER_ADMINs."""
    response = client.post(
        "/api/users",
        json={
            "username": "second.super",
            "password": "Second-Sup3r-Passw0rd!",
            "role": ROLE_SUPER_ADMIN,
        },
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 403
    assert db.session.query(User).filter(User.username == "second.super").count() == 0


def test_super_admin_cannot_promote_an_admin(client, plain_admin, super_admin_token):
    response = client.patch(
        f"/api/users/{plain_admin.id}",
        json={"role": ROLE_SUPER_ADMIN},
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 403

    refreshed = db.session.get(User, plain_admin.id)
    assert refreshed.role == ROLE_ADMIN


def test_only_one_super_admin_exists_after_all_of_it(client, super_admin, super_admin_token):
    client.post(
        "/api/users",
        json={"username": "a.admin", "password": "A-Admin-Passw0rd!", "role": "SUPER_ADMIN"},
        headers=auth_header(super_admin_token),
    )
    client.post(
        "/api/users",
        json={"username": "b.admin", "password": "B-Admin-Passw0rd!", "role": "ADMIN"},
        headers=auth_header(super_admin_token),
    )

    supers = db.session.query(User).filter(User.role == ROLE_SUPER_ADMIN).all()
    assert [u.id for u in supers] == [super_admin.id]


def test_super_admin_can_still_be_demoted_is_blocked_as_last_one(client, super_admin, super_admin_token):
    """The single SUPER_ADMIN cannot demote themselves into an empty throne."""
    response = client.patch(
        f"/api/users/{super_admin.id}",
        json={"role": ROLE_ADMIN},
        headers=auth_header(super_admin_token),
    )
    # Self-role changes are refused outright, before the orphan check.
    assert response.status_code in (400, 403, 409)

    refreshed = db.session.get(User, super_admin.id)
    assert refreshed.role == ROLE_SUPER_ADMIN
