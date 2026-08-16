"""Screenshots: capture a still frame from an endpoint and store it (sections 14, 17).

Capturing is available to admins who can view that endpoint (same scope and
permission as live screen viewing) -- a screenshot is just a saved copy of a
frame they are already authorised to see. Every capture, view and delete is
audited, and the image bytes are AES-encrypted on disk, never in the database.
"""

from __future__ import annotations

import hashlib
import uuid

from flask import Blueprint, Response, current_app, jsonify, request

from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import Endpoint, Screenshot, db, utcnow
from ..models.audit import DELETE_SCREENSHOT, TAKE_SCREENSHOT, VIEW_SCREENSHOT
from ..models.base import add_period
from ..request_context import client_ip, require_current_user
from ..security.authn import require_permission
from ..security.rbac import ENDPOINTS_SCREEN_VIEW, can_access_endpoint
from ..services import audit
from ..services.recording_crypto import decrypt_bytes, derive_key, encrypt_bytes
from ..services.storage import (
    StorageError,
    default_target_id,
    load_file,
    remove_file,
    store_file,
)
from .validation import paginated, pagination

bp = Blueprint("screenshots", __name__, url_prefix="/api")

# JPEG magic number; we only accept JPEG frames (that is what the viewer renders).
_JPEG_MAGIC = b"\xff\xd8\xff"


def _require_viewable_endpoint(endpoint_id: str) -> Endpoint:
    actor = require_current_user()
    endpoint = db.session.get(Endpoint, endpoint_id)
    if endpoint is None or endpoint.is_deleted or not can_access_endpoint(actor, endpoint):
        # Out of scope reads as not-found, like the rest of the endpoint API.
        raise NotFoundError("找不到端點。")
    return endpoint


@bp.post("/endpoints/<endpoint_id>/screenshot")
@require_permission(ENDPOINTS_SCREEN_VIEW)
def capture_screenshot(endpoint_id: str):
    """Save a JPEG frame (raw bytes in the request body) as an encrypted screenshot."""
    endpoint = _require_viewable_endpoint(endpoint_id)

    passphrase = current_app.config.get("RECORDING_KEY_PASSPHRASE")
    if not passphrase:
        raise ConflictError("此伺服器未設定畫面加密金鑰，無法儲存截圖。")

    data = request.get_data(cache=False)
    if not data:
        raise ValidationError("沒有收到截圖影像。")
    max_bytes = current_app.config["SCREENSHOT_MAX_BYTES"]
    if len(data) > max_bytes:
        raise ValidationError("截圖影像過大。")
    if not data.startswith(_JPEG_MAGIC):
        raise ValidationError("截圖格式必須為 JPEG。")

    monitor_index = request.args.get("monitor", type=int)

    now = utcnow()
    name = f"{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.jpg.enc"
    rel_path = f"{endpoint.id}/{name}"

    # Encrypt, then publish the ciphertext to the default storage target (local
    # disk, or a NAS over FTP/SMB). A remote outage falls back to local storage.
    blob = encrypt_bytes(derive_key(passphrase), data)
    app = current_app._get_current_object()
    location, _ = store_file(app, "screenshots", rel_path, blob,
                             target_id=default_target_id(app))

    retention_days = current_app.config["SCREENSHOT_RETENTION_DAYS"]
    shot = Screenshot(
        endpoint_id=endpoint.id,
        taken_by=require_current_user().id,
        taken_at=now,
        monitor_index=monitor_index,
        filename=rel_path,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        source_ip=client_ip(),
        expires_at=add_period(now, days=retention_days),
        storage_backend=location,
    )
    db.session.add(shot)
    db.session.flush()

    audit.record(
        TAKE_SCREENSHOT, target_type="endpoint", target_id=endpoint.id,
        metadata={"screenshotId": shot.id, "deviceName": endpoint.device_name,
                  "monitorIndex": monitor_index, "sizeBytes": len(data)},
    )
    db.session.commit()
    return jsonify(shot.to_dict()), 201


@bp.get("/endpoints/<endpoint_id>/screenshots")
@require_permission(ENDPOINTS_SCREEN_VIEW)
def list_screenshots(endpoint_id: str):
    """Saved screenshots for an endpoint, newest first. Same scope as viewing."""
    endpoint = _require_viewable_endpoint(endpoint_id)
    offset, limit = pagination()
    query = db.session.query(Screenshot).filter(Screenshot.endpoint_id == endpoint.id)
    total = query.count()
    rows = query.order_by(Screenshot.taken_at.desc()).offset(offset).limit(limit).all()
    return jsonify(paginated([s.to_dict() for s in rows], total, offset, limit))


@bp.get("/screenshots/<screenshot_id>/image")
@require_permission(ENDPOINTS_SCREEN_VIEW)
def screenshot_image(screenshot_id: str):
    """Decrypt and return one screenshot as JPEG (scope-checked, audited)."""
    shot = db.session.get(Screenshot, screenshot_id)
    if shot is None:
        raise NotFoundError("找不到截圖。")
    endpoint = _require_viewable_endpoint(shot.endpoint_id)

    passphrase = current_app.config.get("RECORDING_KEY_PASSPHRASE")
    if not passphrase:
        raise ConflictError("此伺服器未設定畫面加密金鑰，無法解密截圖。")

    app = current_app._get_current_object()
    try:
        ciphertext = load_file(app, "screenshots", shot.filename, shot.storage_backend)
    except StorageError:
        raise NotFoundError("截圖檔已不存在（可能已逾保留期限刪除）。")
    try:
        plaintext = decrypt_bytes(derive_key(passphrase), ciphertext)
    except Exception as exc:  # noqa: BLE001 - never leak crypto detail
        raise ConflictError("無法解密此截圖。") from exc

    audit.record(
        VIEW_SCREENSHOT, target_type="endpoint", target_id=endpoint.id,
        metadata={"screenshotId": shot.id, "deviceName": endpoint.device_name},
    )
    db.session.commit()
    return Response(
        plaintext,
        mimetype="image/jpeg",
        headers={"Content-Disposition": "inline", "Cache-Control": "no-store"},
    )


@bp.delete("/screenshots/<screenshot_id>")
@require_permission(ENDPOINTS_SCREEN_VIEW)
def delete_screenshot(screenshot_id: str):
    """Delete a screenshot (file + index row). Audited."""
    shot = db.session.get(Screenshot, screenshot_id)
    if shot is None:
        raise NotFoundError("找不到截圖。")
    endpoint = _require_viewable_endpoint(shot.endpoint_id)

    # Best-effort file delete; the index row goes regardless so it stops listing.
    remove_file(current_app._get_current_object(), "screenshots",
                shot.filename, shot.storage_backend)

    db.session.delete(shot)
    audit.record(
        DELETE_SCREENSHOT, target_type="endpoint", target_id=endpoint.id,
        metadata={"screenshotId": screenshot_id, "deviceName": endpoint.device_name},
    )
    db.session.commit()
    return jsonify({"status": "deleted", "id": screenshot_id})
