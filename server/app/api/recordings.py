"""Recording policies and segment index: /api/recordings (spec sections 14, 17, 23).

SUPER_ADMIN manages policies (turning recording on/off is a monitoring decision).
Listing an endpoint's segments is available to admins who can view that endpoint
(same scope as live screen viewing), and is audited.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import Endpoint, EndpointGroup, RecordingPolicy, RecordingSegment, db
from ..models.audit import (
    CHANGE_RECORDING_POLICY,
    DELETE_RECORDING_POLICY,
)
from ..models.recording import (
    MODE_DIFFERENTIAL,
    RECORDING_MODES,
    TARGET_ENDPOINT,
    TARGET_GROUP,
)
from ..models.storage import StorageTarget
from ..request_context import require_current_user
from ..security.authn import require_permission
from ..security.rbac import (
    ENDPOINTS_SCREEN_VIEW,
    RECORDINGS_MANAGE,
    can_access_endpoint,
)
from ..services import audit, recording_control
from .validation import get_int, get_str, json_body, paginated, pagination, query_datetime

bp = Blueprint("recordings", __name__, url_prefix="/api/recordings")

RECORDING_STATUS = "RECORDING_STATUS"


# --- policies (SUPER_ADMIN) -------------------------------------------------

@bp.get("/status")
@require_permission(RECORDINGS_MANAGE)
def recording_status():
    """Whether the server can record (key + FFmpeg present)."""
    return jsonify({
        "enabled": bool(current_app.config.get("RECORDING_ENABLED")),
        "segmentSeconds": current_app.config["RECORDING_SEGMENT_SECONDS"],
        "defaultFps": current_app.config["RECORDING_FPS"],
        "defaultRetentionDays": current_app.config["RECORDING_DEFAULT_RETENTION_DAYS"],
    })


@bp.get("/policies")
@require_permission(RECORDINGS_MANAGE)
def list_policies():
    rows = db.session.query(RecordingPolicy).order_by(RecordingPolicy.created_at.desc()).all()
    return jsonify({"items": [_policy_dict(p) for p in rows]})


@bp.post("/policies")
@require_permission(RECORDINGS_MANAGE)
def create_policy():
    if not current_app.config.get("RECORDING_ENABLED"):
        raise ConflictError(
            "錄影未啟用：請設定 EEM_RECORDING_KEY 並確認 FFmpeg 存在（見 check-config）。"
        )
    actor = require_current_user()
    body = json_body()

    target_type = get_str(body, "targetType", choices=(TARGET_ENDPOINT, TARGET_GROUP))
    target_id = get_str(body, "targetId", max_length=36)
    _validate_target(target_type, target_id)

    mode = get_str(body, "mode", required=False, default=MODE_DIFFERENTIAL, choices=RECORDING_MODES)
    fps = get_int(body, "fps", default=current_app.config["RECORDING_FPS"], minimum=1, maximum=15)
    retention = get_int(body, "retentionDays",
                        default=current_app.config["RECORDING_DEFAULT_RETENTION_DAYS"],
                        minimum=1, maximum=3650)
    storage_target_id = _validate_storage_target(body)
    label = get_str(body, "label", required=False, max_length=128)

    existing = db.session.query(RecordingPolicy).filter(
        RecordingPolicy.target_type == target_type,
        RecordingPolicy.target_id == target_id,
    ).first()
    if existing is not None:
        raise ConflictError("此對象已有錄影政策，請直接編輯。")

    policy = RecordingPolicy(
        target_type=target_type, target_id=target_id, mode=mode, fps=fps,
        retention_days=retention, enabled=True, storage_target_id=storage_target_id,
        label=label, created_by=actor.id,
    )
    db.session.add(policy)
    db.session.flush()

    audit.record(CHANGE_RECORDING_POLICY, target_type="recording_policy", target_id=policy.id,
                 metadata={"action": "create", "targetType": target_type, "targetId": target_id,
                           "mode": mode, "fps": fps, "retentionDays": retention,
                           "storageTargetId": storage_target_id})
    db.session.commit()

    _apply_policy_change(target_type, target_id)
    return jsonify(_policy_dict(policy)), 201


@bp.patch("/policies/<policy_id>")
@require_permission(RECORDINGS_MANAGE)
def update_policy(policy_id: str):
    policy = db.session.get(RecordingPolicy, policy_id)
    if policy is None:
        raise NotFoundError("找不到錄影政策。")
    body = json_body()
    changes: dict = {}

    if "enabled" in body:
        enabled = bool(body["enabled"])
        changes["enabled"] = {"from": policy.enabled, "to": enabled}
        policy.enabled = enabled
    mode = get_str(body, "mode", required=False, choices=RECORDING_MODES)
    if mode:
        changes["mode"] = {"from": policy.mode, "to": mode}
        policy.mode = mode
    if "fps" in body:
        policy.fps = get_int(body, "fps", minimum=1, maximum=15)
        changes["fps"] = policy.fps
    if "retentionDays" in body:
        policy.retention_days = get_int(body, "retentionDays", minimum=1, maximum=3650)
        changes["retentionDays"] = policy.retention_days
    if "storageTargetId" in body:
        new_target = _validate_storage_target(body)
        changes["storageTargetId"] = {"from": policy.storage_target_id, "to": new_target}
        policy.storage_target_id = new_target

    if not changes:
        raise ValidationError("沒有可更新的欄位。")

    audit.record(CHANGE_RECORDING_POLICY, target_type="recording_policy", target_id=policy.id,
                 metadata={"action": "update", "changes": changes})
    db.session.commit()

    _apply_policy_change(policy.target_type, policy.target_id)
    return jsonify(_policy_dict(policy))


@bp.delete("/policies/<policy_id>")
@require_permission(RECORDINGS_MANAGE)
def delete_policy(policy_id: str):
    policy = db.session.get(RecordingPolicy, policy_id)
    if policy is None:
        raise NotFoundError("找不到錄影政策。")
    target_type, target_id = policy.target_type, policy.target_id
    db.session.delete(policy)
    audit.record(DELETE_RECORDING_POLICY, target_type="recording_policy", target_id=policy_id,
                 metadata={"targetType": target_type, "targetId": target_id})
    db.session.commit()

    _apply_policy_change(target_type, target_id)
    return jsonify({"status": "deleted", "id": policy_id})


# --- segments (scoped to endpoint viewers) ---------------------------------

@bp.get("/endpoints/<endpoint_id>/segments")
@require_permission(ENDPOINTS_SCREEN_VIEW)
def list_segments(endpoint_id: str):
    """Recorded segments for an endpoint in a time range. Same scope as viewing."""
    actor = require_current_user()
    endpoint = db.session.get(Endpoint, endpoint_id)
    if endpoint is None or endpoint.is_deleted or not can_access_endpoint(actor, endpoint):
        raise NotFoundError("找不到端點。")

    offset, limit = pagination()
    query = db.session.query(RecordingSegment).filter(
        RecordingSegment.endpoint_id == endpoint_id
    )
    start = query_datetime("from")
    if start:
        query = query.filter(RecordingSegment.ended_at >= start)
    end = query_datetime("to")
    if end:
        query = query.filter(RecordingSegment.started_at <= end)

    total = query.count()
    rows = (
        query.order_by(RecordingSegment.started_at.desc()).offset(offset).limit(limit).all()
    )
    return jsonify(paginated([s.to_dict() for s in rows], total, offset, limit))


@bp.get("/segments/<segment_id>/video")
@require_permission(ENDPOINTS_SCREEN_VIEW)
def segment_video(segment_id: str):
    """Decrypt and stream one recording segment as MP4 (for playback).

    Same scope as live viewing, and every fetch is audited with the segment's
    time span -- so the trail shows precisely which recorded moments an admin
    replayed (sections 14, 17).
    """
    from flask import Response

    from ..models.audit import VIEW_RECORDING
    from ..models.base import iso
    from ..services.recording_crypto import decrypt_bytes, derive_key
    from ..services.storage import StorageError, load_file

    actor = require_current_user()
    segment = db.session.get(RecordingSegment, segment_id)
    if segment is None:
        raise NotFoundError("找不到錄影片段。")

    endpoint = db.session.get(Endpoint, segment.endpoint_id)
    if endpoint is None or endpoint.is_deleted or not can_access_endpoint(actor, endpoint):
        # Out of scope reads as not-found, like the rest of the endpoint API.
        raise NotFoundError("找不到錄影片段。")

    passphrase = current_app.config.get("RECORDING_KEY_PASSPHRASE")
    if not passphrase:
        raise ConflictError("此伺服器未設定錄影金鑰，無法解密回放。")

    app = current_app._get_current_object()
    try:
        ciphertext = load_file(app, "recordings", segment.filename, segment.storage_backend)
    except StorageError:
        raise NotFoundError("錄影檔已不存在（可能已逾保留期限刪除）。")

    try:
        plaintext = decrypt_bytes(derive_key(passphrase), ciphertext)
    except Exception as exc:  # noqa: BLE001 - never leak crypto detail
        raise ConflictError("無法解密此錄影片段。") from exc

    audit.record(
        VIEW_RECORDING,
        target_type="endpoint",
        target_id=endpoint.id,
        metadata={
            "segmentId": segment.id,
            "from": iso(segment.started_at),
            "to": iso(segment.ended_at),
            "deviceName": endpoint.device_name,
        },
    )
    db.session.commit()

    # Whole-file response (segments are small); one GET = one audit entry.
    return Response(
        plaintext,
        mimetype="video/mp4",
        headers={"Content-Disposition": "inline", "Cache-Control": "no-store"},
    )


# --- helpers ---------------------------------------------------------------

def _validate_target(target_type: str, target_id: str) -> None:
    if target_type == TARGET_ENDPOINT:
        ep = db.session.get(Endpoint, target_id)
        if ep is None or ep.is_deleted:
            raise ValidationError("找不到目標端點。")
    else:
        group = db.session.get(EndpointGroup, target_id)
        if group is None:
            raise ValidationError("找不到目標群組。")


def _validate_storage_target(body: dict) -> str | None:
    """Resolve the requested storage target id. Empty/absent -> None (本機磁碟)."""
    raw = body.get("storageTargetId")
    if raw in (None, "", "LOCAL"):
        return None
    if not isinstance(raw, str):
        raise ValidationError("storageTargetId 格式不正確。")
    target = db.session.get(StorageTarget, raw)
    if target is None:
        raise ValidationError("找不到指定的儲存目標。")
    return target.id


def _policy_dict(policy: RecordingPolicy) -> dict:
    name = None
    if policy.target_type == TARGET_ENDPOINT:
        ep = db.session.get(Endpoint, policy.target_id)
        name = ep.device_name if ep else None
    else:
        group = db.session.get(EndpointGroup, policy.target_id)
        name = group.name if group else None

    storage_name = None
    if policy.storage_target_id:
        st = db.session.get(StorageTarget, policy.storage_target_id)
        storage_name = st.name if st else "（已刪除的目標）"
    return policy.to_dict(target_name=name, storage_target_name=storage_name)


def _apply_policy_change(target_type: str, target_id: str) -> None:
    """Start/stop recorders for the endpoints this policy affects, right now."""
    from ..models import EndpointGroupMember

    app = current_app._get_current_object()
    if target_type == TARGET_ENDPOINT:
        endpoint_ids = [target_id]
    else:
        endpoint_ids = [
            row[0]
            for row in db.session.query(EndpointGroupMember.endpoint_id)
            .filter(EndpointGroupMember.group_id == target_id)
            .all()
        ]
    for endpoint_id in endpoint_ids:
        recording_control.refresh_endpoint(app, endpoint_id)
