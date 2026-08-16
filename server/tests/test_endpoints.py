"""Enrollment, heartbeat, endpoint scope and status (spec sections 10, 11, 21)."""

from __future__ import annotations

from datetime import timedelta

from app.models import AdminEndpointScope, Endpoint, EnrollmentToken, db, utcnow

from .conftest import auth_header


def create_enrollment_token(client, token: str, **overrides) -> dict:
    """Create a token. Pass years=None to exercise the server's default period."""
    payload = {"label": "test-batch", "maxUses": 1, "days": 30}
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    response = client.post(
        "/api/enrollment-tokens", json=payload, headers=auth_header(token)
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def enroll(client, enrollment_token: str, **overrides) -> dict:
    payload = {
        "enrollmentToken": enrollment_token,
        "deviceName": "WS-001",
        "os": "Windows 11",
        "agentVersion": "0.1.0",
    }
    payload.update(overrides)
    return client.post("/api/agent/enroll", json=payload)


def test_enrollment_token_plaintext_is_returned_once_only(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    assert created["token"]

    listed = client.get(
        "/api/enrollment-tokens", headers=auth_header(super_admin_token)
    ).get_json()
    # Listing exposes metadata but never the token itself.
    assert all("token" not in item for item in listed["items"])


def test_enrollment_issues_a_unique_endpoint_id_and_credential(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)

    response = enroll(client, created["token"])
    assert response.status_code == 201

    body = response.get_json()
    assert body["endpointId"]
    assert body["deviceCredential"]
    # The id is server-issued, not the machine name.
    assert body["endpointId"] != "WS-001"


def test_a_single_use_token_cannot_enroll_twice(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token, maxUses=1)

    assert enroll(client, created["token"]).status_code == 201
    second = enroll(client, created["token"])
    assert second.status_code == 401
    # The holder of the token is told what is wrong so IT can act on it.
    assert second.get_json()["details"]["reason"] == "exhausted"


def test_expired_token_is_refused(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    record = db.session.get(EnrollmentToken, created["id"])
    record.expires_at = utcnow() - timedelta(minutes=1)
    db.session.commit()

    assert enroll(client, created["token"]).status_code == 401


def test_revoked_token_is_refused(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    client.post(
        f"/api/enrollment-tokens/{created['id']}/revoke",
        json={"reason": "test"},
        headers=auth_header(super_admin_token),
    )

    assert enroll(client, created["token"]).status_code == 401


def test_unknown_token_is_refused(client):
    assert enroll(client, "not-a-real-token-value-at-all").status_code == 401


def test_heartbeat_updates_last_seen_and_marks_endpoint_online(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    enrolled = enroll(client, created["token"]).get_json()

    response = client.post(
        "/api/agent/heartbeat",
        json={"agentVersion": "0.2.0"},
        headers=auth_header(enrolled["deviceCredential"]),
    )
    assert response.status_code == 200
    assert response.get_json()["endpointId"] == enrolled["endpointId"]

    listed = client.get(
        f"/api/endpoints/{enrolled['endpointId']}", headers=auth_header(super_admin_token)
    ).get_json()
    assert listed["status"] == "ONLINE"
    assert listed["agentVersion"] == "0.2.0"


def test_heartbeat_requires_a_valid_credential(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    enroll(client, created["token"])

    assert client.post("/api/agent/heartbeat", json={}).status_code == 401
    assert client.post(
        "/api/agent/heartbeat", json={}, headers=auth_header("forged-credential")
    ).status_code == 401


def test_stale_endpoint_reports_offline(client, super_admin_token, app):
    created = create_enrollment_token(client, super_admin_token)
    enrolled = enroll(client, created["token"]).get_json()

    record = db.session.get(Endpoint, enrolled["endpointId"])
    record.last_seen_at = utcnow() - timedelta(
        seconds=app.config["OFFLINE_AFTER_SECONDS"] + 60
    )
    db.session.commit()

    body = client.get(
        f"/api/endpoints/{enrolled['endpointId']}", headers=auth_header(super_admin_token)
    ).get_json()
    assert body["status"] == "OFFLINE"


def test_disabling_an_endpoint_revokes_its_credential(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    enrolled = enroll(client, created["token"]).get_json()

    response = client.post(
        f"/api/endpoints/{enrolled['endpointId']}/disable",
        json={"reason": "employee left"},
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "DISABLED"

    # The agent's credential stops working immediately.
    assert client.post(
        "/api/agent/heartbeat",
        json={},
        headers=auth_header(enrolled["deviceCredential"]),
    ).status_code == 401


def test_credential_rotation_invalidates_the_old_secret(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    enrolled = enroll(client, created["token"]).get_json()
    old = enrolled["deviceCredential"]

    rotated = client.post(
        "/api/agent/credential/rotate", headers=auth_header(old)
    ).get_json()
    new = rotated["deviceCredential"]
    assert new != old

    assert client.post(
        "/api/agent/heartbeat", json={}, headers=auth_header(new)
    ).status_code == 200
    assert client.post(
        "/api/agent/heartbeat", json={}, headers=auth_header(old)
    ).status_code == 401


def test_admin_sees_only_endpoints_in_scope(client, super_admin_token, plain_admin,
                                            plain_admin_token):
    first = create_enrollment_token(client, super_admin_token, label="a")
    in_scope = enroll(client, first["token"], deviceName="IN-SCOPE").get_json()

    second = create_enrollment_token(client, super_admin_token, label="b")
    out_of_scope = enroll(client, second["token"], deviceName="OUT-OF-SCOPE").get_json()

    db.session.add(
        AdminEndpointScope(user_id=plain_admin.id, endpoint_id=in_scope["endpointId"])
    )
    db.session.commit()

    listed = client.get("/api/endpoints", headers=auth_header(plain_admin_token)).get_json()
    ids = {item["id"] for item in listed["items"]}
    assert ids == {in_scope["endpointId"]}

    # A direct fetch of the out-of-scope id returns 404, not 403: the API does
    # not confirm that the id exists.
    assert client.get(
        f"/api/endpoints/{out_of_scope['endpointId']}",
        headers=auth_header(plain_admin_token),
    ).status_code == 404


def test_super_admin_sees_every_endpoint(client, super_admin_token):
    for label in ("a", "b"):
        created = create_enrollment_token(client, super_admin_token, label=label)
        enroll(client, created["token"])

    listed = client.get("/api/endpoints", headers=auth_header(super_admin_token)).get_json()
    assert listed["total"] == 2


def test_summary_counts_by_status(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    enroll(client, created["token"])

    summary = client.get(
        "/api/endpoints/summary", headers=auth_header(super_admin_token)
    ).get_json()
    assert summary["total"] == 1
    assert summary["online"] == 1
