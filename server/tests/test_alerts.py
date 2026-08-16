"""Alert center: raising from signals, dedup, delivery to channels, RBAC."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import Alert, Endpoint, db, utcnow
from app.services import alerting

from .conftest import auth_header
from .test_endpoints import create_enrollment_token, enroll


def _enroll(client, token):
    created = create_enrollment_token(client, token)
    return enroll(client, created["token"]).get_json()


def _hb(client, cred, **body):
    return client.post("/api/agent/heartbeat", json=body, headers=auth_header(cred))


def _open_alerts(client, super_admin_token, **params):
    from urllib.parse import urlencode

    q = ("?" + urlencode(params)) if params else ""
    return client.get("/api/alerts" + q, headers=auth_header(super_admin_token)).get_json()


# --- raising from signals ---------------------------------------------------

def test_low_disk_heartbeat_raises_then_resolves(client, super_admin_token):
    e = _enroll(client, super_admin_token)
    cred = e["deviceCredential"]

    _hb(client, cred, inventory={"diskTotalGb": 476, "diskFreeGb": 20, "diskFreePercent": 4})
    data = _open_alerts(client, super_admin_token, status="OPEN")
    disk = [a for a in data["items"] if a["type"] == "LOW_DISK"]
    assert len(disk) == 1
    assert disk[0]["endpointId"] == e["endpointId"]

    # Disk recovers -> the alert resolves, and does not linger open.
    _hb(client, cred, inventory={"diskTotalGb": 476, "diskFreeGb": 300, "diskFreePercent": 63})
    data = _open_alerts(client, super_admin_token, status="OPEN")
    assert not [a for a in data["items"] if a["type"] == "LOW_DISK"]


def test_low_disk_is_deduplicated(client, super_admin_token):
    e = _enroll(client, super_admin_token)
    cred = e["deviceCredential"]
    for _ in range(3):
        _hb(client, cred, inventory={"diskFreePercent": 3})
    data = _open_alerts(client, super_admin_token, status="OPEN")
    assert len([a for a in data["items"] if a["type"] == "LOW_DISK"]) == 1


def test_offline_evaluate_raises_then_resolves(client, super_admin_token, app):
    e = _enroll(client, super_admin_token)
    with app.app_context():
        ep = db.session.get(Endpoint, e["endpointId"])
        ep.last_seen_at = utcnow() - timedelta(hours=2)
        db.session.commit()

    client.post("/api/alerts/evaluate", headers=auth_header(super_admin_token))
    data = _open_alerts(client, super_admin_token, status="OPEN")
    assert [a for a in data["items"] if a["type"] == "OFFLINE"]

    with app.app_context():
        ep = db.session.get(Endpoint, e["endpointId"])
        ep.last_seen_at = utcnow()
        db.session.commit()
    client.post("/api/alerts/evaluate", headers=auth_header(super_admin_token))
    data = _open_alerts(client, super_admin_token, status="OPEN")
    assert not [a for a in data["items"] if a["type"] == "OFFLINE"]


def test_uninstall_attempt_raises_critical_alert(client, super_admin_token):
    e = _enroll(client, super_admin_token)
    client.post(
        "/api/agent/uninstall-attempt",
        json={"attempts": [{"outcome": "WRONG_PASSWORD", "localUser": "bob"}]},
        headers=auth_header(e["deviceCredential"]),
    )
    data = _open_alerts(client, super_admin_token, status="OPEN")
    tamper = [a for a in data["items"] if a["type"] == "UNINSTALL_ATTEMPT"]
    assert len(tamper) == 1
    assert tamper[0]["severity"] == "critical"


# --- delivery ---------------------------------------------------------------

def test_channel_receives_matching_alert_once(client, super_admin_token, app, monkeypatch):
    sent = []
    monkeypatch.setattr(alerting, "_deliver", lambda ch, alert: sent.append((ch.type, alert.type)))

    # A webhook channel at min_severity=warning.
    client.post(
        "/api/alert-channels",
        json={"name": "ops", "type": "webhook", "target": "https://example.com/hook",
              "minSeverity": "warning"},
        headers=auth_header(super_admin_token),
    )

    e = _enroll(client, super_admin_token)
    cred = e["deviceCredential"]
    _hb(client, cred, inventory={"diskFreePercent": 2})   # warning -> delivered
    _hb(client, cred, inventory={"diskFreePercent": 2})   # dedup -> NOT delivered again

    assert sent == [("webhook", "LOW_DISK")]


def test_channel_below_min_severity_is_not_notified(client, super_admin_token, app, monkeypatch):
    sent = []
    monkeypatch.setattr(alerting, "_deliver", lambda ch, alert: sent.append(alert.type))
    client.post(
        "/api/alert-channels",
        json={"name": "critical-only", "type": "webhook",
              "target": "https://example.com/hook", "minSeverity": "critical"},
        headers=auth_header(super_admin_token),
    )
    e = _enroll(client, super_admin_token)
    _hb(client, e["deviceCredential"], inventory={"diskFreePercent": 2})  # warning < critical
    assert sent == []


# --- acknowledge + RBAC -----------------------------------------------------

def test_acknowledge_alert(client, super_admin_token):
    e = _enroll(client, super_admin_token)
    _hb(client, e["deviceCredential"], inventory={"diskFreePercent": 1})
    alert_id = _open_alerts(client, super_admin_token, status="OPEN")["items"][0]["id"]

    r = client.post(f"/api/alerts/{alert_id}/acknowledge", headers=auth_header(super_admin_token))
    assert r.status_code == 200
    assert r.get_json()["status"] == "ACKNOWLEDGED"


def test_plain_admin_can_read_but_not_manage_channels(client, super_admin_token, plain_admin_token):
    assert client.get("/api/alerts", headers=auth_header(plain_admin_token)).status_code == 200
    assert client.get("/api/alert-channels", headers=auth_header(plain_admin_token)).status_code == 403
    assert client.post(
        "/api/alert-channels",
        json={"name": "x", "type": "webhook", "target": "https://e.com/h"},
        headers=auth_header(plain_admin_token),
    ).status_code == 403
