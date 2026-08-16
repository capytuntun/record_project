"""Named storage targets for screen data (spec sections 14, 24).

Instead of one active target, the deployment can define **many** named targets
(each a NAS reached over FTP/SMB). A recording policy then picks which target its
recordings go to; screenshots use the target flagged ``is_default``. "本機磁碟"
is the implicit built-in target (a policy with no target id, or no default) and
is never a row here. Passwords are sealed, never stored or returned in plaintext;
screen data is always encrypted before it is handed to a target.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import TimestampMixin, db, iso

BACKEND_FTP = "FTP"
BACKEND_SMB = "SMB"
REMOTE_BACKENDS = (BACKEND_FTP, BACKEND_SMB)

# Sentinel used in the per-file location column for "stored on local disk".
LOCATION_LOCAL = "LOCAL"


def _uuid() -> str:
    return str(uuid.uuid4())


class StorageTarget(TimestampMixin, db.Model):
    __tablename__ = "storage_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    backend: Mapped[str] = mapped_column(String(16), nullable=False)  # FTP | SMB
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    share: Mapped[str | None] = mapped_column(String(255))      # SMB share name
    base_path: Mapped[str | None] = mapped_column(String(512))
    username: Mapped[str | None] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255))     # SMB domain (optional)
    use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # FTPS

    # The default target -- used by screenshots and offered as the policy default.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # AES-GCM sealed password (base64). Never returned to the client.
    secret_sealed: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[str | None] = mapped_column(String(36))

    def to_dict(self) -> dict:
        """Public shape -- deliberately omits the password."""
        return {
            "id": self.id,
            "name": self.name,
            "backend": self.backend,
            "host": self.host,
            "port": self.port,
            "share": self.share,
            "basePath": self.base_path,
            "username": self.username,
            "domain": self.domain,
            "useTls": self.use_tls,
            "isDefault": self.is_default,
            "hasPassword": bool(self.secret_sealed),
            "updatedAt": iso(self.updated_at),
        }
