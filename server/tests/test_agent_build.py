"""Rebuilding the agent binary from the console (spec sections 16, 18).

The console can rebuild the agent so that changing agent source does not require
a PowerShell session on the server. That is a build, not a shell: the command
line is fixed in code and takes nothing from the request. These tests pin the
authorization, the staleness detection the console relies on, and the fact that
no request input reaches the build.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.models import AuditLog, db
from app.models.audit import ACCESS_DENIED
from app.services import agent_build

from .conftest import auth_header


@pytest.fixture(autouse=True)
def _clean_builder():
    agent_build.builder.reset()
    yield
    agent_build.builder.reset()


def _agent_tree(tmp_path: Path, *, source: bool = True, binary: bool = True) -> Path:
    root = tmp_path / "agent"
    if source:
        src = root / "src" / "EndpointAgent"
        src.mkdir(parents=True)
        (src / "Program.cs").write_text("// agent", encoding="utf-8")
    if binary:
        publish = root / "publish"
        publish.mkdir(parents=True)
        (publish / "EndpointAgent.exe").write_bytes(b"MZ")
    return root


# --- staleness detection ---------------------------------------------------


def test_source_newer_than_binary_is_reported_stale(tmp_path):
    root = _agent_tree(tmp_path)
    binary = root / "publish" / "EndpointAgent.exe"

    # Binary built before the source changed.
    old = time.time() - 600
    os.utime(binary, (old, old))

    status = agent_build.source_status(root / "src", binary)
    assert status["sourceAvailable"] is True
    assert status["binaryExists"] is True
    assert status["stale"] is True


def test_binary_newer_than_source_is_not_stale(tmp_path):
    root = _agent_tree(tmp_path)
    source = root / "src" / "EndpointAgent" / "Program.cs"

    old = time.time() - 600
    os.utime(source, (old, old))

    assert agent_build.source_status(root / "src", root / "publish" / "EndpointAgent.exe")["stale"] is False


def test_missing_binary_is_stale(tmp_path):
    root = _agent_tree(tmp_path, binary=False)
    status = agent_build.source_status(root / "src", root / "publish" / "EndpointAgent.exe")
    assert status["binaryExists"] is False
    assert status["stale"] is True


def test_no_source_means_nothing_to_rebuild(tmp_path):
    root = _agent_tree(tmp_path, source=False)
    status = agent_build.source_status(root / "src", root / "publish" / "EndpointAgent.exe")
    assert status["sourceAvailable"] is False
    assert status["stale"] is False


def test_build_outputs_do_not_count_as_source(tmp_path):
    """bin/ and obj/ are produced by the build, so they must not make the tree
    look permanently newer than the binary that produced them."""
    root = _agent_tree(tmp_path)
    binary = root / "publish" / "EndpointAgent.exe"

    old = time.time() - 600
    os.utime(root / "src" / "EndpointAgent" / "Program.cs", (old, old))
    os.utime(binary, (old + 60, old + 60))

    artefact = root / "src" / "EndpointAgent" / "obj" / "Generated.cs"
    artefact.parent.mkdir(parents=True)
    artefact.write_text("// generated", encoding="utf-8")   # newest file in the tree

    assert agent_build.source_status(root / "src", binary)["stale"] is False


# --- authorization ---------------------------------------------------------


def test_a_plain_admin_cannot_start_a_rebuild(client, plain_admin_token, app, tmp_path):
    """Even a granted 'packages' admin: this replaces the binary that lands on
    every endpoint, which is more than minting one package."""
    from app.models import AdminFeatureGrant, User
    from app.models.user import ROLE_ADMIN

    admin = db.session.query(User).filter(User.role == ROLE_ADMIN).one()
    db.session.add(AdminFeatureGrant(user_id=admin.id, feature="packages"))
    db.session.commit()

    root = _agent_tree(tmp_path)
    app.config["AGENT_ROOT_PATH"] = str(root)
    app.config["AGENT_BINARY_PATH"] = str(root / "publish" / "EndpointAgent.exe")

    response = client.post("/api/packages/agent-build", headers=auth_header(plain_admin_token))
    assert response.status_code == 403
    assert agent_build.builder.is_running() is False

    denied = (
        db.session.query(AuditLog)
        .filter(AuditLog.action.in_(["REBUILD_AGENT", ACCESS_DENIED]))
        .count()
    )
    assert denied >= 1


def test_rebuild_requires_authentication(client):
    assert client.post("/api/packages/agent-build").status_code == 401


def test_rebuild_without_source_is_refused(client, super_admin_token, app, tmp_path):
    root = _agent_tree(tmp_path, source=False)
    app.config["AGENT_ROOT_PATH"] = str(root)
    app.config["AGENT_BINARY_PATH"] = str(root / "publish" / "EndpointAgent.exe")

    response = client.post("/api/packages/agent-build", headers=auth_header(super_admin_token))
    assert response.status_code == 409
    assert "原始碼" in response.get_json()["message"]
    assert agent_build.builder.is_running() is False


# --- status endpoint -------------------------------------------------------


def test_status_reports_staleness_to_the_console(client, super_admin_token, app, tmp_path):
    root = _agent_tree(tmp_path)
    binary = root / "publish" / "EndpointAgent.exe"
    old = time.time() - 600
    os.utime(binary, (old, old))

    app.config["AGENT_ROOT_PATH"] = str(root)
    app.config["AGENT_BINARY_PATH"] = str(binary)

    body = client.get(
        "/api/packages/agent-build", headers=auth_header(super_admin_token)
    ).get_json()

    assert body["stale"] is True
    assert body["sourceAvailable"] is True
    assert body["build"]["status"] == agent_build.STATUS_IDLE


def test_status_is_readable_without_being_super_admin(client, plain_admin_token, app, tmp_path):
    """Reading it is harmless and lets the page explain itself; only starting a
    build is restricted."""
    root = _agent_tree(tmp_path)
    app.config["AGENT_ROOT_PATH"] = str(root)
    app.config["AGENT_BINARY_PATH"] = str(root / "publish" / "EndpointAgent.exe")

    from app.models import AdminFeatureGrant, User
    from app.models.user import ROLE_ADMIN

    admin = db.session.query(User).filter(User.role == ROLE_ADMIN).one()
    db.session.add(AdminFeatureGrant(user_id=admin.id, feature="packages"))
    db.session.commit()

    response = client.get("/api/packages/agent-build", headers=auth_header(plain_admin_token))
    assert response.status_code == 200


# --- the build itself ------------------------------------------------------


def test_the_build_command_takes_nothing_from_the_request(monkeypatch, tmp_path):
    """Section 16 forbids unrestricted remote execution. The command line is
    fixed in code: only paths derived from server config appear in it."""
    root = _agent_tree(tmp_path)
    publish = root / "publish"
    captured: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        captured.append(list(command))
        return _Result()

    monkeypatch.setattr(agent_build.subprocess, "run", fake_run)

    agent_build.builder.start(dotnet="dotnet.exe", agent_root=root, publish_dir=publish)
    for _ in range(200):
        if not agent_build.builder.is_running():
            break
        time.sleep(0.01)

    assert len(captured) == 2
    assert captured[0][:3] == ["dotnet.exe", "publish", str(root / "src" / "EndpointAgent" / "EndpointAgent.csproj")]
    assert captured[1][:2] == ["dotnet.exe", "build"]
    # Nothing resembling a shell is involved.
    for command in captured:
        assert all(part not in ("&&", "|", ";", "cmd", "powershell") for part in command)


def test_build_output_is_decoded_as_utf8_not_the_system_locale(monkeypatch, tmp_path):
    """Regression: bare text=True decodes with the locale codec.

    On a Traditional Chinese Windows that is cp950, and dotnet emits bytes it
    cannot represent -- subprocess's reader thread then dies with
    UnicodeDecodeError and the whole build log is lost, exactly when a failure
    makes it worth reading. Found by running a real build; the mocked tests
    could not see it.
    """
    root = _agent_tree(tmp_path)
    seen: list[dict] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        seen.append(kwargs)
        return _Result()

    monkeypatch.setattr(agent_build.subprocess, "run", fake_run)

    agent_build.builder.start(dotnet="dotnet.exe", agent_root=root, publish_dir=root / "publish")
    for _ in range(200):
        if not agent_build.builder.is_running():
            break
        time.sleep(0.01)

    assert seen, "the build never ran"
    for kwargs in seen:
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"
        assert "text" not in kwargs


def test_a_failed_build_is_reported_not_swallowed(monkeypatch, tmp_path):
    root = _agent_tree(tmp_path)

    class _Result:
        returncode = 1
        stdout = "error CS1002: ; expected"
        stderr = ""

    monkeypatch.setattr(agent_build.subprocess, "run", lambda command, **kw: _Result())

    agent_build.builder.start(dotnet="dotnet.exe", agent_root=root, publish_dir=root / "publish")
    for _ in range(200):
        if not agent_build.builder.is_running():
            break
        time.sleep(0.01)

    state = agent_build.builder.status()
    assert state["status"] == agent_build.STATUS_FAILED
    assert "CS1002" in state["output"]


def test_only_one_build_runs_at_a_time(monkeypatch, tmp_path):
    root = _agent_tree(tmp_path)
    release = {"go": False}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def slow_run(command, **kwargs):
        while not release["go"]:
            time.sleep(0.005)
        return _Result()

    monkeypatch.setattr(agent_build.subprocess, "run", slow_run)

    assert agent_build.builder.start(
        dotnet="dotnet.exe", agent_root=root, publish_dir=root / "publish") is True
    assert agent_build.builder.start(
        dotnet="dotnet.exe", agent_root=root, publish_dir=root / "publish") is False

    release["go"] = True
    for _ in range(400):
        if not agent_build.builder.is_running():
            break
        time.sleep(0.01)
