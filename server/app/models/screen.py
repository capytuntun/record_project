"""Screen viewing sessions (spec section 14).

This table stores only *metadata about the act of viewing* -- who looked at
which endpoint, when, from where, and for how long. It never stores frame
content: frames are relayed in memory and discarded (section 14's "do not
persist the screen to the database").

Its purpose is the audit requirement: every screen view must be reconstructable
-- "admin A viewed endpoint B at time T from IP X".
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import TimestampMixin, db, iso


class ScreenSession(TimestampMixin, db.Model):
    __tablename__ = "screen_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoints.id"), nullable=False, index=True
    )
    viewer_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    viewer_username: Mapped[str] = mapped_column(String(64), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_ip: Mapped[str | None] = mapped_column(String(64))
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "endpointId": self.endpoint_id,
            "viewerUserId": self.viewer_user_id,
            "viewerUsername": self.viewer_username,
            "startedAt": iso(self.started_at),
            "endedAt": iso(self.ended_at),
            "sourceIp": self.source_ip,
            "frameCount": self.frame_count,
        }
