"""Generated installer packages (spec section 18)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import TimestampMixin, days_until, db, iso

STATUS_BUILDING = "BUILDING"
STATUS_READY = "READY"
STATUS_FAILED = "FAILED"
STATUS_DELETED = "DELETED"


class InstallationPackage(TimestampMixin, db.Model):
    """A built MSI, tracked so it can be re-downloaded, audited and expired.

    The MSI itself carries an enrollment token, so the file is as sensitive as
    the token inside it: downloads require permission and are audited, and the
    file is removed once its retention window passes (section 23).
    """

    __tablename__ = "installation_packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    label: Mapped[str] = mapped_column(String(128), nullable=False)
    organization_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # The token baked into this MSI. Revoking it disables every copy in
    # circulation, which is the only kill switch once the file has left here.
    enrollment_token_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("enrollment_tokens.id"), nullable=False, index=True
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_BUILDING)
    failure_reason: Mapped[str | None] = mapped_column(String(255))

    filename: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    agent_version: Mapped[str | None] = mapped_column(String(32))

    # Whether this package's administrator password was set. The password
    # itself is never stored here -- only its hash goes into the MSI.
    has_admin_password: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Whether the built MSI was Authenticode-signed.
    signed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # When the stored file is eligible for cleanup. Independent of the token's
    # own validity: the file can be swept while the token still works.
    file_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "organizationId": self.organization_id,
            "enrollmentTokenId": self.enrollment_token_id,
            "status": self.status,
            "failureReason": self.failure_reason,
            "filename": self.filename,
            "sizeBytes": self.size_bytes,
            "sha256": self.sha256,
            "agentVersion": self.agent_version,
            "hasAdminPassword": bool(self.has_admin_password),
            "signed": bool(self.signed),
            "downloadCount": self.download_count,
            "lastDownloadedAt": iso(self.last_downloaded_at),
            "fileExpiresAt": iso(self.file_expires_at),
            "fileDaysRemaining": days_until(self.file_expires_at),
            "createdBy": self.created_by,
            "createdAt": iso(self.created_at),
        }
