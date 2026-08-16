"""Storage targets: backends (local/FTP/SMB), credential sealing, the multi-target
settings API, and a full screenshot round-trip to a NAS (over a real in-process
FTP server).

SMB's happy path needs a live SMB server, so only its clean-failure path is
covered here; FTP stands in as the end-to-end proof that screen data actually
travels to a remote target and back."""

from __future__ import annotations

import threading

import pytest

from app.models import RecordingPolicy, Screenshot, StorageTarget, db
from app.services.storage import (
    default_target_id,
    load_file,
    remove_file,
    store_file,
)
from app.services.storage.ftp import FtpBackend
from app.services.storage.local import LocalBackend
from app.services.storage.secrets import seal, unseal
from app.services.storage.smb import SmbBackend

from .conftest import auth_header
from .test_endpoints import create_enrollment_token, enroll

# a tiny but valid JPEG
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010101006000600000ffdb004300080606070605080707"
    "07090908"
) + b"\x00" * 32 + b"\xff\xd9"


# --- FTP server fixture ----------------------------------------------------

@pytest.fixture
def ftp_server(tmp_path):
    """A real FTP server on a random localhost port, rooted at a temp dir."""
    pyftpdlib_authorizers = pytest.importorskip("pyftpdlib.authorizers")
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    home = tmp_path / "ftproot"
    home.mkdir()
    authorizer = pyftpdlib_authorizers.DummyAuthorizer()
    authorizer.add_user("nasuser", "naspass", str(home), perm="elradfmwMT")

    handler = FTPHandler
    handler.authorizer = authorizer
    server = FTPServer(("127.0.0.1", 0), handler)
    port = server.address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"host": "127.0.0.1", "port": port, "user": "nasuser",
               "password": "naspass", "home": home}
    finally:
        server.close_all()


def _ftp_body(ftp_server, **extra):
    body = {"name": "主 NAS", "backend": "FTP", "host": ftp_server["host"],
            "port": ftp_server["port"], "username": ftp_server["user"],
            "password": ftp_server["password"], "basePath": "eem", "useTls": False}
    body.update(extra)
    return body


# --- backends --------------------------------------------------------------

def test_local_backend_roundtrip(tmp_path):
    backend = LocalBackend(str(tmp_path / "rec"), str(tmp_path / "shot"))
    backend.put("recordings", "a/b.enc", b"hello")
    assert backend.get("recordings", "a/b.enc") == b"hello"
    ok, _ = backend.test()
    assert ok
    backend.remove("recordings", "a/b.enc")
    from app.services.storage.base import StorageError
    with pytest.raises(StorageError):
        backend.get("recordings", "a/b.enc")


def test_ftp_backend_roundtrip(ftp_server):
    backend = FtpBackend(ftp_server["host"], ftp_server["port"], ftp_server["user"],
                         ftp_server["password"], base_path="eem", use_tls=False)
    ok, message = backend.test()
    assert ok, message

    backend.put("recordings", "dev1/seg.enc", b"ciphertext-bytes")
    stored = ftp_server["home"] / "eem" / "recordings" / "dev1" / "seg.enc"
    assert stored.is_file()
    assert stored.read_bytes() == b"ciphertext-bytes"

    assert backend.get("recordings", "dev1/seg.enc") == b"ciphertext-bytes"
    backend.remove("recordings", "dev1/seg.enc")
    assert not stored.exists()


def test_ftp_backend_bad_password_fails_cleanly(ftp_server):
    backend = FtpBackend(ftp_server["host"], ftp_server["port"], ftp_server["user"],
                         "wrong-password", base_path="eem", use_tls=False)
    ok, message = backend.test()
    assert ok is False
    assert message


def test_smb_backend_unreachable_fails_cleanly():
    backend = SmbBackend("127.0.0.1", 1, "share", "user", "pass", "", "eem")
    ok, message = backend.test()
    assert ok is False
    assert message


# --- credential sealing ----------------------------------------------------

def test_secret_seal_roundtrip():
    sealed = seal("server-secret", "nas-password")
    assert sealed != "nas-password"
    assert unseal("server-secret", sealed) == "nas-password"


# --- facade: target routing + fallback -------------------------------------

def test_store_local_when_no_target(app, tmp_path):
    app.config["RECORDING_DIR"] = str(tmp_path / "rec")
    app.config["SCREENSHOT_DIR"] = str(tmp_path / "shot")
    location, size = store_file(app, "recordings", "x/y.enc", b"data", target_id=None)
    assert location == "LOCAL"
    assert size == 4
    assert load_file(app, "recordings", "x/y.enc", "LOCAL") == b"data"


def test_store_uses_named_target(app, monkeypatch):
    import app.services.storage as storage_mod

    seen = {}

    class Remote:
        type = "FTP"
        is_local = False

        def put(self, kind, filename, data):
            seen["put"] = (kind, filename, data)
            return len(data)

        def get(self, kind, filename):
            return seen["put"][2]

    monkeypatch.setattr(storage_mod, "backend_for_target", lambda _a, tid: Remote())
    location, _ = store_file(app, "recordings", "a/b.enc", b"data", target_id="TARGET-1")
    assert location == "TARGET-1"                    # landed on the named target
    assert load_file(app, "recordings", "a/b.enc", "TARGET-1") == b"data"


def test_store_falls_back_to_local_on_remote_failure(app, tmp_path, monkeypatch):
    app.config["RECORDING_DIR"] = str(tmp_path / "rec")
    app.config["SCREENSHOT_DIR"] = str(tmp_path / "shot")
    import app.services.storage as storage_mod
    from app.services.storage.base import StorageError

    class FailingRemote:
        type = "FTP"
        is_local = False

        def put(self, *a, **k):
            raise StorageError("NAS down")

    monkeypatch.setattr(storage_mod, "backend_for_target", lambda _a, tid: FailingRemote())
    location, size = store_file(app, "recordings", "x/y.enc", b"data", target_id="T9")
    assert location == "LOCAL"                        # degraded to local, not lost
    assert load_file(app, "recordings", "x/y.enc", "LOCAL") == b"data"
    remove_file(app, "recordings", "x/y.enc", "LOCAL")


# --- settings API ----------------------------------------------------------

def test_targets_empty_by_default(client, super_admin_token):
    r = client.get("/api/storage/targets", headers=auth_header(super_admin_token))
    assert r.status_code == 200
    assert r.get_json()["items"] == []


def test_targets_super_admin_only(client, plain_admin_token):
    assert client.get("/api/storage/targets",
                      headers=auth_header(plain_admin_token)).status_code == 403
    r = client.post("/api/storage/targets", json={"name": "x", "backend": "FTP", "host": "h"},
                    headers=auth_header(plain_admin_token))
    assert r.status_code == 403


def test_create_smb_requires_password(client, super_admin_token):
    r = client.post("/api/storage/targets",
                    json={"name": "nas", "backend": "SMB", "host": "nas",
                          "share": "recordings", "username": "u"},
                    headers=auth_header(super_admin_token))
    assert r.status_code == 400


def test_create_target_never_returns_password(client, super_admin_token, ftp_server):
    r = client.post("/api/storage/targets", json=_ftp_body(ftp_server),
                    headers=auth_header(super_admin_token))
    assert r.status_code == 201
    body = r.get_json()
    assert body["backend"] == "FTP"
    assert body["hasPassword"] is True
    assert "password" not in body and "secretSealed" not in body

    row = db.session.get(StorageTarget, body["id"])
    assert row.secret_sealed and ftp_server["password"] not in row.secret_sealed


def test_multiple_targets_and_single_default(client, super_admin_token, ftp_server):
    a = client.post("/api/storage/targets", json=_ftp_body(ftp_server, name="A", isDefault=True),
                    headers=auth_header(super_admin_token)).get_json()
    b = client.post("/api/storage/targets", json=_ftp_body(ftp_server, name="B", isDefault=True),
                    headers=auth_header(super_admin_token)).get_json()
    items = client.get("/api/storage/targets",
                       headers=auth_header(super_admin_token)).get_json()["items"]
    assert len(items) == 2
    defaults = [t["id"] for t in items if t["isDefault"]]
    assert defaults == [b["id"]]          # setting B default cleared A


def test_delete_target_blocked_while_in_use(client, app, super_admin_token, ftp_server):
    app.config["RECORDING_KEY_PASSPHRASE"] = "k"
    app.config["RECORDING_ENABLED"] = True
    ep = _enroll(client, super_admin_token, "POL-EP")
    target = client.post("/api/storage/targets", json=_ftp_body(ftp_server),
                         headers=auth_header(super_admin_token)).get_json()
    client.post("/api/recordings/policies",
                json={"targetType": "ENDPOINT", "targetId": ep, "storageTargetId": target["id"]},
                headers=auth_header(super_admin_token))
    # In use -> cannot delete.
    r = client.delete(f"/api/storage/targets/{target['id']}",
                      headers=auth_header(super_admin_token))
    assert r.status_code == 409


def test_test_connection_ok_and_bad(client, super_admin_token, ftp_server):
    ok = client.post("/api/storage/targets/test", json=_ftp_body(ftp_server),
                     headers=auth_header(super_admin_token))
    assert ok.status_code == 200 and ok.get_json()["ok"] is True
    bad = client.post("/api/storage/targets/test", json=_ftp_body(ftp_server, password="nope"),
                      headers=auth_header(super_admin_token))
    assert bad.status_code == 200 and bad.get_json()["ok"] is False


# --- the money test: a screenshot really travels to the NAS and back -------

def _enroll(client, super_admin_token, name):
    created = create_enrollment_token(client, super_admin_token, label=name)
    return enroll(client, created["token"], deviceName=name).get_json()["endpointId"]


def test_screenshot_round_trips_through_default_target(
    client, app, super_admin_token, ftp_server, tmp_path
):
    app.config["RECORDING_KEY_PASSPHRASE"] = "screen-key"
    app.config["RECORDING_DIR"] = str(tmp_path / "rec")
    app.config["SCREENSHOT_DIR"] = str(tmp_path / "shot")
    ep = _enroll(client, super_admin_token, "NAS-EP")

    target = client.post("/api/storage/targets", json=_ftp_body(ftp_server, isDefault=True),
                         headers=auth_header(super_admin_token)).get_json()
    assert default_target_id(app) == target["id"]

    r = client.post(f"/api/endpoints/{ep}/screenshot", data=JPEG,
                    content_type="image/jpeg", headers=auth_header(super_admin_token))
    assert r.status_code == 201, r.get_json()
    shot_id = r.get_json()["id"]

    row = db.session.get(Screenshot, shot_id)
    assert row.storage_backend == target["id"]        # recorded as living on this target
    on_nas = ftp_server["home"] / "eem" / "screenshots" / ep / row.filename.split("/")[-1]
    assert on_nas.is_file()
    assert on_nas.read_bytes() != JPEG                # encrypted at rest
    assert not on_nas.read_bytes().startswith(b"\xff\xd8")

    got = client.get(f"/api/screenshots/{shot_id}/image", headers=auth_header(super_admin_token))
    assert got.status_code == 200 and got.data == JPEG

    assert client.delete(f"/api/screenshots/{shot_id}",
                         headers=auth_header(super_admin_token)).status_code == 200
    assert not on_nas.exists()
