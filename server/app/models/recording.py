"""Screen recording policies and the segment index (spec sections 14, 23).

Two tables, both metadata-only. Frame content lives in AES-encrypted files on
disk, never in the database (section 14). ``RecordingSegment`` is the index that
maps an endpoint + time range to one of those files.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import TimestampMixin, db, iso

# Recording modes (the user-chosen distinction).
MODE_DIFFERENTIAL = "DIFFERENTIAL"  # H.264 with periodic keyframes + deltas (small)
MODE_FULL = "FULL"                  # all-intra: every frame a keyframe (large, exact)
RECORDING_MODES = (MODE_DIFFERENTIAL, MODE_FULL)

# What a policy targets.
TARGET_ENDPOINT = "ENDPOINT"
TARGET_GROUP = "GROUP"

# Where a policy's recordings are stored: a StorageTarget id, or NULL = 本機磁碟.


def _uuid() -> str:
    return str(uuid.uuid4())


class RecordingPolicy(TimestampMixin, db.Model):
    """A rule that turns on continuous recording for an endpoint or a group."""

    __tablename__ = "recording_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    target_type: Mapped[str] = mapped_column(String(16), nullable=False)  # ENDPOINT | GROUP
    target_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=MODE_DIFFERENTIAL)
    fps: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Which storage target these recordings go to; NULL = 本機磁碟 (local disk).
    storage_target_id: Mapped[str | None] = mapped_column(String(36))

    label: Mapped[str | None] = mapped_column(String(128))
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)

    def to_dict(self, target_name: str | None = None,
                storage_target_name: str | None = None) -> dict:
        return {
            "id": self.id,
            "targetType": self.target_type,
            "targetId": self.target_id,
            "targetName": target_name,
            "mode": self.mode,
            "fps": self.fps,
            "retentionDays": self.retention_days,
            "enabled": self.enabled,
            "storageTargetId": self.storage_target_id,
            "storageTargetName": storage_target_name,
            "label": self.label,
            "createdBy": self.created_by,
            "createdAt": iso(self.created_at),
        }


class RecordingSegment(TimestampMixin, db.Model):
    """One encrypted H.264 segment on disk, indexed by endpoint and time range.

    The row never holds frame content -- only where the (encrypted) file is and
    what time span it covers, so playback can find the right file for a moment.
    """

    __tablename__ = "recording_segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoints.id"), nullable=False, index=True
    )
    policy_id: Mapped[str | None] = mapped_column(String(36))

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # Relative filename under RECORDING_DIR; the encrypted file on disk.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # When the file is eligible for retention deletion.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # Where the encrypted file physically lives: LOCAL | FTP | SMB.
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False, default="LOCAL")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "endpointId": self.endpoint_id,
            "policyId": self.policy_id,
            "startedAt": iso(self.started_at),
            "endedAt": iso(self.ended_at),
            "mode": self.mode,
            "sizeBytes": self.size_bytes,
            "frameCount": self.frame_count,
            "expiresAt": iso(self.expires_at),
        }
