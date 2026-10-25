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
from app.services.signing import SigningSettings

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


# --- signing the rebuilt exe ---------------------------------------------------


def _wait_for_build() -> None:
    for _ in range(400):
        if not agent_build.builder.is_running():
            break
        time.sleep(0.01)


def _signing(tmp_path: Path, *, pfx_exists: bool = True) -> SigningSettings:
    pfx = tmp_path / "signing.pfx"
    if pfx_exists:
        pfx.write_bytes(b"not really a pfx")
    return SigningSettings(
        pfx_path=pfx, password="pfx-pw-s3cret", script=r"C:\agent\signing\Sign-File.ps1",
        timestamp_url="http://timestamp.example",
    )


def test_a_configured_certificate_signs_the_rebuilt_exe(monkeypatch, tmp_path):
    """Same as agent/build.ps1: without this, a console rebuild replaced a
    signed exe with an unsigned one."""
    root = _agent_tree(tmp_path)
    publish = root / "publish"
    calls: list[tuple[list[str], dict]] = []

    class _Result:
        returncode = 0
        stdout = "Signed"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs))
        return _Result()

    monkeypatch.setattr(agent_build.subprocess, "run", fake_run)

    agent_build.builder.start(dotnet="dotnet.exe", agent_root=root, publish_dir=publish,
                              signing=_signing(tmp_path))
    _wait_for_build()

    assert len(calls) == 3, "publish, custom action, then sign"
    sign_cmd, sign_kwargs = calls[2]
    assert sign_cmd[0] == "powershell"
    assert sign_cmd[sign_cmd.index("-File") + 1] == r"C:\agent\signing\Sign-File.ps1"
    assert str(publish / "EndpointAgent.exe") in sign_cmd
    assert sign_cmd[sign_cmd.index("-CertPath") + 1] == str(tmp_path / "signing.pfx")
    assert sign_cmd[sign_cmd.index("-TimestampUrl") + 1] == "http://timestamp.example"
    # The password goes through the environment, never the process list.
    assert all("pfx-pw-s3cret" not in part for command, _ in calls for part in command)
    assert sign_kwargs["env"]["EEM_SIGNING_PASSWORD"] == "pfx-pw-s3cret"
    assert sign_kwargs["shell"] is False

    state = agent_build.builder.status()
    assert state["status"] == agent_build.STATUS_SUCCEEDED
    assert "已簽章" in state["message"]


def test_a_failed_signature_fails_the_rebuild(monkeypatch, tmp_path):
    """An exe that was meant to be signed must not be reported as a good build."""
    root = _agent_tree(tmp_path)

    class _Result:
        stdout = ""
        stderr = ""

        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(command, **kwargs):
        return _Result(1 if command[0] == "powershell" else 0)

    monkeypatch.setattr(agent_build.subprocess, "run", fake_run)

    agent_build.builder.start(dotnet="dotnet.exe", agent_root=root, publish_dir=root / "publish",
                              signing=_signing(tmp_path))
    _wait_for_build()

    state = agent_build.builder.status()
    assert state["status"] == agent_build.STATUS_FAILED
    assert "簽章失敗" in state["message"]


def test_a_missing_certificate_fails_without_running_the_signer(monkeypatch, tmp_path):
    root = _agent_tree(tmp_path)
    commands: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        commands.append(list(command))
        return _Result()

    monkeypatch.setattr(agent_build.subprocess, "run", fake_run)

    agent_build.builder.start(dotnet="dotnet.exe", agent_root=root, publish_dir=root / "publish",
                              signing=_signing(tmp_path, pfx_exists=False))
    _wait_for_build()

    assert [c[0] for c in commands] == ["dotnet.exe", "dotnet.exe"]
    state = agent_build.builder.status()
    assert state["status"] == agent_build.STATUS_FAILED
    assert "憑證不存在" in state["message"]


def test_without_a_certificate_the_success_message_does_not_claim_signed(monkeypatch, tmp_path):
    root = _agent_tree(tmp_path)

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(agent_build.subprocess, "run", lambda command, **kw: _Result())

    agent_build.builder.start(dotnet="dotnet.exe", agent_root=root, publish_dir=root / "publish")
    _wait_for_build()

    state = agent_build.builder.status()
    assert state["status"] == agent_build.STATUS_SUCCEEDED
    assert "簽章" not in state["message"]


@pytest.mark.parametrize("configured", [True, False])
def test_the_console_rebuild_uses_the_server_certificate(
    client, super_admin_token, app, tmp_path, monkeypatch, configured
):
    root = _agent_tree(tmp_path)
    app.config["AGENT_ROOT_PATH"] = str(root)
    app.config["AGENT_BINARY_PATH"] = str(root / "publish" / "EndpointAgent.exe")
    app.config["SIGNING_ENABLED"] = configured
    app.config["SIGNING_PFX_PATH"] = str(tmp_path / "server.pfx") if configured else None
    app.config["SIGNING_PASSWORD"] = "server-pw" if configured else None
    app.config["SIGNING_SCRIPT"] = r"C:\agent\signing\Sign-File.ps1"
    app.config["SIGNING_TIMESTAMP_URL"] = None

    started: list[dict] = []
    monkeypatch.setattr(agent_build, "find_dotnet", lambda configured="": "dotnet.exe")
    monkeypatch.setattr(agent_build.builder, "start",
                        lambda **kwargs: started.append(kwargs) or True)

    response = client.post("/api/packages/agent-build", headers=auth_header(super_admin_token))
    assert response.status_code == 202

    signing = started[0]["signing"]
    if configured:
        assert signing.pfx_path == tmp_path / "server.pfx"
        assert signing.password == "server-pw"
        assert signing.timestamp_url is None
    else:
        assert signing is None


def test_signing_settings_never_show_the_password():
    settings = SigningSettings(pfx_path=Path("C:/x.pfx"), password="hunter2", script="s.ps1")
    assert "hunter2" not in repr(settings)
