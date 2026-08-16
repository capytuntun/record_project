"""Operational alerts and the channels they notify (feature: alert center).

Alerts turn signals the system already has -- an endpoint gone offline, a device
credential about to expire, low disk from inventory, a refused uninstall -- into
something an administrator is told about, instead of a row they have to go
looking for. Each alert carries a ``dedup_key`` so an ongoing condition raises
one alert, not one per evaluation; when the condition clears the alert is
resolved rather than duplicated.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import TimestampMixin, as_utc, db, iso

# Lifecycle.
ALERT_OPEN = "OPEN"
ALERT_ACKNOWLEDGED = "ACKNOWLEDGED"
ALERT_RESOLVED = "RESOLVED"

# Severity, low to high. Channels notify at or above their configured minimum.
SEV_INFO = "info"
SEV_WARNING = "warning"
SEV_CRITICAL = "critical"
SEVERITY_ORDER = {SEV_INFO: 0, SEV_WARNING: 1, SEV_CRITICAL: 2}

# Alert types.
TYPE_OFFLINE = "OFFLINE"
TYPE_LOW_DISK = "LOW_DISK"
TYPE_CREDENTIAL_EXPIRING = "CREDENTIAL_EXPIRING"
TYPE_UNINSTALL_ATTEMPT = "UNINSTALL_ATTEMPT"

# Channels.
CHANNEL_EMAIL = "email"
CHANNEL_WEBHOOK = "webhook"
CHANNEL_TYPES = (CHANNEL_EMAIL, CHANNEL_WEBHOOK)


def _uuid() -> str:
    return str(uuid.uuid4())


class Alert(TimestampMixin, db.Model):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default=SEV_WARNING)
    endpoint_id: Mapped[str | None] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)

    # Identifies an ongoing condition: at most one OPEN alert per key.
    dedup_key: Mapped[str | None] = mapped_column(String(200), index=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ALERT_OPEN, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(36))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "severity": self.severity,
            "endpointId": self.endpoint_id,
            "title": self.title,
            "message": self.message,
            "status": self.status,
            "createdAt": iso(as_utc(self.created_at)),
            "resolvedAt": iso(as_utc(self.resolved_at)),
            "acknowledgedAt": iso(as_utc(self.acknowledged_at)),
            "acknowledgedBy": self.acknowledged_by,
        }


class AlertChannel(TimestampMixin, db.Model):
    __tablename__ = "alert_channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    # An email address, or a webhook URL. Not a secret -- webhook URLs can be,
    # but these are operator-entered destinations, not credentials.
    target: Mapped[str] = mapped_column(String(512), nullable=False)
    min_severity: Mapped[str] = mapped_column(String(16), nullable=False, default=SEV_WARNING)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    def notifies(self, severity: str) -> bool:
        return bool(self.enabled) and (
            SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get(self.min_severity, 0)
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "target": self.target,
            "minSeverity": self.min_severity,
            "enabled": bool(self.enabled),
            "createdAt": iso(as_utc(self.created_at)),
        }
