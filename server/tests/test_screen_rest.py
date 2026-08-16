"""Screen-view REST control plane: tickets, audit, scope (spec section 14)."""

from __future__ import annotations

from app.models import AuditLog, ScreenSession, db
from app.models.audit import VIEW_SCREEN

from .conftest import auth_header
from .test_endpoints import create_enrollment_token, enroll


def _enroll_one(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    return enroll(client, created["token"]).get_json()


def test_issuing_a_ticket_starts_a_session_and_audits_it(client, super_admin, super_admin_token):
    endpoint = _enroll_one(client, super_admin_token)

    response = client.post(
        f"/api/endpoints/{endpoint['endpointId']}/screen/ticket",
        headers=auth_header(super_admin_token),
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ticket"]
    assert body["wsPath"].startswith(f"/api/endpoints/{endpoint['endpointId']}/screen/ws?ticket=")
    # No agent is connected in this test, so the console is told to wait.
    assert body["agentOnline"] is False

    session = db.session.get(ScreenSession, body["sessionId"])
    assert session.viewer_user_id == super_admin.id
    assert session.ended_at is None

    entry = db.session.query(AuditLog).filter(AuditLog.action == VIEW_SCREEN).one()
    assert entry.target_id == endpoint["endpointId"]
    assert entry.actor_user_id == super_admin.id
    assert entry.source_ip is not None


def test_admin_needs_the_screen_permission(client, plain_admin, plain_admin_token,
                                            super_admin_token):
    # plain ADMIN has endpoints:screen:view, so grant it scope first.
    endpoint = _enroll_one(client, super_admin_token)
    from app.models import AdminEndpointScope
    db.session.add(
        AdminEndpointScope(user_id=plain_admin.id, endpoint_id=endpoint["endpointId"])
    )
    db.session.commit()

    ok = client.post(
        f"/api/endpoints/{endpoint['endpointId']}/screen/ticket",
        headers=auth_header(plain_admin_token),
    )
    assert ok.status_code == 200


def test_out_of_scope_endpoint_is_not_viewable(client, plain_admin_token, super_admin_token):
    endpoint = _enroll_one(client, super_admin_token)

    # plain admin has no scope entry for this endpoint.
    response = client.post(
        f"/api/endpoints/{endpoint['endpointId']}/screen/ticket",
        headers=auth_header(plain_admin_token),
    )
    assert response.status_code == 404  # not 403 -- do not confirm the id


def test_unauthenticated_cannot_get_a_ticket(client, super_admin_token):
    endpoint = _enroll_one(client, super_admin_token)
    assert client.post(
        f"/api/endpoints/{endpoint['endpointId']}/screen/ticket"
    ).status_code == 401


def test_sessions_list_shows_the_audit_trail(client, super_admin_token):
    endpoint = _enroll_one(client, super_admin_token)
    for _ in range(3):
        client.post(
            f"/api/endpoints/{endpoint['endpointId']}/screen/ticket",
            headers=auth_header(super_admin_token),
        )

    listed = client.get(
        f"/api/endpoints/{endpoint['endpointId']}/screen/sessions",
        headers=auth_header(super_admin_token),
    ).get_json()
    assert listed["total"] == 3
    assert all(s["viewerUsername"] for s in listed["items"])


def test_a_ticket_is_single_use(client, super_admin_token):
    endpoint = _enroll_one(client, super_admin_token)
    ticket = client.post(
        f"/api/endpoints/{endpoint['endpointId']}/screen/ticket",
        headers=auth_header(super_admin_token),
    ).get_json()["ticket"]

    from app.services.screen_tickets import tickets

    first = tickets.redeem(ticket)
    assert first is not None
    assert first.endpoint_id == endpoint["endpointId"]
    # Second redemption fails: the ticket was consumed.
    assert tickets.redeem(ticket) is None
