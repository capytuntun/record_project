"""Group-based visibility for admins (Phase 1): groups, assignments, exceptions."""

from __future__ import annotations

from app.models import (
    AdminEndpointScope,
    AdminGroupAssignment,
    AuditLog,
    EndpointGroup,
    EndpointGroupMember,
    db,
)
from app.models.audit import CHANGE_ADMIN_SCOPE, CREATE_GROUP
from app.models.user import SCOPE_EXCLUDE, SCOPE_INCLUDE

from .conftest import auth_header
from .test_endpoints import create_enrollment_token, enroll


def _enroll(client, super_admin_token, name):
    created = create_enrollment_token(client, super_admin_token, label=name)
    return enroll(client, created["token"], deviceName=name).get_json()["endpointId"]


# --- group CRUD ------------------------------------------------------------

def test_super_admin_creates_and_populates_a_group(client, super_admin_token):
    a = _enroll(client, super_admin_token, "PC-A")
    b = _enroll(client, super_admin_token, "PC-B")

    created = client.post("/api/groups", json={"name": "財務部"},
                          headers=auth_header(super_admin_token))
    assert created.status_code == 201
    gid = created.get_json()["id"]

    r = client.put(f"/api/groups/{gid}/members", json={"endpointIds": [a, b]},
                   headers=auth_header(super_admin_token))
    assert r.status_code == 200
    assert r.get_json()["memberCount"] == 2

    detail = client.get(f"/api/groups/{gid}", headers=auth_header(super_admin_token)).get_json()
    assert detail["memberCount"] == 2
    assert {m["id"] for m in detail["members"]} == {a, b}

    assert db.session.query(AuditLog).filter(AuditLog.action == CREATE_GROUP).count() == 1


def test_duplicate_group_name_is_rejected(client, super_admin_token):
    client.post("/api/groups", json={"name": "Dup"}, headers=auth_header(super_admin_token))
    again = client.post("/api/groups", json={"name": "Dup"}, headers=auth_header(super_admin_token))
    assert again.status_code == 409


def test_plain_admin_cannot_manage_groups(client, plain_admin_token, super_admin_token):
    # read is also super-admin only in this design
    assert client.get("/api/groups", headers=auth_header(plain_admin_token)).status_code == 403
    assert client.post("/api/groups", json={"name": "x"},
                       headers=auth_header(plain_admin_token)).status_code == 403


# --- group-based visibility ------------------------------------------------

def test_admin_sees_only_assigned_groups_endpoints(client, super_admin, super_admin_token,
                                                    plain_admin, plain_admin_token):
    a = _enroll(client, super_admin_token, "IN-A")
    b = _enroll(client, super_admin_token, "IN-B")
    c = _enroll(client, super_admin_token, "OUT-C")

    gid = client.post("/api/groups", json={"name": "G1"},
                      headers=auth_header(super_admin_token)).get_json()["id"]
    client.put(f"/api/groups/{gid}/members", json={"endpointIds": [a, b]},
               headers=auth_header(super_admin_token))

    # Assign the plain admin to the group.
    r = client.put(f"/api/users/{plain_admin.id}/scope",
                   json={"groupIds": [gid]}, headers=auth_header(super_admin_token))
    assert r.status_code == 200
    assert r.get_json()["effectiveEndpointCount"] == 2

    listed = client.get("/api/endpoints", headers=auth_header(plain_admin_token)).get_json()
    ids = {e["id"] for e in listed["items"]}
    assert ids == {a, b}
    # The out-of-group endpoint is not visible (404, not 403).
    assert client.get(f"/api/endpoints/{c}",
                      headers=auth_header(plain_admin_token)).status_code == 404


def test_individual_include_adds_beyond_groups(client, super_admin_token,
                                               plain_admin, plain_admin_token):
    a = _enroll(client, super_admin_token, "GRP-A")
    extra = _enroll(client, super_admin_token, "EXTRA")
    gid = client.post("/api/groups", json={"name": "G2"},
                      headers=auth_header(super_admin_token)).get_json()["id"]
    client.put(f"/api/groups/{gid}/members", json={"endpointIds": [a]},
               headers=auth_header(super_admin_token))

    client.put(f"/api/users/{plain_admin.id}/scope",
               json={"groupIds": [gid], "includeEndpointIds": [extra]},
               headers=auth_header(super_admin_token))

    listed = client.get("/api/endpoints", headers=auth_header(plain_admin_token)).get_json()
    assert {e["id"] for e in listed["items"]} == {a, extra}


def test_individual_exclude_removes_from_group(client, super_admin_token,
                                               plain_admin, plain_admin_token):
    a = _enroll(client, super_admin_token, "KEEP")
    b = _enroll(client, super_admin_token, "DROP")
    gid = client.post("/api/groups", json={"name": "G3"},
                      headers=auth_header(super_admin_token)).get_json()["id"]
    client.put(f"/api/groups/{gid}/members", json={"endpointIds": [a, b]},
               headers=auth_header(super_admin_token))

    # Group grants both, but exclude b.
    client.put(f"/api/users/{plain_admin.id}/scope",
               json={"groupIds": [gid], "excludeEndpointIds": [b]},
               headers=auth_header(super_admin_token))

    listed = client.get("/api/endpoints", headers=auth_header(plain_admin_token)).get_json()
    assert {e["id"] for e in listed["items"]} == {a}


def test_exclude_wins_over_include(client, super_admin_token, plain_admin, plain_admin_token):
    a = _enroll(client, super_admin_token, "CONFLICT")
    # Same endpoint as both include and exclude -> exclude wins, so invisible.
    client.put(f"/api/users/{plain_admin.id}/scope",
               json={"includeEndpointIds": [a], "excludeEndpointIds": [a]},
               headers=auth_header(super_admin_token))
    listed = client.get("/api/endpoints", headers=auth_header(plain_admin_token)).get_json()
    assert listed["total"] == 0


def test_setting_scope_replaces_previous(client, super_admin_token, plain_admin, plain_admin_token):
    a = _enroll(client, super_admin_token, "R-A")
    b = _enroll(client, super_admin_token, "R-B")
    g1 = client.post("/api/groups", json={"name": "R1"},
                     headers=auth_header(super_admin_token)).get_json()["id"]
    g2 = client.post("/api/groups", json={"name": "R2"},
                     headers=auth_header(super_admin_token)).get_json()["id"]
    client.put(f"/api/groups/{g1}/members", json={"endpointIds": [a]},
               headers=auth_header(super_admin_token))
    client.put(f"/api/groups/{g2}/members", json={"endpointIds": [b]},
               headers=auth_header(super_admin_token))

    client.put(f"/api/users/{plain_admin.id}/scope", json={"groupIds": [g1]},
               headers=auth_header(super_admin_token))
    client.put(f"/api/users/{plain_admin.id}/scope", json={"groupIds": [g2]},
               headers=auth_header(super_admin_token))

    listed = client.get("/api/endpoints", headers=auth_header(plain_admin_token)).get_json()
    assert {e["id"] for e in listed["items"]} == {b}  # only the second assignment remains


def test_deleting_a_group_removes_assignments(client, super_admin_token,
                                              plain_admin, plain_admin_token):
    a = _enroll(client, super_admin_token, "DEL-A")
    gid = client.post("/api/groups", json={"name": "ToDelete"},
                      headers=auth_header(super_admin_token)).get_json()["id"]
    client.put(f"/api/groups/{gid}/members", json={"endpointIds": [a]},
               headers=auth_header(super_admin_token))
    client.put(f"/api/users/{plain_admin.id}/scope", json={"groupIds": [gid]},
               headers=auth_header(super_admin_token))

    client.delete(f"/api/groups/{gid}", headers=auth_header(super_admin_token))

    assert db.session.query(AdminGroupAssignment).filter(
        AdminGroupAssignment.group_id == gid).count() == 0
    listed = client.get("/api/endpoints", headers=auth_header(plain_admin_token)).get_json()
    assert listed["total"] == 0


def test_scope_change_is_audited(client, super_admin_token, plain_admin):
    client.put(f"/api/users/{plain_admin.id}/scope", json={"groupIds": []},
               headers=auth_header(super_admin_token))
    entry = db.session.query(AuditLog).filter(AuditLog.action == CHANGE_ADMIN_SCOPE).one()
    assert entry.target_id == plain_admin.id


def test_cannot_set_scope_on_super_admin(client, super_admin, super_admin_token):
    r = client.put(f"/api/users/{super_admin.id}/scope", json={"groupIds": []},
                   headers=auth_header(super_admin_token))
    assert r.status_code == 400


def test_scope_view_reports_effective_count(client, super_admin_token, plain_admin):
    a = _enroll(client, super_admin_token, "V-A")
    gid = client.post("/api/groups", json={"name": "V1"},
                      headers=auth_header(super_admin_token)).get_json()["id"]
    client.put(f"/api/groups/{gid}/members", json={"endpointIds": [a]},
               headers=auth_header(super_admin_token))
    client.put(f"/api/users/{plain_admin.id}/scope", json={"groupIds": [gid]},
               headers=auth_header(super_admin_token))

    scope = client.get(f"/api/users/{plain_admin.id}/scope",
                       headers=auth_header(super_admin_token)).get_json()
    assert scope["groupIds"] == [gid]
    assert scope["effectiveEndpointCount"] == 1


def test_endpoint_list_shows_group_membership(client, super_admin_token):
    """#3: the endpoints list carries each endpoint's group names so an admin
    can see org and group at a glance."""
    a = _enroll(client, super_admin_token, "WS-A")
    gid = client.post("/api/groups", json={"name": "工程部"},
                      headers=auth_header(super_admin_token)).get_json()["id"]
    client.put(f"/api/groups/{gid}/members", json={"endpointIds": [a]},
               headers=auth_header(super_admin_token))

    items = client.get("/api/endpoints?pageSize=100",
                       headers=auth_header(super_admin_token)).get_json()["items"]
    row = next(e for e in items if e["id"] == a)
    assert row["groups"] == ["工程部"]
    other = next((e for e in items if e["id"] != a), None)
    if other is not None:
        assert other["groups"] == []
