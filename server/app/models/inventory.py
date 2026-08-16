"""Per-endpoint software/hardware inventory (feature: inventory dashboard).

One row per endpoint, upserted from the agent's heartbeat when it carries an
``inventory`` block. The heartbeat stays cheap -- the agent sends this only
every few hours, not every minute -- so most heartbeats do not touch this table.

Scalar columns hold the few facts other features query on (disk headroom for
alerts, OS build for patch triage); the full payload, including the installed-
software list, is kept as JSON so the console can show detail without a column
per field. Nothing here is document content or activity -- it is asset data,
consistent with the data-minimisation stance (CLAUDE.md section 13).
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import TimestampMixin, as_utc, db, iso


class EndpointInventory(TimestampMixin, db.Model):
    __tablename__ = "endpoint_inventory"

    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    os_build: Mapped[str | None] = mapped_column(String(64))
    cpu: Mapped[str | None] = mapped_column(String(128))
    cpu_cores: Mapped[int | None] = mapped_column(Integer)
    memory_total_mb: Mapped[int | None] = mapped_column(Integer)
    memory_free_mb: Mapped[int | None] = mapped_column(Integer)
    disk_total_gb: Mapped[int | None] = mapped_column(Integer)
    disk_free_gb: Mapped[int | None] = mapped_column(Integer)
    # Kept as its own column so the alert engine can filter "disk under N%".
    disk_free_percent: Mapped[int | None] = mapped_column(Integer, index=True)
    uptime_seconds: Mapped[int | None] = mapped_column(Integer)
    software_count: Mapped[int | None] = mapped_column(Integer)

    # Full payload, including the installed-software list.
    data_json: Mapped[str | None] = mapped_column(Text)

    @property
    def software(self) -> list:
        if not self.data_json:
            return []
        try:
            return json.loads(self.data_json).get("software", []) or []
        except (ValueError, TypeError):
            return []

    def to_dict(self) -> dict:
        return {
            "endpointId": self.endpoint_id,
            "collectedAt": iso(as_utc(self.collected_at)),
            "osBuild": self.os_build,
            "cpu": self.cpu,
            "cpuCores": self.cpu_cores,
            "memoryTotalMb": self.memory_total_mb,
            "memoryFreeMb": self.memory_free_mb,
            "diskTotalGb": self.disk_total_gb,
            "diskFreeGb": self.disk_free_gb,
            "diskFreePercent": self.disk_free_percent,
            "uptimeSeconds": self.uptime_seconds,
            "softwareCount": self.software_count,
            "software": self.software,
        }
