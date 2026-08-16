"""Calendar-based token validity, perpetual tokens, and endpoint-side warnings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api.agent import CREDENTIAL_WARN_WITHIN_DAYS
from app.models import EndpointCredential, EnrollmentToken, add_period, db, utcnow

from .conftest import auth_header
from .test_endpoints import create_enrollment_token, enroll


# --- calendar arithmetic ----------------------------------------------------


def test_add_period_handles_month_ends():
    jan31 = datetime(2026, 1, 31, tzinfo=timezone.utc)
    # One month from 31 January is 28 February, not 2 or 3 March.
    assert add_period(jan31, months=1) == datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert add_period(jan31, months=13) == datetime(2027, 2, 28, tzinfo=timezone.utc)


def test_add_period_handles_leap_years():
    feb29 = datetime(2028, 2, 29, tzinfo=timezone.utc)
    assert add_period(feb29, years=1) == datetime(2029, 2, 28, tzinfo=timezone.utc)
    assert add_period(feb29, years=4) == datetime(2032, 2, 29, tzinfo=timezone.utc)


def test_add_period_combines_units():
    start = datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert add_period(start, years=1, months=2, days=10) == datetime(
        2027, 3, 25, tzinfo=timezone.utc
    )


def test_add_period_rejects_negative():
    with pytest.raises(ValueError):
        add_period(utcnow(), days=-1)


# --- creating tokens with calendar validity ---------------------------------


def test_token_defaults_to_one_year(client, super_admin_token):
    """Sending no period at all falls back to the server's default."""
    response = client.post(
        "/api/enrollment-tokens",
        json={"label": "default-period"},
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 201
    created = response.get_json()

    assert created["neverExpires"] is False
    assert created["validity"] == "1 年"
    # Allow slack for leap years and the calendar boundary.
    assert 364 <= created["daysRemaining"] <= 367


def test_token_accepts_years_months_days(client, super_admin_token):
    response = client.post(
        "/api/enrollment-tokens",
        json={"label": "combo", "years": 1, "months": 6, "days": 0},
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["validity"] == "1 年 6 個月"
    assert 540 <= body["daysRemaining"] <= 550


def test_zero_period_is_rejected(client, super_admin_token):
    response = client.post(
        "/api/enrollment-tokens",
        json={"label": "nope", "years": 0, "months": 0, "days": 0},
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 400
    assert "至少" in response.get_json()["message"]


# --- perpetual tokens -------------------------------------------------------


def test_never_expiring_token_can_be_created_and_used(client, super_admin_token):
    response = client.post(
        "/api/enrollment-tokens",
        json={"label": "golden-image", "neverExpires": True, "maxUses": 0},
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 201
    created = response.get_json()

    assert created["neverExpires"] is True
    assert created["expiresAt"] is None
    assert created["unlimitedUses"] is True
    assert created["daysRemaining"] is None
    assert "永不過期" in created["warning"]

    # Unlimited means a second and third enrollment both work.
    for _ in range(3):
        assert enroll(client, created["token"]).status_code == 201


def test_never_expiring_token_survives_a_far_future_clock(client, super_admin_token):
    response = client.post(
        "/api/enrollment-tokens",
        json={"label": "perpetual", "neverExpires": True, "maxUses": 0},
        headers=auth_header(super_admin_token),
    )
    created = response.get_json()

    record = db.session.get(EnrollmentToken, created["id"])
    record.created_at = utcnow() - timedelta(days=365 * 20)
    db.session.commit()

    # No expiry means age is irrelevant.
    assert enroll(client, created["token"]).status_code == 201


def test_never_expiring_token_is_still_revocable(client, super_admin_token):
    """The control that replaces expiry has to actually work."""
    response = client.post(
        "/api/enrollment-tokens",
        json={"label": "perpetual", "neverExpires": True, "maxUses": 0},
        headers=auth_header(super_admin_token),
    )
    created = response.get_json()
    assert enroll(client, created["token"]).status_code == 201

    client.post(
        f"/api/enrollment-tokens/{created['id']}/revoke",
        json={"reason": "leaked"},
        headers=auth_header(super_admin_token),
    )

    failed = enroll(client, created["token"])
    assert failed.status_code == 401
    assert "撤銷" in failed.get_json()["message"]


def test_perpetual_token_creation_is_flagged_in_the_audit_log(client, super_admin_token):
    client.post(
        "/api/enrollment-tokens",
        json={"label": "perpetual", "neverExpires": True},
        headers=auth_header(super_admin_token),
    )
    from app.models import AuditLog
    from app.models.audit import CREATE_ENROLLMENT_TOKEN

    entry = (
        db.session.query(AuditLog)
        .filter(AuditLog.action == CREATE_ENROLLMENT_TOKEN)
        .one()
    )
    assert '"neverExpires": true' in entry.metadata_json


# --- the endpoint gets an actionable reason ---------------------------------


def test_expired_token_tells_the_endpoint_why(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    record = db.session.get(EnrollmentToken, created["id"])
    record.expires_at = utcnow() - timedelta(days=1)
    db.session.commit()

    response = enroll(client, created["token"])
    assert response.status_code == 401
    body = response.get_json()
    assert body["details"]["reason"] == "expired"
    assert "已過期" in body["message"]


def test_exhausted_token_tells_the_endpoint_why(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token, maxUses=1)
    assert enroll(client, created["token"]).status_code == 201

    body = enroll(client, created["token"]).get_json()
    assert body["details"]["reason"] == "exhausted"
    assert "次數" in body["message"]


def test_unknown_token_still_reveals_nothing(client):
    """Only a token the caller already holds gets a specific reason."""
    response = enroll(client, "definitely-not-a-real-token-value")
    assert response.status_code == 401
    body = response.get_json()
    assert "details" not in body
    assert "過期" not in body["message"]
    assert "撤銷" not in body["message"]


# --- credential expiry warning reaches the endpoint -------------------------


def test_heartbeat_reports_credential_lifetime(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    enrolled = enroll(client, created["token"]).get_json()

    body = client.post(
        "/api/agent/heartbeat", json={}, headers=auth_header(enrolled["deviceCredential"])
    ).get_json()

    assert body["credentialExpiresAt"]
    assert body["credentialDaysRemaining"] > CREDENTIAL_WARN_WITHIN_DAYS
    assert body["warnings"] == []


def test_heartbeat_warns_before_the_credential_expires(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    enrolled = enroll(client, created["token"]).get_json()

    credential = (
        db.session.query(EndpointCredential)
        .filter(EndpointCredential.endpoint_id == enrolled["endpointId"])
        .one()
    )
    credential.expires_at = utcnow() + timedelta(days=5)
    db.session.commit()

    body = client.post(
        "/api/agent/heartbeat", json={}, headers=auth_header(enrolled["deviceCredential"])
    ).get_json()

    assert len(body["warnings"]) == 1
    warning = body["warnings"][0]
    assert warning["code"] == "credential_expiring"
    assert warning["action"] == "rotate"
    assert warning["daysRemaining"] == 5
    # The message is what the agent shows on the machine.
    assert "5 天後到期" in warning["message"]


def test_rotating_clears_the_expiry_warning(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    enrolled = enroll(client, created["token"]).get_json()

    credential = (
        db.session.query(EndpointCredential)
        .filter(EndpointCredential.endpoint_id == enrolled["endpointId"])
        .one()
    )
    credential.expires_at = utcnow() + timedelta(days=3)
    db.session.commit()

    rotated = client.post(
        "/api/agent/credential/rotate", headers=auth_header(enrolled["deviceCredential"])
    ).get_json()

    body = client.post(
        "/api/agent/heartbeat", json={}, headers=auth_header(rotated["deviceCredential"])
    ).get_json()
    assert body["warnings"] == []
