"""Endpoint groups and per-admin visibility assignment: /api/groups, /api/users/<id>/scope.

Groups are the unit of visibility: an admin assigned to a group sees that
group's endpoints. Individual INCLUDE/EXCLUDE exceptions layer on top. All of
this is SUPER_ADMIN only -- granting visibility is a privilege change (section 6).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import (
    AdminEndpointScope,
    AdminGroupAssignment,
    Endpoint,
    EndpointGroup,
    EndpointGroupMember,
    User,
    db,
)
from ..models.audit import (
    CHANGE_ADMIN_SCOPE,
    CHANGE_GROUP_MEMBERS,
    CREATE_GROUP,
    DELETE_GROUP,
    UPDATE_GROUP,
)
from ..models.user import ROLE_ADMIN, SCOPE_EXCLUDE, SCOPE_INCLUDE
from ..request_context import require_current_user
from ..security.authn import require_permission
from ..security.rbac import GROUPS_MANAGE, GROUPS_READ, endpoint_ids_in_scope
from ..services import audit
from .validation import get_str, json_body, paginated, pagination

bp = Blueprint("groups", __name__, url_prefix="/api")


# --- helpers ---------------------------------------------------------------

def _load_group(group_id: str) -> EndpointGroup:
    group = db.session.get(EndpointGroup, group_id)
    if group is None:
        raise NotFoundError("找不到群組。")
    return group


def _member_count(group_id: str) -> int:
    return (
        db.session.query(EndpointGroupMember)
        .filter(EndpointGroupMember.group_id == group_id)
        .count()
    )


def _id_list(body: dict, field: str) -> list[str]:
    """Read a JSON array of non-empty string ids."""
    value = body.get(field, [])
    if not isinstance(value, list):
        raise ValidationError(f"'{field}' 必須是陣列。")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"'{field}' 只能包含字串 id。")
        out.append(item.strip())
    return out


def _existing_endpoint_ids(ids: list[str]) -> set[str]:
    """Filter to endpoint ids that actually exist and are not deleted."""
    if not ids:
        return set()
    rows = (
        db.session.query(Endpoint.id)
        .filter(Endpoint.id.in_(ids), Endpoint.deleted_at.is_(None))
        .all()
    )
    return {row[0] for row in rows}


# --- groups CRUD -----------------------------------------------------------

@bp.get("/groups")
@require_permission(GROUPS_READ)
def list_groups():
    offset, limit = pagination()
    query = db.session.query(EndpointGroup).order_by(EndpointGroup.name)
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    items = [g.to_dict(member_count=_member_count(g.id)) for g in rows]
    return jsonify(paginated(items, total, offset, limit))


@bp.post("/groups")
@require_permission(GROUPS_MANAGE)
def create_group():
    body = json_body()
    name = get_str(body, "name", max_length=128)
    description = get_str(body, "description", required=False, max_length=512)

    if db.session.query(EndpointGroup).filter(EndpointGroup.name == name).first():
        raise ConflictError("已有同名群組。")

    actor = require_current_user()
    group = EndpointGroup(name=name, description=description, created_by=actor.id)
    db.session.add(group)
    db.session.flush()

    audit.record(CREATE_GROUP, target_type="group", target_id=group.id,
                 metadata={"name": name})
    db.session.commit()
    return jsonify(group.to_dict(member_count=0)), 201


@bp.get("/groups/<group_id>")
@require_permission(GROUPS_READ)
def get_group(group_id: str):
    group = _load_group(group_id)
    offline_after = _offline_after()
    members = (
        db.session.query(Endpoint)
        .join(EndpointGroupMember, EndpointGroupMember.endpoint_id == Endpoint.id)
        .filter(EndpointGroupMember.group_id == group_id, Endpoint.deleted_at.is_(None))
        .all()
    )
    data = group.to_dict(member_count=len(members))
    data["members"] = [e.to_dict(offline_after) for e in members]
    return jsonify(data)


@bp.patch("/groups/<group_id>")
@require_permission(GROUPS_MANAGE)
def update_group(group_id: str):
    group = _load_group(group_id)
    body = json_body()
    changes: dict = {}

    name = get_str(body, "name", required=False, max_length=128)
    if name and name != group.name:
        if db.session.query(EndpointGroup).filter(
            EndpointGroup.name == name, EndpointGroup.id != group_id
        ).first():
            raise ConflictError("已有同名群組。")
        changes["name"] = {"from": group.name, "to": name}
        group.name = name

    if "description" in body:
        description = get_str(body, "description", required=False, max_length=512)
        changes["description"] = True
        group.description = description

    if not changes:
        raise ValidationError("沒有可更新的欄位。")

    audit.record(UPDATE_GROUP, target_type="group", target_id=group.id,
                 metadata={"changes": changes})
    db.session.commit()
    return jsonify(group.to_dict(member_count=_member_count(group.id)))


@bp.delete("/groups/<group_id>")
@require_permission(GROUPS_MANAGE)
def delete_group(group_id: str):
    group = _load_group(group_id)
    # Assignments referencing this group must go too, or admins would keep a
    # dangling grant.
    removed_assignments = (
        db.session.query(AdminGroupAssignment)
        .filter(AdminGroupAssignment.group_id == group_id)
        .delete(synchronize_session=False)
    )
    db.session.delete(group)  # cascades to members

    audit.record(DELETE_GROUP, target_type="group", target_id=group_id,
                 metadata={"name": group.name, "assignmentsRemoved": removed_assignments})
    db.session.commit()
    return jsonify({"status": "deleted", "id": group_id})


@bp.put("/groups/<group_id>/members")
@require_permission(GROUPS_MANAGE)
def set_group_members(group_id: str):
    """Replace the group's membership with the given endpoint ids."""
    group = _load_group(group_id)
    body = json_body()
    wanted = _existing_endpoint_ids(_id_list(body, "endpointIds"))

    current = {
        row[0]
        for row in db.session.query(EndpointGroupMember.endpoint_id)
        .filter(EndpointGroupMember.group_id == group_id)
        .all()
    }
    to_add = wanted - current
    to_remove = current - wanted

    for endpoint_id in to_add:
        db.session.add(EndpointGroupMember(group_id=group_id, endpoint_id=endpoint_id))
    if to_remove:
        db.session.query(EndpointGroupMember).filter(
            EndpointGroupMember.group_id == group_id,
            EndpointGroupMember.endpoint_id.in_(to_remove),
        ).delete(synchronize_session=False)

    audit.record(CHANGE_GROUP_MEMBERS, target_type="group", target_id=group_id,
                 metadata={"name": group.name, "added": len(to_add), "removed": len(to_remove)})
    db.session.commit()
    return jsonify({"memberCount": len(wanted), "added": len(to_add), "removed": len(to_remove)})


# --- per-admin visibility --------------------------------------------------

@bp.get("/users/<user_id>/scope")
@require_permission(GROUPS_READ)
def get_admin_scope(user_id: str):
    user = db.session.get(User, user_id)
    if user is None or user.is_deleted:
        raise NotFoundError("找不到帳號。")

    group_ids = [
        row[0] for row in db.session.query(AdminGroupAssignment.group_id)
        .filter(AdminGroupAssignment.user_id == user_id).all()
    ]
    includes = [
        row[0] for row in db.session.query(AdminEndpointScope.endpoint_id)
        .filter(AdminEndpointScope.user_id == user_id,
                AdminEndpointScope.mode == SCOPE_INCLUDE).all()
    ]
    excludes = [
        row[0] for row in db.session.query(AdminEndpointScope.endpoint_id)
        .filter(AdminEndpointScope.user_id == user_id,
                AdminEndpointScope.mode == SCOPE_EXCLUDE).all()
    ]
    effective = endpoint_ids_in_scope(user)
    return jsonify({
        "userId": user_id,
        "role": user.role,
        "groupIds": group_ids,
        "includeEndpointIds": includes,
        "excludeEndpointIds": excludes,
        # None means unrestricted (SUPER_ADMIN).
        "effectiveEndpointCount": None if effective is None else len(effective),
    })


@bp.put("/users/<user_id>/scope")
@require_permission(GROUPS_MANAGE)
def set_admin_scope(user_id: str):
    """Replace an admin's group assignments and individual exceptions."""
    user = db.session.get(User, user_id)
    if user is None or user.is_deleted:
        raise NotFoundError("找不到帳號。")
    if user.role != ROLE_ADMIN:
        # SUPER_ADMIN already sees everything; scope is meaningless for it.
        raise ValidationError("只有一般管理員需要設定可見範圍。")

    body = json_body()
    group_ids = _id_list(body, "groupIds")
    includes = _existing_endpoint_ids(_id_list(body, "includeEndpointIds"))
    excludes = _existing_endpoint_ids(_id_list(body, "excludeEndpointIds"))

    valid_group_ids = {
        row[0] for row in db.session.query(EndpointGroup.id)
        .filter(EndpointGroup.id.in_(group_ids)).all()
    } if group_ids else set()

    # Replace assignments.
    db.session.query(AdminGroupAssignment).filter(
        AdminGroupAssignment.user_id == user_id
    ).delete(synchronize_session=False)
    for gid in valid_group_ids:
        db.session.add(AdminGroupAssignment(user_id=user_id, group_id=gid))

    # Replace individual exceptions.
    db.session.query(AdminEndpointScope).filter(
        AdminEndpointScope.user_id == user_id
    ).delete(synchronize_session=False)
    # An id can carry only one scope row (unique constraint). If it appears in
    # both lists, EXCLUDE wins -- matching the runtime rule in
    # endpoint_ids_in_scope where excludes are always subtracted last.
    for eid in includes - excludes:
        db.session.add(AdminEndpointScope(user_id=user_id, endpoint_id=eid, mode=SCOPE_INCLUDE))
    for eid in excludes:
        db.session.add(AdminEndpointScope(user_id=user_id, endpoint_id=eid, mode=SCOPE_EXCLUDE))

    db.session.flush()
    effective = endpoint_ids_in_scope(user)

    audit.record(
        CHANGE_ADMIN_SCOPE,
        target_type="user",
        target_id=user_id,
        metadata={
            "username": user.username,
            "groups": len(valid_group_ids),
            "includes": len(includes),
            "excludes": len(excludes - includes),
            "effectiveEndpoints": None if effective is None else len(effective),
        },
    )
    db.session.commit()
    return jsonify({
        "status": "updated",
        "groupIds": sorted(valid_group_ids),
        "effectiveEndpointCount": None if effective is None else len(effective),
    })


def _offline_after() -> int:
    from flask import current_app

    return current_app.config["OFFLINE_AFTER_SECONDS"]
