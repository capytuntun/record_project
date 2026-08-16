"""Storage target settings: /api/storage/targets (spec sections 14, 17, 24).

SUPER_ADMIN only -- where screen data is stored is a system security setting
(section 6). The deployment can define many named targets (FTP/SMB NAS); a
recording policy picks one, and screenshots use the one flagged default. Every
change and connection test is audited; the NAS password is sealed at rest and
never returned to the client.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import RecordingPolicy, db
from ..models.audit import (
    CHANGE_STORAGE_SETTING,
    RESULT_FAILURE,
    RESULT_SUCCESS,
    TEST_STORAGE_TARGET,
)
from ..models.storage import REMOTE_BACKENDS, StorageTarget
from ..request_context import require_current_user
from ..security.authn import require_permission
from ..security.rbac import SYSTEM_SETTINGS_MANAGE
from ..services import audit
from ..services.storage import build_backend
from ..services.storage.secrets import seal, unseal
from .validation import get_int, get_str, json_body

bp = Blueprint("storage", __name__, url_prefix="/api/storage")


@bp.get("/targets")
@require_permission(SYSTEM_SETTINGS_MANAGE)
def list_targets():
    rows = db.session.query(StorageTarget).order_by(StorageTarget.created_at.asc()).all()
    return jsonify({"items": [t.to_dict() for t in rows]})


@bp.post("/targets")
@require_permission(SYSTEM_SETTINGS_MANAGE)
def create_target():
    actor = require_current_user()
    values, _, password = _collect(json_body())

    target = StorageTarget(created_by=actor.id)
    _assign(target, values)
    if password:
        target.secret_sealed = seal(current_app.config["SECRET_KEY"], password)
    if values["backend"] == "SMB" and not target.secret_sealed:
        raise ValidationError("SMB 目標需要密碼。")

    db.session.add(target)
    db.session.flush()
    if values["is_default"]:
        _make_sole_default(target.id)

    audit.record(CHANGE_STORAGE_SETTING, target_type="storage_target", target_id=target.id,
                 metadata={"action": "create", "name": values["name"],
                           "backend": values["backend"], "host": values["host"]})
    db.session.commit()
    return jsonify(target.to_dict()), 201


@bp.put("/targets/<target_id>")
@require_permission(SYSTEM_SETTINGS_MANAGE)
def update_target(target_id: str):
    target = db.session.get(StorageTarget, target_id)
    if target is None:
        raise NotFoundError("找不到儲存目標。")
    values, password_provided, password = _collect(json_body())

    _assign(target, values)
    if password_provided:
        target.secret_sealed = (
            seal(current_app.config["SECRET_KEY"], password) if password else None
        )
    if values["backend"] == "SMB" and not target.secret_sealed:
        raise ValidationError("SMB 目標需要密碼。")

    if values["is_default"]:
        _make_sole_default(target.id)
    else:
        target.is_default = False

    audit.record(CHANGE_STORAGE_SETTING, target_type="storage_target", target_id=target.id,
                 metadata={"action": "update", "name": values["name"],
                           "backend": values["backend"], "host": values["host"]})
    db.session.commit()
    return jsonify(target.to_dict())


@bp.delete("/targets/<target_id>")
@require_permission(SYSTEM_SETTINGS_MANAGE)
def delete_target(target_id: str):
    target = db.session.get(StorageTarget, target_id)
    if target is None:
        raise NotFoundError("找不到儲存目標。")

    used = (
        db.session.query(RecordingPolicy)
        .filter(RecordingPolicy.storage_target_id == target_id)
        .count()
    )
    if used:
        raise ConflictError(f"仍有 {used} 條錄影政策使用此儲存目標，請先改用其他目標。")

    db.session.delete(target)
    audit.record(CHANGE_STORAGE_SETTING, target_type="storage_target", target_id=target_id,
                 metadata={"action": "delete", "name": target.name})
    db.session.commit()
    return jsonify({"status": "deleted", "id": target_id})


@bp.post("/targets/test")
@require_permission(SYSTEM_SETTINGS_MANAGE)
def test_new_target():
    """Test an ad-hoc config (before saving it)."""
    values, _, password = _collect(json_body())
    return _run_test(values, password, label=values["name"])


@bp.post("/targets/<target_id>/test")
@require_permission(SYSTEM_SETTINGS_MANAGE)
def test_saved_target(target_id: str):
    """Test a stored target (optionally with a new password from the body)."""
    target = db.session.get(StorageTarget, target_id)
    if target is None:
        raise NotFoundError("找不到儲存目標。")
    body = json_body() if _has_json() else {}
    password = body.get("password") if isinstance(body.get("password"), str) else None
    if not password and target.secret_sealed:
        password = unseal(current_app.config["SECRET_KEY"], target.secret_sealed)
    values = {
        "backend": target.backend, "host": target.host, "port": target.port,
        "share": target.share, "base_path": target.base_path,
        "username": target.username, "domain": target.domain, "use_tls": target.use_tls,
    }
    return _run_test(values, password, label=target.name)


# --- helpers ---------------------------------------------------------------

def _run_test(values: dict, password: str | None, label: str | None):
    try:
        backend = build_backend(values, password)
        ok, message = backend.test()
    except Exception as exc:  # noqa: BLE001 - report, never 500
        ok, message = False, f"測試失敗：{exc}"

    audit.record(TEST_STORAGE_TARGET, target_type="storage_target",
                 result=RESULT_SUCCESS if ok else RESULT_FAILURE,
                 metadata={"name": label, "ok": ok})
    db.session.commit()
    return jsonify({"ok": ok, "message": message})


def _make_sole_default(keep_id: str) -> None:
    """Ensure exactly one default target."""
    for other in db.session.query(StorageTarget).filter(StorageTarget.id != keep_id).all():
        other.is_default = False
    target = db.session.get(StorageTarget, keep_id)
    target.is_default = True


def _assign(target: StorageTarget, values: dict) -> None:
    target.name = values["name"]
    target.backend = values["backend"]
    target.host = values["host"]
    target.port = values["port"]
    target.share = values["share"]
    target.base_path = values["base_path"]
    target.username = values["username"]
    target.domain = values["domain"]
    target.use_tls = values["use_tls"]


def _has_json() -> bool:
    return request.is_json and request.get_json(silent=True) is not None


def _collect(body: dict):
    """Validate a target config. Returns (values, password_provided, password)."""
    name = get_str(body, "name", max_length=128)
    backend = get_str(body, "backend", choices=REMOTE_BACKENDS)

    values = {
        "name": name, "backend": backend,
        "host": get_str(body, "host", max_length=255),
        "port": get_int(body, "port", minimum=1, maximum=65535),
        "base_path": get_str(body, "basePath", required=False, max_length=512),
        "username": get_str(body, "username", required=False, max_length=255),
        "share": None, "domain": None, "use_tls": True,
        "is_default": bool(body.get("isDefault", False)),
    }
    if backend == "FTP":
        values["use_tls"] = bool(body.get("useTls", True))
    elif backend == "SMB":
        values["share"] = get_str(body, "share", max_length=255)
        values["domain"] = get_str(body, "domain", required=False, max_length=255)

    password_provided = isinstance(body.get("password"), str)
    password = body.get("password") if password_provided else None
    return values, password_provided, password
