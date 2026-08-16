"""Screenshot index (spec sections 14, 23).

A screenshot is a single still frame an admin captured from the live viewer.
Like recordings, the image bytes live in an AES-encrypted file on disk, never in
the database (section 14) -- this row is only the index (who captured it, which
endpoint, when, where the encrypted file is, and when it expires).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import TimestampMixin, db, iso


def _uuid() -> str:
    return str(uuid.uuid4())


class Screenshot(TimestampMixin, db.Model):
    __tablename__ = "screenshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoints.id"), nullable=False, index=True
    )
    # The admin who captured it (screenshots are only ever admin-initiated).
    taken_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    monitor_index: Mapped[int | None] = mapped_column(Integer)

    # Relative filename under SCREENSHOT_DIR; the encrypted JPEG on disk.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))

    source_ip: Mapped[str | None] = mapped_column(String(64))

    # When the file is eligible for retention deletion.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # Where the encrypted file physically lives: LOCAL | FTP | SMB.
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False, default="LOCAL")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "endpointId": self.endpoint_id,
            "takenBy": self.taken_by,
            "takenAt": iso(self.taken_at),
            "monitorIndex": self.monitor_index,
            "sizeBytes": self.size_bytes,
            "expiresAt": iso(self.expires_at),
        }
