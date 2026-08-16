"""Raising, resolving, and delivering operational alerts (feature: alert center).

Alerts are created at the points where the signal already exists -- a heartbeat
(low disk, credential expiry), a refused uninstall, or an offline sweep -- and
delivered to any enabled channel whose minimum severity they meet. Delivery is
best-effort: a failed email or webhook is logged, never allowed to break the
request that raised the alert.

Deduplication keeps an ongoing condition to a single OPEN alert: raising the same
``dedup_key`` again returns the existing alert without a second notification, and
``resolve`` closes it when the condition clears.
"""

from __future__ import annotations

import json
import logging
import smtplib
import urllib.request
from email.message import EmailMessage

from flask import current_app

from ..models import Endpoint, db, utcnow
from ..models.alert import (
    ALERT_ACKNOWLEDGED,
    ALERT_OPEN,
    ALERT_RESOLVED,
    CHANNEL_EMAIL,
    CHANNEL_WEBHOOK,
    SEV_CRITICAL,
    SEV_WARNING,
    TYPE_OFFLINE,
    Alert,
    AlertChannel,
)
from ..models.endpoint import STATE_ACTIVE, STATUS_OFFLINE

logger = logging.getLogger("eem.alerts")

_OPEN_STATES = (ALERT_OPEN, ALERT_ACKNOWLEDGED)


def raise_alert(
    *,
    type: str,
    severity: str,
    title: str,
    message: str | None = None,
    endpoint_id: str | None = None,
    dedup_key: str | None = None,
) -> tuple[Alert, bool]:
    """Create an alert (and notify) unless one for the same condition is open.

    Returns ``(alert, created)``. When ``created`` is False the returned alert is
    the pre-existing open one, and no notification was sent.
    """
    if dedup_key:
        existing = (
            db.session.query(Alert)
            .filter(Alert.dedup_key == dedup_key, Alert.status.in_(_OPEN_STATES))
            .first()
        )
        if existing is not None:
            return existing, False

    alert = Alert(
        type=type,
        severity=severity,
        title=title,
        message=message,
        endpoint_id=endpoint_id,
        dedup_key=dedup_key,
        status=ALERT_OPEN,
    )
    db.session.add(alert)
    db.session.flush()
    _dispatch(alert)
    return alert, True


def resolve(dedup_key: str) -> int:
    """Close any open alert(s) for a condition that has cleared."""
    rows = (
        db.session.query(Alert)
        .filter(Alert.dedup_key == dedup_key, Alert.status.in_(_OPEN_STATES))
        .all()
    )
    now = utcnow()
    for row in rows:
        row.status = ALERT_RESOLVED
        row.resolved_at = now
    return len(rows)


def evaluate_offline(offline_after_seconds: int | None = None) -> int:
    """Sweep endpoints: raise an alert for each that has been offline too long,
    and resolve the alert for any that has come back. Returns new-alert count.

    Uses a longer threshold than the console's ONLINE/OFFLINE badge -- a machine
    that is merely powered off for the evening should not page anyone; this is
    for endpoints that have gone quiet for a sustained period.
    """
    threshold = offline_after_seconds or current_app.config["ALERT_OFFLINE_AFTER_SECONDS"]
    endpoints = (
        db.session.query(Endpoint)
        .filter(
            Endpoint.deleted_at.is_(None),
            Endpoint.state == STATE_ACTIVE,
            Endpoint.enrolled_at.isnot(None),
        )
        .all()
    )
    created = 0
    for endpoint in endpoints:
        key = f"offline:{endpoint.id}"
        status = endpoint.status(threshold)
        if status == STATUS_OFFLINE:
            _, is_new = raise_alert(
                type=TYPE_OFFLINE,
                severity=SEV_WARNING,
                title=f"端點離線：{endpoint.device_name or endpoint.id}",
                message="此端點已超過設定時間沒有回報心跳。",
                endpoint_id=endpoint.id,
                dedup_key=key,
            )
            created += 1 if is_new else 0
        else:
            resolve(key)
    return created


# --- delivery --------------------------------------------------------------

def _dispatch(alert: Alert) -> None:
    channels = db.session.query(AlertChannel).filter(AlertChannel.enabled == 1).all()
    for channel in channels:
        if not channel.notifies(alert.severity):
            continue
        try:
            _deliver(channel, alert)
        except Exception as exc:  # noqa: BLE001 -- delivery must never break the caller
            logger.warning("alert delivery failed (%s -> %s): %s",
                           alert.id, channel.type, exc)


def _deliver(channel: AlertChannel, alert: Alert) -> None:
    """Send one alert to one channel. Isolated (and module-level) so tests can
    substitute it, and so a channel type added later has one place to grow."""
    if channel.type == CHANNEL_EMAIL:
        _send_email(channel.target, alert)
    elif channel.type == CHANNEL_WEBHOOK:
        _post_webhook(channel.target, alert)


def _send_email(to_address: str, alert: Alert) -> None:
    config = current_app.config
    host = config.get("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP 未設定（EEM_SMTP_HOST）")

    message = EmailMessage()
    message["Subject"] = f"[端點告警][{alert.severity}] {alert.title}"
    message["From"] = config.get("SMTP_FROM") or "eem-alerts@localhost"
    message["To"] = to_address
    message.set_content((alert.message or alert.title) + f"\n\n嚴重性：{alert.severity}\n類型：{alert.type}")

    port = int(config.get("SMTP_PORT", 587))
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        if config.get("SMTP_USE_TLS", True):
            smtp.starttls()
        user = config.get("SMTP_USER")
        if user:
            smtp.login(user, config.get("SMTP_PASSWORD", ""))
        smtp.send_message(message)


def _post_webhook(url: str, alert: Alert) -> None:
    payload = {
        "type": alert.type,
        "severity": alert.severity,
        "title": alert.title,
        # Slack/Teams/LINE-compatible: a plain "text" they all render, plus the
        # structured fields for anything that wants them.
        "text": f"[{alert.severity}] {alert.title}\n{alert.message or ''}",
        "message": alert.message,
        "endpointId": alert.endpoint_id,
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 -- operator-entered URL
        response.read()
