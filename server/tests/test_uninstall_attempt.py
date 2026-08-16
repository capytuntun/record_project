"""Refused uninstall attempts reported by the agent (spec sections 16, 17, 19).

When someone at a managed endpoint tries to remove the agent and cannot supply
the administrator password, the MSI refuses and the agent forwards the attempt
here. It must land in the audit log as a DENIED entry so an administrator finds
out, and it must be attributable to the endpoint that reported it -- never to
one an attacker names.
"""

from __future__ import annotations

import json

from app.models import AuditLog, db
from app.models.audit import RESULT_DENIED, UNINSTALL_ATTEMPT

from .conftest import auth_header
from .test_endpoints import create_enrollment_token, enroll


def _enrolled(client, super_admin_token) -> tuple[str, str]:
    """Return (endpointId, deviceCredential) for a freshly enrolled endpoint."""
    created = create_enrollment_token(client, super_admin_token)
    body = enroll(client, created["token"]).get_json()
    return body["endpointId"], body["deviceCredential"]


def _report(client, credential: str, attempts: list) -> object:
    return client.post(
        "/api/agent/uninstall-attempt",
        json={"attempts": attempts},
        headers={"Authorization": f"Bearer {credential}"},
    )


def _entries() -> list[AuditLog]:
    return (
        db.session.query(AuditLog).filter(AuditLog.action == UNINSTALL_ATTEMPT).all()
    )


def test_a_refused_uninstall_is_written_to_the_audit_log(client, super_admin_token):
    endpoint_id, credential = _enrolled(client, super_admin_token)

    response = _report(client, credential, [
        {"at": "2026-08-16T05:30:00Z", "outcome": "WRONG_PASSWORD",
         "localUser": "WIN11-1\\user"},
    ])
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["count"] == 1

    entry = next(e for e in _entries())
    assert entry.result == RESULT_DENIED
    assert entry.target_id == endpoint_id
    assert entry.actor_type == "AGENT"

    metadata = json.loads(entry.metadata_json)
    assert metadata["outcome"] == "WRONG_PASSWORD"
    assert metadata["localUser"] == "WIN11-1\\user"


def test_every_wrong_password_becomes_its_own_entry(client, super_admin_token):
    """There is no attempt limit on the endpoint, so guessing must get noisier.

    One row per guess is the whole point: five wrong passwords is five entries,
    not one entry saying "five".
    """
    _, credential = _enrolled(client, super_admin_token)

    response = _report(client, credential, [
        {"outcome": "WRONG_PASSWORD", "localUser": "lab\\tester"} for _ in range(5)
    ])
    assert response.get_json()["count"] == 5
    assert len(_entries()) == 5


def test_a_cancelled_prompt_is_recorded_too(client, super_admin_token):
    _, credential = _enrolled(client, super_admin_token)

    _report(client, credential, [{"outcome": "CANCELLED"}])

    metadata = json.loads(_entries()[0].metadata_json)
    assert metadata["outcome"] == "CANCELLED"


def test_an_unknown_outcome_is_normalised(client, super_admin_token):
    """The endpoint is not trusted to define new outcome values."""
    _, credential = _enrolled(client, super_admin_token)

    _report(client, credential, [{"outcome": "SOMETHING_INVENTED"}])

    metadata = json.loads(_entries()[0].metadata_json)
    assert metadata["outcome"] == "WRONG_PASSWORD"


def test_the_report_is_attributed_to_the_presenting_credential(client, super_admin_token):
    """An endpoint cannot report an attempt against a different endpoint.

    Identity comes from the device credential, so nothing in the body can
    redirect the entry -- including an endpointId the caller invents.
    """
    first_id, first_credential = _enrolled(client, super_admin_token)
    second_id, _ = _enrolled(client, super_admin_token)
    assert first_id != second_id

    _report(client, first_credential, [
        {"outcome": "WRONG_PASSWORD", "endpointId": second_id, "target_id": second_id},
    ])

    entries = _entries()
    assert len(entries) == 1
    assert entries[0].target_id == first_id


def test_unauthenticated_reports_are_rejected(client):
    response = client.post(
        "/api/agent/uninstall-attempt", json={"attempts": [{"outcome": "WRONG_PASSWORD"}]}
    )
    assert response.status_code == 401
    assert _entries() == []


def test_a_flood_is_summarised_not_silently_dropped(client, super_admin_token):
    """Beyond the per-report cap, the remainder becomes one summary entry.

    Dropping them would understate the attack: an administrator would be told
    "20 attempts" when there were 45.
    """
    from app.api.agent import MAX_UNINSTALL_ATTEMPTS_PER_REPORT as CAP

    _, credential = _enrolled(client, super_admin_token)

    response = _report(client, credential,
                       [{"outcome": "WRONG_PASSWORD"} for _ in range(CAP + 25)])
    assert response.status_code == 200
    body = response.get_json()
    assert body["count"] == CAP
    assert body["suppressed"] == 25

    entries = _entries()
    assert len(entries) == CAP + 1          # the detailed ones plus the summary

    summary = json.loads(entries[-1].metadata_json)
    assert summary["suppressed"] == 25
    assert "未逐筆記錄" in summary["note"]


def test_a_non_list_body_is_rejected(client, super_admin_token):
    _, credential = _enrolled(client, super_admin_token)

    response = client.post(
        "/api/agent/uninstall-attempt",
        json={"attempts": "not-a-list"},
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 400
    assert _entries() == []


def test_malformed_entries_are_skipped_not_fatal(client, super_admin_token):
    _, credential = _enrolled(client, super_admin_token)

    response = _report(client, credential, ["a string", 42, None,
                                            {"outcome": "WRONG_PASSWORD"}])
    assert response.status_code == 200
    assert response.get_json()["count"] == 1


def test_the_attempt_shows_up_in_the_admin_audit_feed(client, super_admin_token):
    """It is only a notification if an administrator can actually see it."""
    _, credential = _enrolled(client, super_admin_token)
    _report(client, credential, [{"outcome": "WRONG_PASSWORD", "localUser": "lab\\tester"}])

    feed = client.get(
        f"/api/audit-logs?action={UNINSTALL_ATTEMPT}", headers=auth_header(super_admin_token)
    )
    assert feed.status_code == 200
    items = feed.get_json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == UNINSTALL_ATTEMPT
    assert items[0]["result"] == RESULT_DENIED


def test_unprotected_uninstall_is_recorded_and_alerts(client, super_admin_token, app):
    """A package with no uninstall password reports 'UNPROTECTED' at removal time,
    so it lands in the audit log AND raises an alert -- the case that previously
    produced no alert at all."""
    from app.models.alert import TYPE_UNINSTALL_ATTEMPT, Alert

    _, cred = _enrolled(client, super_admin_token)
    r = _report(client, cred, [{"outcome": "UNPROTECTED", "localUser": "eve"}])
    assert r.status_code == 200

    with app.app_context():
        entry = _entries()[-1]
        assert entry.result == RESULT_DENIED
        assert json.loads(entry.metadata_json)["outcome"] == "UNPROTECTED"
        assert db.session.query(Alert).filter(
            Alert.type == TYPE_UNINSTALL_ATTEMPT, Alert.status == "OPEN"
        ).count() == 1
