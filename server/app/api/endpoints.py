"""Managed endpoint administration: /api/endpoints (spec sections 20, 21)."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..errors import NotFoundError, ValidationError
from ..models import Endpoint, EndpointCredential, db, utcnow
from ..models.audit import (
    DELETE_ENDPOINT,
    DISABLE_ENDPOINT,
    REVOKE_ENDPOINT_CREDENTIAL,
    UPDATE_ENDPOINT,
)
from ..models.endpoint import STATE_ACTIVE, STATE_DISABLED
from ..request_context import require_current_user
from ..security.authn import require_permission
from ..security.rbac import (
    ENDPOINTS_DELETE,
    ENDPOINTS_DISABLE,
    ENDPOINTS_READ,
    ENDPOINTS_UPDATE,
    apply_endpoint_scope,
    can_access_endpoint,
)
from ..services import audit
from .validation import get_str, json_body, paginated, pagination

bp = Blueprint("endpoints", __name__, url_prefix="/api/endpoints")


def _offline_after() -> int:
    return current_app.config["OFFLINE_AFTER_SECONDS"]


def _load_endpoint(endpoint_id: str) -> Endpoint:
    """Load an endpoint the caller is allowed to see.

    An out-of-scope endpoint returns 404 rather than 403 so the API does not
    confirm that an id exists to an admin who has no business knowing (IDOR,
    section 4.5).
    """
    actor = require_current_user()
    endpoint = db.session.get(Endpoint, endpoint_id)
    if endpoint is None or endpoint.is_deleted:
        raise NotFoundError("Endpoint not found.")
    if not can_access_endpoint(actor, endpoint):
        audit.record_denied(
            UPDATE_ENDPOINT,
            target_type="endpoint",
            target_id=endpoint_id,
            reason="out_of_scope",
        )
        db.session.commit()
        raise NotFoundError("Endpoint not found.")
    return endpoint


@bp.get("")
@require_permission(ENDPOINTS_READ)
def list_endpoints():
    actor = require_current_user()
    offset, limit = pagination()

    query = apply_endpoint_scope(
        db.session.query(Endpoint).filter(Endpoint.deleted_at.is_(None)), actor
    )

    org = request.args.get("organizationId", "").strip()
    if org:
        query = query.filter(Endpoint.organization_id == org)

    search = request.args.get("search", "").strip()
    if search:
        query = query.filter(Endpoint.device_name.like(f"%{search}%"))

    rows = query.order_by(Endpoint.last_seen_at.desc().nullslast()).all()
    offline_after = _offline_after()
    items = [row.to_dict(offline_after) for row in rows]

    # Status is derived from heartbeat age, so it is filtered after loading
    # rather than in SQL.
    status_filter = request.args.get("status", "").strip().upper()
    if status_filter:
        items = [item for item in items if item["status"] == status_filter]

    page_items = items[offset : offset + limit]
    page_ids = [item["id"] for item in page_items]
    # Flag which endpoints have something to play back / screenshots to browse,
    # so the console can disable the "回放" / "截圖" buttons when empty.
    available = _recording_available_ids(page_ids)
    with_shots = _endpoints_with_screenshots(page_ids)
    groups_map = _groups_for(page_ids)
    for item in page_items:
        item["recordingAvailable"] = item["id"] in available
        item["hasScreenshots"] = item["id"] in with_shots
        item["groups"] = groups_map.get(item["id"], [])

    total = len(items)
    return jsonify(paginated(page_items, total, offset, limit))


def _groups_for(endpoint_ids: list[str]) -> dict[str, list[str]]:
    """Map each endpoint id to the names of the groups it belongs to (for the
    endpoints list, so an admin can see an endpoint's org and groups at a glance)."""
    if not endpoint_ids:
        return {}
    from ..models import EndpointGroup, EndpointGroupMember

    rows = (
        db.session.query(EndpointGroupMember.endpoint_id, EndpointGroup.name)
        .join(EndpointGroup, EndpointGroup.id == EndpointGroupMember.group_id)
        .filter(EndpointGroupMember.endpoint_id.in_(endpoint_ids))
        .all()
    )
    out: dict[str, list[str]] = {}
    for endpoint_id, name in rows:
        out.setdefault(endpoint_id, []).append(name)
    return out


def _recording_available_ids(endpoint_ids: list[str]) -> set[str]:
    """Endpoints (from the given set) that have a recording policy covering them
    or any existing recorded segments -- i.e. something a viewer could replay."""
    from ..models import EndpointGroupMember, RecordingPolicy, RecordingSegment
    from ..models.recording import TARGET_ENDPOINT, TARGET_GROUP

    ids = set(endpoint_ids)
    if not ids:
        return set()
    available: set[str] = set()

    # Endpoint-level enabled policies.
    for (eid,) in (
        db.session.query(RecordingPolicy.target_id)
        .filter(RecordingPolicy.enabled.is_(True),
                RecordingPolicy.target_type == TARGET_ENDPOINT,
                RecordingPolicy.target_id.in_(ids))
        .all()
    ):
        available.add(eid)

    # Group-level enabled policies: any endpoint in such a group.
    group_ids = {
        gid for (gid,) in db.session.query(RecordingPolicy.target_id)
        .filter(RecordingPolicy.enabled.is_(True),
                RecordingPolicy.target_type == TARGET_GROUP).all()
    }
    if group_ids:
        for (eid,) in (
            db.session.query(EndpointGroupMember.endpoint_id)
            .filter(EndpointGroupMember.endpoint_id.in_(ids),
                    EndpointGroupMember.group_id.in_(group_ids))
            .all()
        ):
            available.add(eid)

    # Endpoints that already have recordings (policy may since have been removed).
    for (eid,) in (
        db.session.query(RecordingSegment.endpoint_id)
        .filter(RecordingSegment.endpoint_id.in_(ids))
        .distinct()
        .all()
    ):
        available.add(eid)

    return available


def _endpoints_with_screenshots(endpoint_ids: list[str]) -> set[str]:
    """Endpoints (from the given set) that have at least one saved screenshot."""
    from ..models import Screenshot

    ids = set(endpoint_ids)
    if not ids:
        return set()
    return {
        eid for (eid,) in db.session.query(Screenshot.endpoint_id)
        .filter(Screenshot.endpoint_id.in_(ids)).distinct().all()
    }


@bp.get("/summary")
@require_permission(ENDPOINTS_READ)
def summary():
    """Counts for the dashboard overview tile (spec section 20)."""
    actor = require_current_user()
    rows = apply_endpoint_scope(
        db.session.query(Endpoint).filter(Endpoint.deleted_at.is_(None)), actor
    ).all()

    offline_after = _offline_after()
    counts = {"total": len(rows), "online": 0, "offline": 0, "warning": 0, "disabled": 0,
              "unregistered": 0}
    for row in rows:
        key = row.status(offline_after).lower()
        counts[key] = counts.get(key, 0) + 1
    return jsonify(counts)


@bp.get("/<endpoint_id>")
@require_permission(ENDPOINTS_READ)
def get_endpoint(endpoint_id: str):
    endpoint = _load_endpoint(endpoint_id)
    from ..models.inventory import EndpointInventory

    detail = endpoint.to_dict(_offline_after())
    inv = db.session.get(EndpointInventory, endpoint.id)
    detail["inventory"] = inv.to_dict() if inv is not None else None
    return jsonify(detail)


@bp.get("/<endpoint_id>/status")
@require_permission(ENDPOINTS_READ)
def endpoint_status(endpoint_id: str):
    endpoint = _load_endpoint(endpoint_id)
    from ..models import iso

    return jsonify(
        {
            "id": endpoint.id,
            "status": endpoint.status(_offline_after()),
            "lastSeenAt": iso(endpoint.last_seen_at),
            "heartbeatIntervalSeconds": current_app.config["HEARTBEAT_INTERVAL_SECONDS"],
        }
    )


@bp.patch("/<endpoint_id>")
@require_permission(ENDPOINTS_UPDATE)
def update_endpoint(endpoint_id: str):
    endpoint = _load_endpoint(endpoint_id)
    body = json_body()
    changes: dict = {}

    label = get_str(body, "deviceName", required=False, max_length=128)
    if label is not None and label != endpoint.device_name:
        changes["deviceName"] = {"from": endpoint.device_name, "to": label}
        endpoint.device_name = label

    org = get_str(body, "organizationId", required=False, max_length=64)
    if org is not None and org != endpoint.organization_id:
        changes["organizationId"] = {"from": endpoint.organization_id, "to": org}
        endpoint.organization_id = org

    if not changes:
        raise ValidationError("No supported fields to update. Accepts 'deviceName' and 'organizationId'.")

    audit.record(
        UPDATE_ENDPOINT, target_type="endpoint", target_id=endpoint.id, metadata={"changes": changes}
    )
    db.session.commit()
    return jsonify(endpoint.to_dict(_offline_after()))


@bp.post("/<endpoint_id>/disable")
@require_permission(ENDPOINTS_DISABLE)
def disable_endpoint(endpoint_id: str):
    """Stop an endpoint from reporting, and revoke its credentials (section 19)."""
    endpoint = _load_endpoint(endpoint_id)
    body = json_body() if request.is_json else {}
    reason = get_str(body, "reason", required=False, max_length=255)

    endpoint.state = STATE_DISABLED
    revoked = _revoke_credentials(endpoint, "endpoint_disabled")

    audit.record(
        DISABLE_ENDPOINT,
        target_type="endpoint",
        target_id=endpoint.id,
        metadata={"reason": reason, "credentialsRevoked": revoked},
    )
    db.session.commit()
    return jsonify(endpoint.to_dict(_offline_after()))


@bp.post("/<endpoint_id>/enable")
@require_permission(ENDPOINTS_DISABLE)
def enable_endpoint(endpoint_id: str):
    """Re-enable a disabled endpoint.

    Credentials stay revoked: the agent must enroll again to get a new one.
    """
    endpoint = _load_endpoint(endpoint_id)
    endpoint.state = STATE_ACTIVE

    audit.record(
        UPDATE_ENDPOINT,
        target_type="endpoint",
        target_id=endpoint.id,
        metadata={"changes": {"state": {"from": STATE_DISABLED, "to": STATE_ACTIVE}},
                  "note": "re-enrollment required"},
    )
    db.session.commit()
    return jsonify(endpoint.to_dict(_offline_after()))


@bp.post("/<endpoint_id>/revoke-credentials")
@require_permission(ENDPOINTS_DISABLE)
def revoke_credentials(endpoint_id: str):
    endpoint = _load_endpoint(endpoint_id)
    revoked = _revoke_credentials(endpoint, "manual_revocation")

    audit.record(
        REVOKE_ENDPOINT_CREDENTIAL,
        target_type="endpoint",
        target_id=endpoint.id,
        metadata={"credentialsRevoked": revoked},
    )
    db.session.commit()
    return jsonify({"status": "revoked", "credentialsRevoked": revoked})


@bp.delete("/<endpoint_id>")
@require_permission(ENDPOINTS_DELETE)
def delete_endpoint(endpoint_id: str):
    """Soft delete, so historical audit entries still resolve the endpoint."""
    endpoint = _load_endpoint(endpoint_id)
    endpoint.deleted_at = utcnow()
    endpoint.state = STATE_DISABLED
    revoked = _revoke_credentials(endpoint, "endpoint_deleted")

    audit.record(
        DELETE_ENDPOINT,
        target_type="endpoint",
        target_id=endpoint.id,
        metadata={"deviceName": endpoint.device_name, "credentialsRevoked": revoked},
    )
    db.session.commit()
    return jsonify({"status": "deleted", "id": endpoint.id})


def _revoke_credentials(endpoint: Endpoint, reason: str) -> int:
    now = utcnow()
    live = (
        db.session.query(EndpointCredential)
        .filter(
            EndpointCredential.endpoint_id == endpoint.id,
            EndpointCredential.revoked_at.is_(None),
        )
        .all()
    )
    for credential in live:
        credential.revoked_at = now
        credential.revoked_reason = reason
    return len(live)
