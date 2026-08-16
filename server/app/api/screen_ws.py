"""WebSocket endpoints for live screen viewing (sections 14, 29).

Two sockets meet at the hub:

  * the agent socket (/api/agent/screen/ws) authenticates with the device
    credential in an Authorization header -- a C# client can set that -- and
    pushes JPEG frames plus a monitor list.
  * the viewer socket (/api/endpoints/<id>/screen/ws) authenticates with a
    single-use ticket in the query string, because a browser cannot set the
    header.

Neither socket trusts the other's identity: the agent proves the endpoint it
is, the viewer proves the endpoint it may watch, and the hub only connects the
two when those match.
"""

from __future__ import annotations

import json
import logging

from flask import current_app, request

from ..errors import ApiError
from ..extensions import db, sock
from ..models import utcnow
from ..models.screen import ScreenSession
from ..security.authn import authenticate_agent_credential
from ..services import recording_control
from ..services.screen_hub import WsConnection, hub
from ..services.screen_tickets import tickets

logger = logging.getLogger("eem.screen")


def register_screen_ws() -> None:
    """Attach the WebSocket routes to the shared Sock instance.

    Called from the app factory after sock.init_app so the routes exist on the
    running application.
    """

    @sock.route("/api/agent/screen/ws")
    def agent_socket(ws):  # noqa: ANN001
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            ws.send(json.dumps({"type": "error", "message": "credential required"}))
            return

        try:
            endpoint, credential = authenticate_agent_credential(token.strip())
            endpoint_id = endpoint.id
            db.session.commit()
        except ApiError as exc:
            ws.send(json.dumps({"type": "error", "message": exc.message}))
            db.session.rollback()
            return
        finally:
            # Do not hold a DB connection for the life of the socket.
            db.session.remove()

        conn = WsConnection(ws, kind="agent", endpoint_id=endpoint_id, label="agent")
        hub.register_agent(conn)

        # Capture the app object for use off the request context inside the
        # receive loop. Recording is engaged later, once the agent's monitor list
        # arrives (its first message), so the recording frame rate can be matched
        # to the screen resolution -- a 4K screen records at a lower rate. See
        # _handle_agent_control.
        app = current_app._get_current_object()

        try:
            while True:
                message = ws.receive()
                if message is None:
                    break
                if isinstance(message, (bytes, bytearray)):
                    if len(message) <= _max_frame_bytes():
                        frame = bytes(message)
                        hub.on_agent_frame(endpoint_id, frame)
                        recording_control.on_frame(endpoint_id, frame)
                    # Oversized frames are dropped, not buffered.
                else:
                    _handle_agent_control(app, endpoint_id, message)
        except Exception:
            logger.info("agent socket for %s closed", endpoint_id)
        finally:
            recording_control.on_agent_disconnected(endpoint_id)
            hub.unregister_agent(conn)

    @sock.route("/api/endpoints/<endpoint_id>/screen/ws")
    def viewer_socket(ws, endpoint_id):  # noqa: ANN001
        ticket = tickets.redeem(request.args.get("ticket", ""))
        if ticket is None or ticket.endpoint_id != endpoint_id:
            ws.send(json.dumps({"type": "error", "message": "invalid or expired ticket"}))
            return

        conn = WsConnection(ws, kind="viewer", endpoint_id=endpoint_id, label=ticket.username)
        hub.register_viewer(conn)
        frames = 0
        try:
            while True:
                message = ws.receive()
                if message is None:
                    break
                if isinstance(message, str):
                    _handle_viewer_control(endpoint_id, message)
                # A viewer never sends binary; ignore if it does.
        except Exception:
            logger.info("viewer socket for %s closed", endpoint_id)
        finally:
            hub.unregister_viewer(conn)
            _close_session(ticket.session_id)

    logger.info("screen WebSocket routes registered")


def _handle_agent_control(app, endpoint_id: str, message: str) -> None:  # noqa: ANN001
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    if payload.get("type") == "monitors" and isinstance(payload.get("monitors"), list):
        # Cap the list so a misbehaving agent cannot flood viewers.
        hub.on_agent_monitors(endpoint_id, payload["monitors"][:16])
        # The resolution is now known -- engage recording if a policy covers this
        # endpoint, choosing a frame rate that suits the screen (4K records
        # slower). Idempotent: a repeat monitors message will not double-start.
        with app.app_context():
            try:
                recording_control.on_agent_connected(app, endpoint_id)
            finally:
                db.session.remove()


def _handle_viewer_control(endpoint_id: str, message: str) -> None:
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    if payload.get("type") == "set_monitor":
        index = payload.get("index")
        if isinstance(index, int) and 0 <= index < 16:
            hub.set_monitor(endpoint_id, index)


def _close_session(session_id: str) -> None:
    """Stamp the session's end time when its viewer disconnects."""
    try:
        session = db.session.get(ScreenSession, session_id)
        if session is not None and session.ended_at is None:
            session.ended_at = utcnow()
            db.session.commit()
    except Exception:
        db.session.rollback()
    finally:
        db.session.remove()


def _max_frame_bytes() -> int:
    from flask import current_app

    return current_app.config["SCREEN_MAX_FRAME_BYTES"]
