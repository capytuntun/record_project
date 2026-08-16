"""Administrator accounts and their refresh-token sessions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import SoftDeleteMixin, TimestampMixin, db, iso

ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_ADMIN = "ADMIN"
ROLES = (ROLE_SUPER_ADMIN, ROLE_ADMIN)

STATUS_ACTIVE = "ACTIVE"
STATUS_SUSPENDED = "SUSPENDED"
USER_STATUSES = (STATUS_ACTIVE, STATUS_SUSPENDED)


def _uuid() -> str:
    return str(uuid.uuid4())


class User(TimestampMixin, SoftDeleteMixin, db.Model):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=ROLE_ADMIN)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=STATUS_ACTIVE)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))

    # Bumped whenever privileges or credentials change. Access tokens carry the
    # epoch they were minted under, so a mismatch invalidates them instantly.
    # A counter rather than a timestamp: JWT 'iat' has one-second resolution, so
    # a time-based cutoff would let a token issued in the same second survive.
    token_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    scopes: Mapped[list["AdminEndpointScope"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_super_admin(self) -> bool:
        return self.role == ROLE_SUPER_ADMIN

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE and not self.is_deleted

    def to_dict(self) -> dict:
        """Public representation. Never includes password_hash."""
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "status": self.status,
            "createdAt": iso(self.created_at),
            "updatedAt": iso(self.updated_at),
            "lastLoginAt": iso(self.last_login_at),
            "createdBy": self.created_by,
            "deletedAt": iso(self.deleted_at),
        }


SCOPE_INCLUDE = "INCLUDE"
SCOPE_EXCLUDE = "EXCLUDE"


class AdminEndpointScope(TimestampMixin, db.Model):
    """Per-admin individual endpoint exceptions on top of group visibility.

    An admin's visible endpoints come mainly from the groups they are assigned
    to (AdminGroupAssignment). This table holds individual overrides:
    INCLUDE adds one endpoint outside any assigned group; EXCLUDE removes one
    that a group would otherwise grant. SUPER_ADMIN ignores all of this and
    sees every endpoint (section 6).
    """

    __tablename__ = "admin_endpoint_scopes"
    __table_args__ = (UniqueConstraint("user_id", "endpoint_id", name="uq_scope_user_endpoint"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoints.id"), nullable=False, index=True
    )
    # INCLUDE (add) or EXCLUDE (remove). Existing rows default to INCLUDE, which
    # preserves their old "grant this endpoint" meaning.
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=SCOPE_INCLUDE)

    user: Mapped[User] = relationship(back_populates="scopes")


class EndpointGroup(TimestampMixin, db.Model):
    """A named set of endpoints, the unit an admin is granted visibility over."""

    __tablename__ = "endpoint_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(512))
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))

    members: Mapped[list["EndpointGroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )

    def to_dict(self, member_count: int | None = None) -> dict:
        from .base import iso

        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "createdBy": self.created_by,
            "createdAt": iso(self.created_at),
        }
        if member_count is not None:
            data["memberCount"] = member_count
        return data


class EndpointGroupMember(TimestampMixin, db.Model):
    """Membership of an endpoint in a group. An endpoint may be in many groups."""

    __tablename__ = "endpoint_group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "endpoint_id", name="uq_group_endpoint"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoint_groups.id"), nullable=False, index=True
    )
    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoints.id"), nullable=False, index=True
    )

    group: Mapped[EndpointGroup] = relationship(back_populates="members")


class AdminGroupAssignment(TimestampMixin, db.Model):
    """Which groups a (non-super) admin can see."""

    __tablename__ = "admin_group_assignments"
    __table_args__ = (
        UniqueConstraint("user_id", "group_id", name="uq_admin_group"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoint_groups.id"), nullable=False, index=True
    )


class AdminFeatureGrant(TimestampMixin, db.Model):
    """A feature a SUPER_ADMIN has individually granted to a (non-super) admin.

    ``feature`` is a key from rbac.GRANTABLE_FEATURES. The grant unlocks that
    feature's permissions for this admin, on top of the base admin set.
    """

    __tablename__ = "admin_feature_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "feature", name="uq_admin_feature"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    feature: Mapped[str] = mapped_column(String(32), nullable=False)
    granted_by: Mapped[str | None] = mapped_column(String(36))


class RefreshToken(TimestampMixin, db.Model):
    """A refresh-token session supporting rotation, revocation and reuse detection.

    Only the SHA-256 of the token is stored, so a database read does not yield
    usable credentials (section 5).
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (Index("ix_refresh_tokens_family", "family_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # All tokens rotated from one login share a family; reuse of a rotated
    # token revokes the whole family.
    family_id: Mapped[str] = mapped_column(String(36), nullable=False, default=_uuid)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))
    replaced_by_id: Mapped[str | None] = mapped_column(String(36))

    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_ip: Mapped[str | None] = mapped_column(String(64))
    created_user_agent: Mapped[str | None] = mapped_column(String(256))
