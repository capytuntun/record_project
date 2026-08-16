"""Authentication, token rotation and revocation (spec section 5)."""

from __future__ import annotations

from app.models import AuditLog, db
from app.models.audit import LOGIN, LOGIN_FAILED, TOKEN_REUSE_DETECTED

from .conftest import ADMIN_PASSWORD, SUPER_ADMIN_PASSWORD, auth_header, login


def test_login_returns_tokens_and_records_audit(client, super_admin):
    body = login(client, super_admin.username, SUPER_ADMIN_PASSWORD)

    assert body["tokenType"] == "Bearer"
    assert body["accessToken"] and body["refreshToken"]
    assert body["user"]["role"] == "SUPER_ADMIN"
    assert "passwordHash" not in body["user"]
    assert "password_hash" not in str(body)

    entry = db.session.query(AuditLog).filter(AuditLog.action == LOGIN).one()
    assert entry.actor_user_id == super_admin.id


def test_login_with_wrong_password_is_rejected_and_audited(client, super_admin):
    response = client.post(
        "/api/auth/login",
        json={"username": super_admin.username, "password": "wrong-password"},
    )
    assert response.status_code == 401
    # The message must not distinguish a bad password from a missing account.
    assert response.get_json()["message"] == "Invalid username or password."

    entry = db.session.query(AuditLog).filter(AuditLog.action == LOGIN_FAILED).one()
    assert entry.result == "FAILURE"


def test_login_for_unknown_user_gives_the_same_error(client):
    response = client.post(
        "/api/auth/login", json={"username": "no.such.user", "password": "whatever-123"}
    )
    assert response.status_code == 401
    assert response.get_json()["message"] == "Invalid username or password."


def test_suspended_account_cannot_log_in(client, plain_admin):
    plain_admin.status = "SUSPENDED"
    db.session.commit()

    response = client.post(
        "/api/auth/login",
        json={"username": plain_admin.username, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 401


def test_me_requires_a_token(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers=auth_header("garbage")).status_code == 401


def test_refresh_rotates_the_token(client, super_admin):
    body = login(client, super_admin.username, SUPER_ADMIN_PASSWORD)
    first_refresh = body["refreshToken"]

    response = client.post("/api/auth/refresh", json={"refreshToken": first_refresh})
    assert response.status_code == 200
    rotated = response.get_json()

    assert rotated["refreshToken"] != first_refresh
    assert rotated["accessToken"]


def test_reusing_a_rotated_refresh_token_kills_the_whole_family(client, super_admin):
    body = login(client, super_admin.username, SUPER_ADMIN_PASSWORD)
    original = body["refreshToken"]

    rotated = client.post("/api/auth/refresh", json={"refreshToken": original}).get_json()

    # Replaying the consumed token is treated as theft.
    replay = client.post("/api/auth/refresh", json={"refreshToken": original})
    assert replay.status_code == 401

    # ...and the token issued from it is dead too.
    followup = client.post("/api/auth/refresh", json={"refreshToken": rotated["refreshToken"]})
    assert followup.status_code == 401

    assert db.session.query(AuditLog).filter(
        AuditLog.action == TOKEN_REUSE_DETECTED
    ).count() == 1


def test_logout_revokes_the_presented_refresh_token(client, super_admin):
    body = login(client, super_admin.username, SUPER_ADMIN_PASSWORD)

    response = client.post(
        "/api/auth/logout",
        json={"refreshToken": body["refreshToken"]},
        headers=auth_header(body["accessToken"]),
    )
    assert response.status_code == 200
    assert response.get_json()["revokedSessions"] == 1

    assert client.post(
        "/api/auth/refresh", json={"refreshToken": body["refreshToken"]}
    ).status_code == 401


def test_password_change_invalidates_existing_access_tokens(client, super_admin):
    body = login(client, super_admin.username, SUPER_ADMIN_PASSWORD)
    token = body["accessToken"]

    assert client.get("/api/auth/me", headers=auth_header(token)).status_code == 200

    response = client.post(
        f"/api/users/{super_admin.id}/password",
        json={"currentPassword": SUPER_ADMIN_PASSWORD, "newPassword": "An0ther-Str0ng-Pass!"},
        headers=auth_header(token),
    )
    assert response.status_code == 200

    # The token predates tokens_valid_from, so it stops working immediately.
    assert client.get("/api/auth/me", headers=auth_header(token)).status_code == 401


def test_weak_passwords_are_rejected(client, super_admin, super_admin_token):
    response = client.post(
        "/api/users",
        json={"username": "weak.user", "password": "short", "role": "ADMIN"},
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 400
    assert "12 characters" in response.get_json()["message"]
