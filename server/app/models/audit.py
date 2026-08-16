"""Append-only audit trail (spec section 17).

Rows are written through ``app.services.audit.record`` and are never updated or
deleted by application code. The API exposes read-only access.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import db, iso, utcnow

# Actions worth reconstructing after the fact.
LOGIN = "LOGIN"
LOGIN_FAILED = "LOGIN_FAILED"
LOGOUT = "LOGOUT"
TOKEN_REFRESH = "TOKEN_REFRESH"
TOKEN_REUSE_DETECTED = "TOKEN_REUSE_DETECTED"
CREATE_USER = "CREATE_USER"
UPDATE_USER = "UPDATE_USER"
DELETE_USER = "DELETE_USER"
CHANGE_ROLE = "CHANGE_ROLE"
CHANGE_PASSWORD = "CHANGE_PASSWORD"
CREATE_ENROLLMENT_TOKEN = "CREATE_ENROLLMENT_TOKEN"
CREATE_PACKAGE = "CREATE_PACKAGE"
DOWNLOAD_PACKAGE = "DOWNLOAD_PACKAGE"
DELETE_PACKAGE = "DELETE_PACKAGE"
REVOKE_ENROLLMENT_TOKEN = "REVOKE_ENROLLMENT_TOKEN"
ENDPOINT_ENROLLED = "ENDPOINT_ENROLLED"
UPDATE_ENDPOINT = "UPDATE_ENDPOINT"
DISABLE_ENDPOINT = "DISABLE_ENDPOINT"
DELETE_ENDPOINT = "DELETE_ENDPOINT"
REVOKE_ENDPOINT_CREDENTIAL = "REVOKE_ENDPOINT_CREDENTIAL"
VIEW_SCREEN = "VIEW_SCREEN"
TAKE_SCREENSHOT = "TAKE_SCREENSHOT"
VIEW_SCREENSHOT = "VIEW_SCREENSHOT"
DELETE_SCREENSHOT = "DELETE_SCREENSHOT"
CREATE_GROUP = "CREATE_GROUP"
UPDATE_GROUP = "UPDATE_GROUP"
DELETE_GROUP = "DELETE_GROUP"
CHANGE_GROUP_MEMBERS = "CHANGE_GROUP_MEMBERS"
CHANGE_ADMIN_SCOPE = "CHANGE_ADMIN_SCOPE"
CHANGE_ADMIN_FEATURES = "CHANGE_ADMIN_FEATURES"
CHANGE_RECORDING_POLICY = "CHANGE_RECORDING_POLICY"
DELETE_RECORDING_POLICY = "DELETE_RECORDING_POLICY"
VIEW_RECORDING = "VIEW_RECORDING"
CHANGE_STORAGE_SETTING = "CHANGE_STORAGE_SETTING"
TEST_STORAGE_TARGET = "TEST_STORAGE_TARGET"
BOOTSTRAP_SUPER_ADMIN = "BOOTSTRAP_SUPER_ADMIN"
# Someone at the endpoint tried to remove the agent and could not supply the
# administrator password. Reported by the agent, never by the installer itself.
UNINSTALL_ATTEMPT = "UNINSTALL_ATTEMPT"
ACCESS_DENIED = "ACCESS_DENIED"
ACKNOWLEDGE_ALERT = "ACKNOWLEDGE_ALERT"
CHANGE_ALERT_CHANNEL = "CHANGE_ALERT_CHANNEL"

RESULT_SUCCESS = "SUCCESS"
RESULT_FAILURE = "FAILURE"
RESULT_DENIED = "DENIED"


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_actor_time", "actor_user_id", "timestamp"),
        Index("ix_audit_logs_action_time", "action", "timestamp"),
        Index("ix_audit_logs_target", "target_type", "target_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Nullable: agent-initiated and pre-authentication events have no admin actor.
    actor_user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_username: Mapped[str | None] = mapped_column(String(64))
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="USER")

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    source_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(256))
    request_id: Mapped[str | None] = mapped_column(String(36))

    result: Mapped[str] = mapped_column(String(16), nullable=False, default=RESULT_SUCCESS)

    # JSON-encoded, scrubbed of secrets before it reaches this column.
    metadata_json: Mapped[str | None] = mapped_column(Text)

    def to_dict(self) -> dict:
        import json

        return {
            "id": self.id,
            "actorUserId": self.actor_user_id,
            "actorUsername": self.actor_username,
            "actorType": self.actor_type,
            "action": self.action,
            "targetType": self.target_type,
            "targetId": self.target_id,
            "timestamp": iso(self.timestamp),
            "sourceIp": self.source_ip,
            "userAgent": self.user_agent,
            "requestId": self.request_id,
            "result": self.result,
            "metadata": json.loads(self.metadata_json) if self.metadata_json else None,
        }
