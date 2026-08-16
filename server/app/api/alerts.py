"""Alert center: /api/alerts and /api/alert-channels (feature: alert center).

Reading and acknowledging alerts needs ALERTS_READ (admins have it); configuring
where they are delivered needs ALERTS_MANAGE (super-admin), because a channel is
a place endpoint information leaves the system. Every channel change and every
acknowledgement is audited.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from ..errors import NotFoundError, ValidationError
from ..models import db, utcnow
from ..models.alert import (
    ALERT_ACKNOWLEDGED,
    ALERT_OPEN,
    CHANNEL_TYPES,
    SEVERITY_ORDER,
    SEV_INFO,
    Alert,
    AlertChannel,
)
from ..models.audit import ACKNOWLEDGE_ALERT, CHANGE_ALERT_CHANNEL, RESULT_SUCCESS
from ..request_context import require_current_user
from ..security.authn import require_permission
from ..security.rbac import ALERTS_MANAGE, ALERTS_READ
from ..services import alerting, audit
from .validation import get_str, json_body

bp = Blueprint("alerts", __name__, url_prefix="/api")


# --- alerts ----------------------------------------------------------------

@bp.get("/alerts")
@require_permission(ALERTS_READ)
def list_alerts():
    from flask import request

    query = db.session.query(Alert)
    status = request.args.get("status")
    severity = request.args.get("severity")
    if status:
        query = query.filter(Alert.status == status.upper())
    if severity:
        query = query.filter(Alert.severity == severity.lower())

    rows = query.order_by(Alert.created_at.desc()).limit(300).all()
    open_count = (
        db.session.query(Alert)
        .filter(Alert.status.in_((ALERT_OPEN, ALERT_ACKNOWLEDGED)))
        .count()
    )
    return jsonify({"items": [a.to_dict() for a in rows], "openCount": open_count})


@bp.post("/alerts/<alert_id>/acknowledge")
@require_permission(ALERTS_READ)
def acknowledge_alert(alert_id: str):
    actor = require_current_user()
    alert = db.session.get(Alert, alert_id)
    if alert is None:
        raise NotFoundError("找不到此告警。")
    if alert.status == ALERT_OPEN:
        alert.status = ALERT_ACKNOWLEDGED
        alert.acknowledged_at = utcnow()
        alert.acknowledged_by = actor.id
        audit.record(
            ACKNOWLEDGE_ALERT,
            actor=actor,
            target_type="alert",
            target_id=alert.id,
            result=RESULT_SUCCESS,
            metadata={"type": alert.type, "endpointId": alert.endpoint_id},
        )
    db.session.commit()
    return jsonify(alert.to_dict())


@bp.post("/alerts/evaluate")
@require_permission(ALERTS_READ)
def evaluate_alerts():
    """Sweep for offline endpoints now. The console calls this on load and on a
    timer; an external scheduler can also hit it. Idempotent -- deduplicated."""
    created = alerting.evaluate_offline()
    db.session.commit()
    return jsonify({"created": created})


# --- channels --------------------------------------------------------------

@bp.get("/alert-channels")
@require_permission(ALERTS_MANAGE)
def list_channels():
    rows = db.session.query(AlertChannel).order_by(AlertChannel.created_at.asc()).all()
    return jsonify({"items": [c.to_dict() for c in rows]})


def _channel_payload(body: dict) -> dict:
    name = get_str(body, "name", max_length=128)
    ctype = get_str(body, "type", max_length=16)
    if ctype not in CHANNEL_TYPES:
        raise ValidationError("通道類型必須是 email 或 webhook。")
    target = get_str(body, "target", max_length=512)
    min_severity = (get_str(body, "minSeverity", required=False, max_length=16) or "warning").lower()
    if min_severity not in SEVERITY_ORDER:
        raise ValidationError("嚴重性必須是 info / warning / critical。")
    if ctype == "email" and "@" not in target:
        raise ValidationError("email 通道的目標必須是電子郵件地址。")
    if ctype == "webhook" and not target.lower().startswith(("http://", "https://")):
        raise ValidationError("webhook 通道的目標必須是 http(s) 網址。")
    return {"name": name, "type": ctype, "target": target, "min_severity": min_severity}


@bp.post("/alert-channels")
@require_permission(ALERTS_MANAGE)
def create_channel():
    actor = require_current_user()
    values = _channel_payload(json_body())
    channel = AlertChannel(**values, enabled=1)
    db.session.add(channel)
    db.session.flush()
    audit.record(
        CHANGE_ALERT_CHANNEL, actor=actor, target_type="alert_channel", target_id=channel.id,
        result=RESULT_SUCCESS, metadata={"action": "create", "type": channel.type},
    )
    db.session.commit()
    return jsonify(channel.to_dict()), 201


@bp.patch("/alert-channels/<channel_id>")
@require_permission(ALERTS_MANAGE)
def update_channel(channel_id: str):
    actor = require_current_user()
    channel = db.session.get(AlertChannel, channel_id)
    if channel is None:
        raise NotFoundError("找不到此通道。")
    body = json_body()
    if "enabled" in body:
        channel.enabled = 1 if body.get("enabled") else 0
    ms = get_str(body, "minSeverity", required=False, max_length=16)
    if ms:
        if ms.lower() not in SEVERITY_ORDER:
            raise ValidationError("嚴重性必須是 info / warning / critical。")
        channel.min_severity = ms.lower()
    audit.record(
        CHANGE_ALERT_CHANNEL, actor=actor, target_type="alert_channel", target_id=channel.id,
        result=RESULT_SUCCESS, metadata={"action": "update"},
    )
    db.session.commit()
    return jsonify(channel.to_dict())


@bp.delete("/alert-channels/<channel_id>")
@require_permission(ALERTS_MANAGE)
def delete_channel(channel_id: str):
    actor = require_current_user()
    channel = db.session.get(AlertChannel, channel_id)
    if channel is None:
        raise NotFoundError("找不到此通道。")
    db.session.delete(channel)
    audit.record(
        CHANGE_ALERT_CHANNEL, actor=actor, target_type="alert_channel", target_id=channel_id,
        result=RESULT_SUCCESS, metadata={"action": "delete"},
    )
    db.session.commit()
    return jsonify({"status": "deleted"})


@bp.post("/alert-channels/<channel_id>/test")
@require_permission(ALERTS_MANAGE)
def test_channel(channel_id: str):
    """Deliver a one-off test notification so the operator can confirm a channel
    works. Not deduplicated and not stored as an alert -- it is only a probe."""
    channel = db.session.get(AlertChannel, channel_id)
    if channel is None:
        raise NotFoundError("找不到此通道。")
    probe = Alert(
        type="TEST", severity=SEV_INFO, title="端點管理系統測試通知",
        message="這是一則測試通知，確認此通道可以收到告警。",
    )
    try:
        alerting._deliver(channel, probe)
    except Exception as exc:  # noqa: BLE001 -- report the failure, do not 500
        return jsonify({"ok": False, "error": str(exc)}), 200
    return jsonify({"ok": True})
