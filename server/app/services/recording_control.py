"""Drives recording lifecycle from the agent's screen connection.

When an agent's screen WebSocket connects, if the endpoint is covered by an
enabled recording policy, a recorder is started and the hub is told to keep the
agent capturing. Frames are forwarded to the recorder; when the agent
disconnects, recording stops. This is what makes recording "continuous" -- it
runs the whole time the agent is present, independent of any live viewer.
"""

from __future__ import annotations

import logging

from ..models import EndpointGroupMember, RecordingPolicy, db
from ..models.recording import TARGET_ENDPOINT, TARGET_GROUP
from .recorder import manager
from .screen_hub import hub

logger = logging.getLogger("eem.recording")


def resolve_policy(endpoint_id: str):
    """The effective enabled recording policy for an endpoint, or None.

    An endpoint-level policy wins over a group-level one; among group policies,
    the first enabled match is used.
    """
    endpoint_policy = (
        db.session.query(RecordingPolicy)
        .filter(
            RecordingPolicy.enabled.is_(True),
            RecordingPolicy.target_type == TARGET_ENDPOINT,
            RecordingPolicy.target_id == endpoint_id,
        )
        .first()
    )
    if endpoint_policy is not None:
        return endpoint_policy

    group_ids = [
        row[0]
        for row in db.session.query(EndpointGroupMember.group_id)
        .filter(EndpointGroupMember.endpoint_id == endpoint_id)
        .all()
    ]
    if not group_ids:
        return None

    return (
        db.session.query(RecordingPolicy)
        .filter(
            RecordingPolicy.enabled.is_(True),
            RecordingPolicy.target_type == TARGET_GROUP,
            RecordingPolicy.target_id.in_(group_ids),
        )
        .first()
    )


# A screen whose longest edge is at least this wide is treated as "4K-class".
# 3840x2160 (and wider/taller variants) qualify; 2560x1440 and below do not.
_UHD_MIN_LONGEST_EDGE = 3840


def _primary_monitor(monitors: list | None) -> dict | None:
    """The monitor recording captures: the one flagged primary, else the first."""
    if not monitors:
        return None
    for m in monitors:
        if isinstance(m, dict) and m.get("primary"):
            return m
    first = monitors[0]
    return first if isinstance(first, dict) else None


def effective_recording_fps(app, endpoint_id: str, base_fps: int) -> int:
    """The frame rate to record at, given the endpoint's screen resolution.

    A 4K-class screen is capped to ``SCREEN_RECORDING_FPS_4K`` so the endpoint's
    capture CPU stays within budget; everything else records at ``base_fps`` (the
    policy's value). If the resolution is not yet known this returns ``base_fps``
    -- but recording is engaged only once the monitor list has arrived (see
    :mod:`app.api.screen_ws`), so in practice the resolution is always known.
    """
    monitor = _primary_monitor(hub.monitors(endpoint_id))
    if monitor is None:
        return base_fps
    longest = max(int(monitor.get("width", 0)), int(monitor.get("height", 0)))
    if longest >= _UHD_MIN_LONGEST_EDGE:
        return min(base_fps, app.config["SCREEN_RECORDING_FPS_4K"])
    return base_fps


def on_agent_connected(app, endpoint_id: str) -> None:
    """Start recording if a policy covers this endpoint and it is not already
    running. Called once the agent's monitor list is known, so the frame rate
    can be matched to the screen resolution."""
    if manager.is_recording(endpoint_id):
        return
    policy = resolve_policy(endpoint_id)
    if policy is None:
        return
    fps = effective_recording_fps(app, endpoint_id, policy.fps)
    started = manager.start(
        app,
        endpoint_id=endpoint_id,
        policy_id=policy.id,
        mode=policy.mode,
        fps=fps,
        retention_days=policy.retention_days,
        storage_target_id=policy.storage_target_id,
    )
    if started:
        hub.start_recording(endpoint_id, fps)
        logger.info("recording engaged for endpoint %s via policy %s at %s fps",
                    endpoint_id, policy.id, fps)


def on_agent_disconnected(endpoint_id: str) -> None:
    if manager.is_recording(endpoint_id):
        hub.stop_recording(endpoint_id)
        manager.stop(endpoint_id)


def on_frame(endpoint_id: str, data: bytes) -> None:
    manager.feed(endpoint_id, data)


def refresh_endpoint(app, endpoint_id: str) -> None:
    """Re-evaluate an endpoint after a policy change: start or stop as needed.

    Only affects endpoints whose agent is currently connected (recording can
    only run while frames are flowing).
    """
    if not hub.is_agent_online(endpoint_id):
        return
    policy = resolve_policy(endpoint_id)
    recording = manager.is_recording(endpoint_id)
    if policy is not None and not recording:
        on_agent_connected(app, endpoint_id)
    elif policy is None and recording:
        on_agent_disconnected(endpoint_id)
