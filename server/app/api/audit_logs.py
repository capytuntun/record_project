"""Read-only audit trail access: /api/audit-logs (spec section 17).

There is no write, update or delete route by design. Rows are created only by
``app.services.audit`` as a side effect of the operation they describe.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..errors import ValidationError
from ..models import AuditLog, db
from ..security.authn import require_permission
from ..security.rbac import AUDIT_LOGS_READ
from .validation import paginated, pagination, query_datetime

bp = Blueprint("audit_logs", __name__, url_prefix="/api/audit-logs")

MAX_EXPORT_ROWS = 10_000


@bp.get("")
@require_permission(AUDIT_LOGS_READ)
def list_audit_logs():
    offset, limit = pagination()
    query = _filtered_query()
    total = query.count()
    rows = query.order_by(AuditLog.timestamp.desc()).offset(offset).limit(limit).all()
    return jsonify(paginated([row.to_dict() for row in rows], total, offset, limit))


@bp.get("/export")
@require_permission(AUDIT_LOGS_READ)
def export_audit_logs():
    """Bounded export for compliance review (spec section 23)."""
    query = _filtered_query()
    rows = query.order_by(AuditLog.timestamp.desc()).limit(MAX_EXPORT_ROWS).all()
    truncated = len(rows) == MAX_EXPORT_ROWS
    return jsonify(
        {
            "items": [row.to_dict() for row in rows],
            "count": len(rows),
            "truncated": truncated,
            "maxRows": MAX_EXPORT_ROWS,
        }
    )


def _filtered_query():
    query = db.session.query(AuditLog)

    action = request.args.get("action", "").strip()
    if action:
        if len(action) > 64:
            raise ValidationError("'action' is too long.")
        query = query.filter(AuditLog.action == action)

    actor = request.args.get("actorUserId", "").strip()
    if actor:
        query = query.filter(AuditLog.actor_user_id == actor)

    target_type = request.args.get("targetType", "").strip()
    if target_type:
        query = query.filter(AuditLog.target_type == target_type)

    target_id = request.args.get("targetId", "").strip()
    if target_id:
        query = query.filter(AuditLog.target_id == target_id)

    result = request.args.get("result", "").strip().upper()
    if result:
        if result not in {"SUCCESS", "FAILURE", "DENIED"}:
            raise ValidationError("'result' must be SUCCESS, FAILURE or DENIED.")
        query = query.filter(AuditLog.result == result)

    start = query_datetime("from")
    if start:
        query = query.filter(AuditLog.timestamp >= start)

    end = query_datetime("to")
    if end:
        query = query.filter(AuditLog.timestamp <= end)

    return query
