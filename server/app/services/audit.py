"""Audit trail writer (spec section 17).

Every sensitive operation calls :func:`record`. Metadata is scrubbed before it
is persisted so credentials never reach the audit table or the log (section 27).
"""

from __future__ import annotations

import json
import logging

from ..models import AuditLog, User, db
from ..models.audit import RESULT_DENIED, RESULT_FAILURE, RESULT_SUCCESS
from ..request_context import client_ip, current_user, request_id, user_agent

logger = logging.getLogger("eem.audit")

# Substring match, case-insensitive: any metadata key containing one of these
# is replaced with a placeholder rather than stored.
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "cookie",
    "session",
    "apikey",
    "api_key",
    "private_key",
    "privatekey",
)

_REDACTED = "[REDACTED]"
_MAX_METADATA_CHARS = 4000


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def scrub(value, _depth: int = 0):
    """Recursively redact secret-looking keys from metadata."""
    if _depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _is_sensitive(str(key)) else scrub(val, _depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item, _depth + 1) for item in value]
    return value


def record(
    action: str,
    *,
    actor: User | None = None,
    actor_type: str = "USER",
    actor_username: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    result: str = RESULT_SUCCESS,
    metadata: dict | None = None,
) -> AuditLog:
    """Append an audit entry to the current transaction.

    The caller is responsible for committing. Audit rows commit together with
    the change they describe, so a rolled-back operation leaves no entry
    claiming it succeeded.
    """
    principal = actor if actor is not None else current_user()

    payload = None
    if metadata:
        serialized = json.dumps(scrub(metadata), default=str, ensure_ascii=False)
        if len(serialized) > _MAX_METADATA_CHARS:
            serialized = json.dumps({"truncated": True, "size": len(serialized)})
        payload = serialized

    entry = AuditLog(
        actor_user_id=principal.id if principal else None,
        actor_username=(principal.username if principal else actor_username),
        actor_type=actor_type,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        source_ip=client_ip(),
        user_agent=user_agent(),
        request_id=request_id(),
        result=result,
        metadata_json=payload,
    )
    db.session.add(entry)

    logger.info(
        "audit action=%s actor=%s target=%s/%s result=%s request_id=%s",
        action,
        entry.actor_username or "-",
        target_type or "-",
        target_id or "-",
        result,
        entry.request_id or "-",
    )
    return entry


def record_denied(action: str, *, target_type: str | None = None, target_id: str | None = None,
                  reason: str | None = None) -> AuditLog:
    """Record a refused operation. Denials are as interesting as successes."""
    return record(
        action,
        target_type=target_type,
        target_id=target_id,
        result=RESULT_DENIED,
        metadata={"reason": reason} if reason else None,
    )


def record_failure(action: str, *, target_type: str | None = None, target_id: str | None = None,
                   reason: str | None = None, actor_username: str | None = None) -> AuditLog:
    return record(
        action,
        actor_username=actor_username,
        target_type=target_type,
        target_id=target_id,
        result=RESULT_FAILURE,
        metadata={"reason": reason} if reason else None,
    )
