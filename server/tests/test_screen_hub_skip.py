"""The recording-safe frame-skip flag on the screen hub (performance work).

Skipping unchanged frames on the endpoint is a real CPU saving, but the same
frame stream feeds the server-side H.264 recorder, which needs a steady rate
for correct playback timing. So the hub tells the agent it may skip only while
an endpoint is NOT being recorded. These tests pin that contract, including the
transition back to skipping when recording stops but a viewer stays.
"""

from __future__ import annotations

import json

from app.services.screen_hub import ScreenHub, WsConnection


class FakeWs:
    """Records what the hub sends, standing in for a real WebSocket."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, payload) -> None:
        self.sent.append(payload)


def _start_messages(ws: FakeWs) -> list[dict]:
    out = []
    for payload in ws.sent:
        if isinstance(payload, str):
            try:
                message = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "start":
                out.append(message)
    return out


def _agent(hub: ScreenHub, endpoint_id: str) -> FakeWs:
    ws = FakeWs()
    hub.register_agent(WsConnection(ws, kind="agent", endpoint_id=endpoint_id))
    return ws


def _viewer(hub: ScreenHub, endpoint_id: str) -> WsConnection:
    conn = WsConnection(FakeWs(), kind="viewer", endpoint_id=endpoint_id)
    hub.register_viewer(conn)
    return conn


def test_live_view_only_allows_skipping(app):
    with app.app_context():
        hub = ScreenHub()
        agent_ws = _agent(hub, "e1")
        _viewer(hub, "e1")

        starts = _start_messages(agent_ws)
        assert starts, "a viewer joining should start capture"
        assert starts[-1]["allowSkipUnchanged"] is True


def test_recording_forbids_skipping(app):
    with app.app_context():
        hub = ScreenHub()
        agent_ws = _agent(hub, "e1")
        hub.start_recording("e1")

        starts = _start_messages(agent_ws)
        assert starts, "recording should start capture"
        # A recorded stream must arrive at a steady rate; skipping would compress
        # the recording's timeline.
        assert starts[-1]["allowSkipUnchanged"] is False


def test_start_carries_cpu_controls(app):
    """Every start -- live view or recording -- must ship the capture CPU knobs
    so the endpoint caps its own cost. These apply even when skipping is off
    (recording), which is the whole point of the recording CPU fix."""
    with app.app_context():
        hub = ScreenHub()
        agent_ws = _agent(hub, "e1")
        hub.start_recording("e1")

        start = _start_messages(agent_ws)[-1]
        assert start["allowSkipUnchanged"] is False   # recording
        # The CPU controls are present and sane regardless of skip mode.
        assert start["cpuBudgetPercent"] == app.config["SCREEN_CPU_BUDGET_PERCENT"]
        assert start["jpegQualityFloor"] == app.config["SCREEN_JPEG_QUALITY_FLOOR"]
        assert 0 < start["cpuBudgetPercent"] <= 100
        assert start["jpegQualityFloor"] <= start["jpegQuality"]


def test_recording_while_a_viewer_watches_still_forbids_skipping(app):
    with app.app_context():
        hub = ScreenHub()
        agent_ws = _agent(hub, "e1")
        _viewer(hub, "e1")                 # start #1: allow skip
        hub.start_recording("e1")          # start #2: must forbid skip

        starts = _start_messages(agent_ws)
        assert len(starts) >= 2
        assert starts[-1]["allowSkipUnchanged"] is False


def test_stopping_recording_with_a_viewer_restores_skipping(app):
    """When recording ends but someone is still watching, the endpoint should
    be told it may skip again -- the whole point of the re-send fix."""
    with app.app_context():
        hub = ScreenHub()
        agent_ws = _agent(hub, "e1")
        _viewer(hub, "e1")
        hub.start_recording("e1")
        hub.stop_recording("e1")

        starts = _start_messages(agent_ws)
        assert starts[-1]["allowSkipUnchanged"] is True


def test_stopping_recording_with_no_viewer_stops_capture(app):
    """No viewer, recording ended: capture stops entirely, not a re-start."""
    with app.app_context():
        hub = ScreenHub()
        agent_ws = _agent(hub, "e1")
        hub.start_recording("e1")
        agent_ws.sent.clear()
        hub.stop_recording("e1")

        messages = [json.loads(p) for p in agent_ws.sent if isinstance(p, str)]
        types = [m.get("type") for m in messages]
        assert "stop" in types
        assert "start" not in types
