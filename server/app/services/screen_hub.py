"""In-process relay between endpoint agents and console viewers (section 14, 29).

An agent connects one WebSocket and pushes JPEG frames of the screen it was
asked to capture. One or more viewers connect their own WebSockets to watch a
given endpoint. This hub routes frames from the agent to every viewer of that
endpoint, and control messages (start/stop capture, switch monitor) the other
way.

Design constraints from the spec:

* Frames are never persisted. The hub keeps only the single most recent frame
  per endpoint, in memory, so a viewer that joins mid-stream sees something
  immediately; it is overwritten continuously and dropped when viewing stops.
* Capture runs only while at least one authorised viewer is connected. When the
  last viewer leaves, the agent is told to stop -- an endpoint is never
  captured with nobody watching.

Scope: this hub lives in one process. With multiple Gunicorn workers an agent
and a viewer can land on different workers and not see each other; production
needs sticky routing or a shared broker. That limitation is documented, not
hidden.
"""

from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger("eem.screen")


class WsConnection:
    """A single WebSocket with a lock, since frames are sent from another
    connection's thread and simple-websocket sends are not re-entrant."""

    def __init__(self, ws, *, kind: str, endpoint_id: str, label: str = "") -> None:
        self.ws = ws
        self.kind = kind  # "agent" | "viewer"
        self.endpoint_id = endpoint_id
        self.label = label
        self._send_lock = threading.Lock()
        self.connected_at = time.monotonic()

    def send_text(self, message: dict) -> bool:
        return self._send(json.dumps(message))

    def send_bytes(self, data: bytes) -> bool:
        return self._send(data)

    def _send(self, payload) -> bool:
        try:
            with self._send_lock:
                self.ws.send(payload)
            return True
        except Exception:
            # A dead socket is normal (viewer closed the tab); the caller drops
            # it from the hub. Never let one bad viewer break the fan-out.
            return False


class ScreenHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._agents: dict[str, WsConnection] = {}
        self._viewers: dict[str, set[WsConnection]] = {}
        self._last_frame: dict[str, bytes] = {}
        self._monitors: dict[str, list] = {}
        # Endpoints being recorded, mapped to the frame rate the recorder was
        # started with (None if unspecified). Recording counts like a persistent
        # viewer: the agent keeps capturing while recording is on, even with
        # nobody watching live. The fps is kept here so the agent is told to
        # capture at exactly the recorder's rate -- the recorder's timeline
        # assumes a steady frame rate, so the two must not disagree.
        self._recording: dict[str, int | None] = {}

    def _should_capture(self, endpoint_id: str) -> bool:
        return bool(self._viewers.get(endpoint_id)) or endpoint_id in self._recording

    # --- agent side -------------------------------------------------------

    def register_agent(self, conn: WsConnection) -> None:
        with self._lock:
            existing = self._agents.get(conn.endpoint_id)
            if existing is not None and existing is not conn:
                existing.send_text({"type": "superseded"})
            self._agents[conn.endpoint_id] = conn
            capture = self._should_capture(conn.endpoint_id)
        logger.info("agent connected for endpoint %s (should_capture=%s)",
                    conn.endpoint_id, capture)
        if capture:
            # A viewer is waiting, or recording is active for this endpoint.
            self._tell_agent_start(conn.endpoint_id)

    # --- recording control ------------------------------------------------

    def start_recording(self, endpoint_id: str, fps: int | None = None) -> None:
        with self._lock:
            self._recording[endpoint_id] = fps
            agent_online = endpoint_id in self._agents
        if agent_online:
            self._tell_agent_start(endpoint_id)

    def stop_recording(self, endpoint_id: str) -> None:
        with self._lock:
            self._recording.pop(endpoint_id, None)
            still_needed = self._should_capture(endpoint_id)
            agent = self._agents.get(endpoint_id)
        if not still_needed and agent is not None:
            agent.send_text({"type": "stop"})
        elif still_needed and agent is not None:
            # A viewer is still watching but recording just ended: re-send start
            # so the agent may resume skipping unchanged frames. Recording needs
            # a steady frame rate for correct playback timing; live view does
            # not, so this flips the endpoint back to the cheaper mode.
            self._tell_agent_start(endpoint_id)

    def unregister_agent(self, conn: WsConnection) -> None:
        with self._lock:
            if self._agents.get(conn.endpoint_id) is conn:
                del self._agents[conn.endpoint_id]
                self._last_frame.pop(conn.endpoint_id, None)
                self._monitors.pop(conn.endpoint_id, None)
                viewers = list(self._viewers.get(conn.endpoint_id, ()))
            else:
                viewers = []
        for viewer in viewers:
            viewer.send_text({"type": "agent_offline"})

    def on_agent_frame(self, endpoint_id: str, data: bytes) -> int:
        """Fan a frame out to every viewer. Returns how many received it."""
        with self._lock:
            self._last_frame[endpoint_id] = data
            viewers = list(self._viewers.get(endpoint_id, ()))
        delivered = 0
        for viewer in viewers:
            if viewer.send_bytes(data):
                delivered += 1
            else:
                self._drop_viewer(viewer)
        return delivered

    def on_agent_monitors(self, endpoint_id: str, monitors: list) -> None:
        with self._lock:
            self._monitors[endpoint_id] = monitors
            viewers = list(self._viewers.get(endpoint_id, ()))
        for viewer in viewers:
            viewer.send_text({"type": "monitors", "monitors": monitors})

    # --- viewer side ------------------------------------------------------

    def register_viewer(self, conn: WsConnection) -> None:
        with self._lock:
            viewers = self._viewers.setdefault(conn.endpoint_id, set())
            first = len(viewers) == 0
            viewers.add(conn)
            agent_online = conn.endpoint_id in self._agents
            monitors = self._monitors.get(conn.endpoint_id)
            last_frame = self._last_frame.get(conn.endpoint_id)

        conn.send_text({"type": "status", "agentOnline": agent_online})
        if monitors:
            conn.send_text({"type": "monitors", "monitors": monitors})
        if last_frame:
            conn.send_bytes(last_frame)
        if first and agent_online:
            self._tell_agent_start(conn.endpoint_id)

    def unregister_viewer(self, conn: WsConnection) -> None:
        self._drop_viewer(conn)

    def _drop_viewer(self, conn: WsConnection) -> None:
        with self._lock:
            viewers = self._viewers.get(conn.endpoint_id)
            if not viewers or conn not in viewers:
                return
            viewers.discard(conn)
            empty = len(viewers) == 0
            if empty:
                del self._viewers[conn.endpoint_id]
            agent = self._agents.get(conn.endpoint_id)
            # Keep capturing if recording still needs it.
            keep = self._should_capture(conn.endpoint_id)
        if empty and not keep and agent is not None:
            # Nobody is watching and nothing is recording: stop capturing.
            agent.send_text({"type": "stop"})

    def set_monitor(self, endpoint_id: str, index: int) -> bool:
        with self._lock:
            agent = self._agents.get(endpoint_id)
        if agent is None:
            return False
        return agent.send_text({"type": "set_monitor", "index": index})

    # --- queries ----------------------------------------------------------

    def is_agent_online(self, endpoint_id: str) -> bool:
        with self._lock:
            return endpoint_id in self._agents

    def viewer_count(self, endpoint_id: str) -> int:
        with self._lock:
            return len(self._viewers.get(endpoint_id, ()))

    def monitors(self, endpoint_id: str) -> list | None:
        """The monitor list the agent last reported, or None if not yet known.

        Used to pick a recording frame rate that suits the screen resolution.
        """
        with self._lock:
            monitors = self._monitors.get(endpoint_id)
            return list(monitors) if monitors is not None else None

    def _tell_agent_start(self, endpoint_id: str) -> None:
        from flask import current_app

        with self._lock:
            agent = self._agents.get(endpoint_id)
            # When an endpoint is being recorded the frame stream also feeds the
            # server-side H.264 encoder, which assumes a steady frame rate --
            # dropping unchanged frames there would compress the recording's
            # wall-clock timeline. So skipping is offered only for live-view-only
            # capture. The flag is additive: an older agent ignores it and keeps
            # sending every frame, which is exactly today's behaviour.
            recording = endpoint_id in self._recording
            allow_skip = not recording
            # While recording, capture at exactly the recorder's frame rate (which
            # may be lowered for a 4K screen); live view uses the default.
            recording_fps = self._recording.get(endpoint_id)
        if agent is None:
            return
        target_fps = recording_fps if recording_fps else current_app.config["SCREEN_TARGET_FPS"]
        agent.send_text({
            "type": "start",
            "targetFps": target_fps,
            "jpegQuality": current_app.config["SCREEN_JPEG_QUALITY"],
            "allowSkipUnchanged": allow_skip,
            # Capture CPU controls. Unlike allowSkipUnchanged these apply while
            # recording too: recording still needs a frame every tick, but not a
            # freshly encoded one -- so the agent re-sends the cached JPEG for a
            # still screen and trades quality to stay within the CPU budget.
            "cpuBudgetPercent": current_app.config["SCREEN_CPU_BUDGET_PERCENT"],
            "jpegQualityFloor": current_app.config["SCREEN_JPEG_QUALITY_FLOOR"],
        })

    def reset(self) -> None:
        """Test helper: forget all state."""
        with self._lock:
            self._agents.clear()
            self._viewers.clear()
            self._last_frame.clear()
            self._monitors.clear()


# One hub per process.
hub = ScreenHub()
