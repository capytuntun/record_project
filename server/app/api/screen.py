"""Live screen viewing: REST control plane for /api/endpoints/<id>/screen.

The WebSocket transport itself lives in ``screen_ws`` -- this module handles the
authenticated REST steps around it: issuing a viewer ticket (which is where the
audit entry for "admin viewed endpoint" is written) and listing past sessions.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from ..errors import ConflictError, NotFoundError
from ..models import Endpoint, db, utcnow
from ..models.audit import VIEW_SCREEN
from ..models.screen import ScreenSession
from ..request_context import client_ip, require_current_user
from ..security.authn import require_permission
from ..security.rbac import ENDPOINTS_SCREEN_VIEW, can_access_endpoint
from ..services import audit
from ..services.screen_hub import hub
from ..services.screen_tickets import tickets
from .validation import paginated, pagination

bp = Blueprint("screen", __name__, url_prefix="/api/endpoints")


def _load_viewable_endpoint(endpoint_id: str) -> Endpoint:
    actor = require_current_user()
    endpoint = db.session.get(Endpoint, endpoint_id)
    if endpoint is None or endpoint.is_deleted:
        raise NotFoundError("找不到端點。")
    if not can_access_endpoint(actor, endpoint):
        # Out of scope reads as "not found" so the API does not confirm the id
        # exists to an admin with no business seeing it.
        audit.record_denied(
            VIEW_SCREEN, target_type="endpoint", target_id=endpoint_id, reason="out_of_scope"
        )
        db.session.commit()
        raise NotFoundError("找不到端點。")
    return endpoint


@bp.post("/<endpoint_id>/screen/ticket")
@require_permission(ENDPOINTS_SCREEN_VIEW)
def issue_ticket(endpoint_id: str):
    """Begin a viewing session and return a WebSocket ticket.

    This is the audited moment: the session row and the VIEW_SCREEN audit entry
    are written here, before any frame is seen. Whether the agent is reachable
    is reported so the console can show "waiting for agent" instead of failing.
    """
    actor = require_current_user()
    endpoint = _load_viewable_endpoint(endpoint_id)

    session = ScreenSession(
        endpoint_id=endpoint.id,
        viewer_user_id=actor.id,
        viewer_username=actor.username,
        started_at=utcnow(),
        source_ip=client_ip(),
    )
    db.session.add(session)
    db.session.flush()

    audit.record(
        VIEW_SCREEN,
        target_type="endpoint",
        target_id=endpoint.id,
        metadata={
            "sessionId": session.id,
            "deviceName": endpoint.device_name,
            "agentOnline": hub.is_agent_online(endpoint.id),
        },
    )
    db.session.commit()

    token = tickets.issue(
        endpoint_id=endpoint.id,
        user_id=actor.id,
        username=actor.username,
        session_id=session.id,
        ttl_seconds=current_app.config["SCREEN_TICKET_TTL_SECONDS"],
    )

    return jsonify({
        "sessionId": session.id,
        "ticket": token,
        "wsPath": f"/api/endpoints/{endpoint.id}/screen/ws?ticket={token}",
        "agentOnline": hub.is_agent_online(endpoint.id),
        "ticketTtlSeconds": current_app.config["SCREEN_TICKET_TTL_SECONDS"],
    })


@bp.get("/<endpoint_id>/screen/sessions")
@require_permission(ENDPOINTS_SCREEN_VIEW)
def list_sessions(endpoint_id: str):
    """Past viewing sessions for this endpoint -- the audit trail of who looked."""
    _load_viewable_endpoint(endpoint_id)
    offset, limit = pagination()
    query = db.session.query(ScreenSession).filter(ScreenSession.endpoint_id == endpoint_id)
    total = query.count()
    rows = (
        query.order_by(ScreenSession.started_at.desc()).offset(offset).limit(limit).all()
    )
    return jsonify(paginated([r.to_dict() for r in rows], total, offset, limit))
