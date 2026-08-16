"""Managed endpoints, their per-device credentials, and enrollment tokens."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import SoftDeleteMixin, TimestampMixin, as_utc, days_until, db, iso, utcnow

# Stored lifecycle, controlled by an administrator.
STATE_ACTIVE = "ACTIVE"
STATE_DISABLED = "DISABLED"
ENDPOINT_STATES = (STATE_ACTIVE, STATE_DISABLED)

# Reported status returned to the console (spec section 21). ONLINE / OFFLINE /
# WARNING are derived from last_seen_at rather than stored, so a crashed agent
# cannot leave a stale ONLINE row behind.
STATUS_ONLINE = "ONLINE"
STATUS_OFFLINE = "OFFLINE"
STATUS_WARNING = "WARNING"
STATUS_DISABLED = "DISABLED"
STATUS_UNREGISTERED = "UNREGISTERED"


def _uuid() -> str:
    return str(uuid.uuid4())


class Endpoint(TimestampMixin, SoftDeleteMixin, db.Model):
    __tablename__ = "endpoints"

    # Server-issued identity. Deliberately not the machine name (section 10).
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    organization_id: Mapped[str | None] = mapped_column(String(64), index=True)

    device_name: Mapped[str | None] = mapped_column(String(128))
    os_name: Mapped[str | None] = mapped_column(String(128))
    os_version: Mapped[str | None] = mapped_column(String(64))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    local_user: Mapped[str | None] = mapped_column(String(128))
    last_ip: Mapped[str | None] = mapped_column(String(64))

    state: Mapped[str] = mapped_column(String(32), nullable=False, default=STATE_ACTIVE)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrolled_via_token_id: Mapped[str | None] = mapped_column(String(36))

    credentials: Mapped[list["EndpointCredential"]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan"
    )

    def status(self, offline_after_seconds: int, warning_after_seconds: int | None = None) -> str:
        """Derive the reported status from lifecycle state and heartbeat age."""
        if self.state == STATE_DISABLED:
            return STATUS_DISABLED
        if self.enrolled_at is None:
            return STATUS_UNREGISTERED

        last_seen = as_utc(self.last_seen_at)
        if last_seen is None:
            return STATUS_OFFLINE

        age = (utcnow() - last_seen).total_seconds()
        if age > offline_after_seconds:
            return STATUS_OFFLINE
        # A missed heartbeat or two: reachable but degraded.
        threshold = warning_after_seconds or max(offline_after_seconds // 2, 1)
        if age > threshold:
            return STATUS_WARNING
        return STATUS_ONLINE

    def to_dict(self, offline_after_seconds: int) -> dict:
        return {
            "id": self.id,
            "organizationId": self.organization_id,
            "deviceName": self.device_name,
            "os": self.os_name,
            "osVersion": self.os_version,
            "agentVersion": self.agent_version,
            "localUser": self.local_user,
            "ip": self.last_ip,
            "state": self.state,
            "status": self.status(offline_after_seconds),
            "lastSeenAt": iso(self.last_seen_at),
            "enrolledAt": iso(self.enrolled_at),
            "createdAt": iso(self.created_at),
            "updatedAt": iso(self.updated_at),
        }


class EndpointCredential(TimestampMixin, db.Model):
    """A rotating per-device secret. Never shared between endpoints (section 11).

    Only the SHA-256 of the secret is stored; the plaintext is returned once at
    enrollment and never again.
    """

    __tablename__ = "endpoint_credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoints.id"), nullable=False, index=True
    )
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    endpoint: Mapped[Endpoint] = relationship(back_populates="credentials")

    def is_usable(self) -> bool:
        if self.revoked_at is not None:
            return False
        expires = as_utc(self.expires_at)
        return expires is None or expires > utcnow()

    def days_remaining(self) -> int | None:
        """Whole days until this credential stops working. None = no expiry."""
        return days_until(self.expires_at)

    def expiry_warning(self, warn_within_days: int) -> dict | None:
        """A warning for the agent to surface locally, or None.

        Once a credential actually expires the agent can no longer authenticate,
        so there is nobody left to warn -- the notice has to go out while the
        credential still works. The agent shows this to enterprise IT and
        rotates before it lapses.
        """
        remaining = self.days_remaining()
        if remaining is None or remaining > warn_within_days:
            return None
        return {
            "code": "credential_expiring",
            "daysRemaining": remaining,
            "expiresAt": iso(self.expires_at),
            "action": "rotate",
            # days_remaining rounds up, so 1 means "some time in the next 24h".
            "message": (
                "裝置憑證已到期，Agent 必須重新註冊才能繼續回報狀態。"
                if remaining == 0
                else "裝置憑證將在 24 小時內到期，請確認 Agent 能連線以自動更新憑證。"
                if remaining == 1
                else f"裝置憑證將在 {remaining} 天後到期，請確認 Agent 能連線以自動更新憑證。"
            ),
        }


class EnrollmentToken(TimestampMixin, db.Model):
    """Installer token, scoped and revocable (spec section 18).

    Expiry is optional: an installer image is built once and may sit on a share
    or in a golden image for years, so a token that dies on a timer would make
    the package stop working. NULL expires_at means "until revoked", and
    max_uses 0 means "unlimited".

    That deliberately drops one of the four controls, so the other three carry
    more weight: revocation is immediate, every enrollment is audited, and the
    token is scoped to an organization. A leaked non-expiring token lets someone
    register a rogue endpoint until an administrator notices and revokes it --
    it never grants console access.

    Only the SHA-256 is stored. The plaintext is shown once, at creation.
    """

    __tablename__ = "enrollment_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    label: Mapped[str] = mapped_column(String(128), nullable=False)
    organization_id: Mapped[str | None] = mapped_column(String(64), index=True)
    policy_id: Mapped[str | None] = mapped_column(String(36))

    # NULL = never expires.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0 = unlimited uses.
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))

    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    @property
    def never_expires(self) -> bool:
        return self.expires_at is None

    @property
    def unlimited_uses(self) -> bool:
        return self.max_uses == 0

    def unusable_reason(self) -> str | None:
        """Why this token cannot be used, or None when it is fine.

        The reason is safe to return to an installer: whoever is asking already
        holds the token, so confirming that their own token is expired or
        revoked tells them nothing they could not learn by trying. It lets the
        agent show enterprise IT an actionable message instead of a generic
        failure.
        """
        if self.revoked_at is not None:
            return "revoked"
        if not self.unlimited_uses and self.use_count >= self.max_uses:
            return "exhausted"
        expires = as_utc(self.expires_at)
        if expires is not None and expires <= utcnow():
            return "expired"
        return None

    def is_usable(self) -> bool:
        return self.unusable_reason() is None

    def days_remaining(self) -> int | None:
        """Whole days until expiry, rounded up. None when it never expires."""
        return days_until(self.expires_at)

    def to_dict(self) -> dict:
        """Never includes the token itself, only its metadata."""
        return {
            "id": self.id,
            "label": self.label,
            "organizationId": self.organization_id,
            "policyId": self.policy_id,
            "expiresAt": iso(self.expires_at),
            "neverExpires": self.never_expires,
            "daysRemaining": self.days_remaining(),
            "maxUses": self.max_uses,
            "unlimitedUses": self.unlimited_uses,
            "useCount": self.use_count,
            "revokedAt": iso(self.revoked_at),
            "usable": self.is_usable(),
            "unusableReason": self.unusable_reason(),
            "createdBy": self.created_by,
            "createdAt": iso(self.created_at),
        }
