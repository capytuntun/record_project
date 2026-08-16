"""Audit trail content and sanitized error responses (spec sections 17, 26, 27)."""

from __future__ import annotations

from app.models import AuditLog, db
from app.models.audit import CREATE_USER
from app.services.audit import scrub

from .conftest import SUPER_ADMIN_PASSWORD, auth_header, login


def test_audit_entry_captures_who_what_when_where(client, super_admin, super_admin_token):
    client.post(
        "/api/users",
        json={"username": "new.admin", "password": "N3w-Admin-Passw0rd!", "role": "ADMIN"},
        headers=auth_header(super_admin_token),
    )

    entry = db.session.query(AuditLog).filter(AuditLog.action == CREATE_USER).one()
    assert entry.actor_user_id == super_admin.id
    assert entry.actor_username == super_admin.username
    assert entry.target_type == "user"
    assert entry.timestamp is not None
    assert entry.source_ip is not None
    assert entry.request_id is not None
    assert entry.result == "SUCCESS"


def test_audit_metadata_never_stores_a_password():
    scrubbed = scrub(
        {
            "username": "someone",
            "password": "hunter2",
            "nested": {"refreshToken": "abc", "apiKey": "def", "safe": "kept"},
            "list": [{"sessionCookie": "xyz"}],
        }
    )

    assert scrubbed["username"] == "someone"
    assert scrubbed["password"] == "[REDACTED]"
    assert scrubbed["nested"]["refreshToken"] == "[REDACTED]"
    assert scrubbed["nested"]["apiKey"] == "[REDACTED]"
    assert scrubbed["nested"]["safe"] == "kept"
    assert scrubbed["list"][0]["sessionCookie"] == "[REDACTED]"


def test_no_audit_row_records_a_secret(client, super_admin_token):
    client.post(
        "/api/enrollment-tokens",
        json={"label": "batch", "maxUses": 1},
        headers=auth_header(super_admin_token),
    )
    created = client.get(
        "/api/enrollment-tokens", headers=auth_header(super_admin_token)
    ).get_json()
    assert created["items"]

    for row in db.session.query(AuditLog).all():
        blob = (row.metadata_json or "").lower()
        assert "password" not in blob or "[redacted]" in blob


def test_audit_log_has_no_write_route(client, super_admin_token):
    header = auth_header(super_admin_token)
    assert client.post("/api/audit-logs", json={}, headers=header).status_code == 405
    assert client.delete("/api/audit-logs", headers=header).status_code == 405


def test_error_response_carries_a_request_id_and_no_internals(client):
    response = client.get("/api/users/does-not-exist")
    assert response.status_code == 401

    body = response.get_json()
    assert set(body) >= {"error", "message", "requestId"}
    assert body["requestId"]

    serialized = str(body).lower()
    for leak in ("traceback", "sqlalchemy", "select ", "c:\\", "/app/"):
        assert leak not in serialized


def test_unknown_route_returns_json_not_html(client):
    response = client.get("/api/definitely-not-a-route")
    assert response.status_code == 404
    assert response.is_json
    assert response.get_json()["error"] == "not_found"


def test_malformed_json_is_rejected_cleanly(client, super_admin_token):
    response = client.post(
        "/api/users",
        data="not json at all",
        content_type="application/json",
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "validation_error"


def test_security_headers_are_present(client):
    response = client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Request-Id"]


def test_audit_log_filters(client, super_admin, super_admin_token):
    login(client, super_admin.username, SUPER_ADMIN_PASSWORD)

    filtered = client.get(
        "/api/audit-logs?action=LOGIN", headers=auth_header(super_admin_token)
    ).get_json()
    assert filtered["total"] >= 1
    assert all(item["action"] == "LOGIN" for item in filtered["items"])

    empty = client.get(
        "/api/audit-logs?action=NOT_A_REAL_ACTION", headers=auth_header(super_admin_token)
    ).get_json()
    assert empty["total"] == 0
