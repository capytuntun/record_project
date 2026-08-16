"""End-to-end screen streaming over a real WebSocket.

Flask's test client cannot speak WebSocket, so this test runs the app on a real
loopback port and connects genuine WebSocket clients: one impersonating an
agent, one a viewer. It proves the whole relay -- agent authenticates, viewer
redeems a ticket, a frame pushed by the agent arrives at the viewer, monitor
switches reach the agent, and an unauthorised socket is rejected.

Skipped if websocket-client is not installed.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

websocket = pytest.importorskip("websocket")  # websocket-client

from app import create_app  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.models import User, db  # noqa: E402
from app.models.user import ROLE_SUPER_ADMIN  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LiveServer:
    """A real app on a real port, sharing one in-memory database."""

    def __init__(self):
        self.port = _free_port()
        # A single shared connection so the server thread and the test see the
        # same in-memory SQLite database.
        self.config = TestConfig()
        self.config.SQLALCHEMY_DATABASE_URI = "sqlite://"
        self.app = create_app(self.config)
        from sqlalchemy.pool import StaticPool

        self.app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
        # Rebind the engine with the shared-pool options.
        with self.app.app_context():
            db.engine.dispose()
        self._thread = None

    def __enter__(self):
        with self.app.app_context():
            db.create_all()
            user = User(
                username="root.admin",
                password_hash=hash_password("Sup3r-Admin-Passw0rd!"),
                role=ROLE_SUPER_ADMIN,
            )
            db.session.add(user)
            db.session.commit()

        from werkzeug.serving import make_server

        self.server = make_server("127.0.0.1", self.port, self.app, threaded=True)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        _wait_for_port(self.port)
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self._thread.join(timeout=5)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def ws_url(self, path: str) -> str:
        return f"ws://127.0.0.1:{self.port}{path}"


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def _post(server, path, body=None, token=None):
    import urllib.error
    import urllib.request

    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(server.url(path), data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _enroll_agent(server, token):
    _, created = _post(server, "/api/enrollment-tokens",
                       {"label": "ws-test", "days": 30}, token)
    _, enrolled = _post(server, "/api/agent/enroll",
                       {"enrollmentToken": created["token"], "deviceName": "WS-DEV"})
    return enrolled


@pytest.fixture
def server():
    with LiveServer() as s:
        yield s


def _login(server):
    _, body = _post(server, "/api/auth/login",
                   {"username": "root.admin", "password": "Sup3r-Admin-Passw0rd!"})
    return body["accessToken"]


def test_frame_from_agent_reaches_viewer(server):
    token = _login(server)
    agent = _enroll_agent(server, token)

    # Agent connects with its device credential in the header.
    agent_ws = websocket.create_connection(
        server.ws_url("/api/agent/screen/ws"),
        header=[f"Authorization: Bearer {agent['deviceCredential']}"],
        timeout=5,
    )
    agent_ws.send(json.dumps({
        "type": "monitors",
        "monitors": [{"index": 0, "width": 1920, "height": 1080, "primary": True}],
    }))

    # Viewer gets a ticket over REST, then opens its socket with it.
    _, ticket = _post(server, f"/api/endpoints/{agent['endpointId']}/screen/ticket",
                      token=token)
    viewer_ws = websocket.create_connection(server.ws_url(ticket["wsPath"]), timeout=5)

    # Viewer should receive status + monitor list on connect, and the agent
    # should be told to start capturing.
    got_monitors = False
    got_start = False
    deadline = time.time() + 3
    while time.time() < deadline and not (got_monitors and got_start):
        viewer_ws.settimeout(0.5)
        try:
            msg = viewer_ws.recv()
            if isinstance(msg, str) and json.loads(msg).get("type") == "monitors":
                got_monitors = True
        except Exception:
            pass
        agent_ws.settimeout(0.5)
        try:
            msg = agent_ws.recv()
            if isinstance(msg, str) and json.loads(msg).get("type") == "start":
                got_start = True
        except Exception:
            pass

    assert got_start, "agent was not told to start capturing when a viewer joined"
    assert got_monitors, "viewer did not receive the monitor list"

    # Agent pushes a JPEG frame; the viewer must receive the same bytes.
    frame = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"fake-jpeg-payload" * 20
    agent_ws.send_binary(frame)

    viewer_ws.settimeout(3)
    received = None
    for _ in range(10):
        msg = viewer_ws.recv()
        if isinstance(msg, (bytes, bytearray)):
            received = bytes(msg)
            break
    assert received == frame, "viewer did not receive the frame the agent sent"

    # Monitor switch from the viewer reaches the agent.
    viewer_ws.send(json.dumps({"type": "set_monitor", "index": 1}))
    agent_ws.settimeout(3)
    switched = None
    for _ in range(10):
        msg = agent_ws.recv()
        if isinstance(msg, str) and json.loads(msg).get("type") == "set_monitor":
            switched = json.loads(msg)["index"]
            break
    assert switched == 1

    # When the viewer leaves, the agent is told to stop.
    viewer_ws.close()
    agent_ws.settimeout(3)
    stopped = False
    for _ in range(10):
        try:
            msg = agent_ws.recv()
        except Exception:
            break
        if isinstance(msg, str) and json.loads(msg).get("type") == "stop":
            stopped = True
            break
    assert stopped, "agent was not told to stop when the last viewer left"

    agent_ws.close()


def test_agent_socket_rejects_a_bad_credential(server):
    ws = websocket.create_connection(
        server.ws_url("/api/agent/screen/ws"),
        header=["Authorization: Bearer not-a-real-credential"],
        timeout=5,
    )
    ws.settimeout(3)
    msg = ws.recv()
    assert json.loads(msg)["type"] == "error"
    ws.close()


def test_viewer_socket_rejects_a_bad_ticket(server):
    token = _login(server)
    agent = _enroll_agent(server, token)
    ws = websocket.create_connection(
        server.ws_url(f"/api/endpoints/{agent['endpointId']}/screen/ws?ticket=garbage"),
        timeout=5,
    )
    ws.settimeout(3)
    msg = ws.recv()
    assert json.loads(msg)["type"] == "error"
    ws.close()


def test_viewer_ticket_is_bound_to_its_endpoint(server):
    token = _login(server)
    agent_a = _enroll_agent(server, token)
    agent_b = _enroll_agent(server, token)

    _, ticket = _post(server, f"/api/endpoints/{agent_a['endpointId']}/screen/ticket",
                      token=token)
    raw = ticket["ticket"]

    # Present A's ticket on B's socket: must be refused.
    ws = websocket.create_connection(
        server.ws_url(f"/api/endpoints/{agent_b['endpointId']}/screen/ws?ticket={raw}"),
        timeout=5,
    )
    ws.settimeout(3)
    msg = ws.recv()
    assert json.loads(msg)["type"] == "error"
    ws.close()
