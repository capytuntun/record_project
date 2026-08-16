"""Resolution-aware recording frame rate (endpoint CPU control).

Encoding a 4K frame five times a second cannot fit the endpoint's capture-CPU
budget, so a 4K-class screen records at a lower rate. The choice is made once,
server-side, and drives BOTH the FFmpeg recorder and the agent's capture rate --
the recorder's timeline assumes a steady frame rate, so the two must agree.
"""

from __future__ import annotations

import json

from app.services import recording_control
from app.services.screen_hub import ScreenHub, WsConnection
from app.services.screen_hub import hub as shared_hub


class FakeWs:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, payload) -> None:
        self.sent.append(payload)


def _last_start(ws: FakeWs) -> dict:
    starts = [
        json.loads(p)
        for p in ws.sent
        if isinstance(p, str) and json.loads(p).get("type") == "start"
    ]
    assert starts, "expected a start message"
    return starts[-1]


# --- the resolution -> fps decision -----------------------------------------

def _seed_monitors(endpoint_id: str, monitors: list) -> None:
    shared_hub.reset()
    shared_hub.on_agent_monitors(endpoint_id, monitors)


def test_4k_screen_lowers_recording_fps(app):
    with app.app_context():
        _seed_monitors("e1", [{"index": 0, "width": 3840, "height": 2160, "primary": True}])
        fps = recording_control.effective_recording_fps(app, "e1", 5)
        assert fps == app.config["SCREEN_RECORDING_FPS_4K"]
        assert fps < 5
    shared_hub.reset()


def test_1080p_screen_keeps_policy_fps(app):
    with app.app_context():
        _seed_monitors("e1", [{"index": 0, "width": 1920, "height": 1080, "primary": True}])
        assert recording_control.effective_recording_fps(app, "e1", 5) == 5
    shared_hub.reset()


def test_1440p_screen_keeps_policy_fps(app):
    with app.app_context():
        _seed_monitors("e1", [{"index": 0, "width": 2560, "height": 1440, "primary": True}])
        assert recording_control.effective_recording_fps(app, "e1", 5) == 5
    shared_hub.reset()


def test_primary_monitor_decides_not_a_secondary_4k(app):
    """The captured monitor is the primary; a non-primary 4K panel must not drag
    the rate down for a 1080p primary."""
    with app.app_context():
        _seed_monitors("e1", [
            {"index": 0, "width": 1920, "height": 1080, "primary": True},
            {"index": 1, "width": 3840, "height": 2160, "primary": False},
        ])
        assert recording_control.effective_recording_fps(app, "e1", 5) == 5
    shared_hub.reset()


def test_unknown_resolution_falls_back_to_base(app):
    with app.app_context():
        shared_hub.reset()
        assert recording_control.effective_recording_fps(app, "unknown", 5) == 5
    shared_hub.reset()


def test_cap_never_raises_a_low_policy_fps(app):
    """min(): a policy already below the 4K cap stays where it is."""
    with app.app_context():
        _seed_monitors("e1", [{"index": 0, "width": 3840, "height": 2160, "primary": True}])
        assert recording_control.effective_recording_fps(app, "e1", 2) == 2
    shared_hub.reset()


# --- the fps reaches the agent's start message ------------------------------

def test_recording_start_tells_agent_the_recorder_fps(app):
    with app.app_context():
        hub = ScreenHub()
        ws = FakeWs()
        hub.register_agent(WsConnection(ws, kind="agent", endpoint_id="e1"))
        hub.start_recording("e1", fps=3)

        start = _last_start(ws)
        assert start["targetFps"] == 3
        assert start["allowSkipUnchanged"] is False


def test_live_view_uses_the_default_fps(app):
    with app.app_context():
        hub = ScreenHub()
        ws = FakeWs()
        hub.register_agent(WsConnection(ws, kind="agent", endpoint_id="e1"))
        # A viewer, no recording: the default target fps applies.
        hub.register_viewer(WsConnection(FakeWs(), kind="viewer", endpoint_id="e1"))

        start = _last_start(ws)
        assert start["targetFps"] == app.config["SCREEN_TARGET_FPS"]
        assert start["allowSkipUnchanged"] is True
