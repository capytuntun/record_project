"""Server-side authorization (spec sections 6 and 7).

These are the tests that matter most: a plain ADMIN must not be able to reach
SUPER_ADMIN accounts or privileged routes, whatever the frontend shows.
"""

from __future__ import annotations

from app.models import AuditLog, User, db
from app.models.audit import ACCESS_DENIED
from app.models.user import ROLE_ADMIN, ROLE_SUPER_ADMIN
from app.security.passwords import hash_password

from .conftest import ADMIN_PASSWORD, auth_header


def test_admin_cannot_delete_a_super_admin(client, super_admin, plain_admin_token):
    response = client.delete(
        f"/api/users/{super_admin.id}", headers=auth_header(plain_admin_token)
    )
    # USERS_DELETE is not in the ADMIN permission set at all.
    assert response.status_code == 403

    refreshed = db.session.get(User, super_admin.id)
    assert refreshed.deleted_at is None


def test_admin_cannot_create_a_super_admin(client, plain_admin_token):
    response = client.post(
        "/api/users",
        json={
            "username": "sneaky.super",
            "password": "Str0ng-Passw0rd!x",
            "role": "SUPER_ADMIN",
        },
        headers=auth_header(plain_admin_token),
    )
    assert response.status_code == 403
    assert db.session.query(User).filter(User.username == "sneaky.super").count() == 0


def test_admin_cannot_modify_a_super_admin(client, super_admin, plain_admin_token):
    response = client.patch(
        f"/api/users/{super_admin.id}",
        json={"status": "SUSPENDED"},
        headers=auth_header(plain_admin_token),
    )
    assert response.status_code == 403

    refreshed = db.session.get(User, super_admin.id)
    assert refreshed.status == "ACTIVE"


def test_admin_can_create_a_plain_admin(client, plain_admin_token):
    response = client.post(
        "/api/users",
        json={"username": "colleague", "password": "C0lleague-Passw0rd!", "role": "ADMIN"},
        headers=auth_header(plain_admin_token),
    )
    assert response.status_code == 201
    assert response.get_json()["role"] == ROLE_ADMIN


def test_admin_cannot_read_audit_logs(client, plain_admin_token):
    response = client.get("/api/audit-logs", headers=auth_header(plain_admin_token))
    assert response.status_code == 403


def test_super_admin_can_read_audit_logs(client, super_admin_token):
    response = client.get("/api/audit-logs", headers=auth_header(super_admin_token))
    assert response.status_code == 200
    assert "items" in response.get_json()


def test_admin_cannot_create_enrollment_tokens(client, plain_admin_token):
    response = client.post(
        "/api/enrollment-tokens",
        json={"label": "unauthorized"},
        headers=auth_header(plain_admin_token),
    )
    assert response.status_code == 403


def test_permission_denial_is_written_to_the_audit_log(client, plain_admin_token):
    client.get("/api/audit-logs", headers=auth_header(plain_admin_token))

    entry = db.session.query(AuditLog).filter(AuditLog.action == ACCESS_DENIED).one()
    assert entry.result == "DENIED"
    assert "audit_logs:read" in (entry.target_id or "")


def test_the_last_super_admin_cannot_be_deleted(client, super_admin, super_admin_token):
    other = User(
        username="second.super",
        password_hash=hash_password("Second-Sup3r-Pass!"),
        role=ROLE_SUPER_ADMIN,
    )
    db.session.add(other)
    db.session.commit()

    # Two exist, so removing one is fine.
    assert client.delete(
        f"/api/users/{other.id}", headers=auth_header(super_admin_token)
    ).status_code == 200

    # The remaining one cannot delete itself, and no one else can either.
    response = client.delete(
        f"/api/users/{super_admin.id}", headers=auth_header(super_admin_token)
    )
    assert response.status_code == 400  # self-deletion is refused first


def test_the_last_super_admin_cannot_be_demoted(client, super_admin, super_admin_token):
    other = User(
        username="second.super",
        password_hash=hash_password("Second-Sup3r-Pass!"),
        role=ROLE_SUPER_ADMIN,
    )
    db.session.add(other)
    db.session.commit()

    # Demote the second one: allowed, one SUPER_ADMIN remains.
    assert client.patch(
        f"/api/users/{other.id}",
        json={"role": "ADMIN"},
        headers=auth_header(super_admin_token),
    ).status_code == 200

    # Now demoting the last one would leave the system with none.
    response = client.patch(
        f"/api/users/{super_admin.id}",
        json={"role": "ADMIN"},
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 400  # self-role-change is refused


def test_role_change_forces_reauthentication(client, super_admin, super_admin_token):
    """Demotion must kill the demoted account's tokens immediately.

    Promotion is no longer reachable -- SUPER_ADMIN is un-assignable, so the
    only role change left is demoting a surplus SUPER_ADMIN from a deployment
    that predates that rule. That is exactly the case this exercises, so the
    second account is inserted directly rather than through the API.
    """
    surplus_password = "Surplus-Sup3r-Pass!"
    surplus = User(
        username="surplus.super",
        password_hash=hash_password(surplus_password),
        role=ROLE_SUPER_ADMIN,
    )
    db.session.add(surplus)
    db.session.commit()

    body = client.post(
        "/api/auth/login",
        json={"username": surplus.username, "password": surplus_password},
    ).get_json()
    surplus_token = body["accessToken"]
    assert client.get("/api/auth/me", headers=auth_header(surplus_token)).status_code == 200

    assert client.patch(
        f"/api/users/{surplus.id}",
        json={"role": ROLE_ADMIN},
        headers=auth_header(super_admin_token),
    ).status_code == 200

    # The old token carried role=SUPER_ADMIN; it must not survive the demotion.
    assert client.get("/api/auth/me", headers=auth_header(surplus_token)).status_code == 401
