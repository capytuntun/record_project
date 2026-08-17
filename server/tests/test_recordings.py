"""Recording policy API + segment scope (Phase 2). No FFmpeg needed here --
these exercise policy CRUD, RBAC, and segment-listing scope, not the encoder."""

from __future__ import annotations

from datetime import timedelta

from app.models import (
    AuditLog,
    Endpoint,
    EndpointGroup,
    EndpointGroupMember,
    RecordingPolicy,
    RecordingSegment,
    db,
    utcnow,
)
from app.models.audit import CHANGE_RECORDING_POLICY
from app.services.recording_control import resolve_policy

from .conftest import auth_header
from .test_endpoints import create_enrollment_token, enroll


def _enroll(client, super_admin_token, name):
    created = create_enrollment_token(client, super_admin_token, label=name)
    return enroll(client, created["token"], deviceName=name).get_json()["endpointId"]


def _enable_recording(app):
    # Pretend recording is configured so the API's guard passes; the encoder is
    # never invoked in these tests (no agent frames flow). RECORDING_ENABLED is
    # a static value in app.config (Flask evaluates the Config property once at
    # startup), so set it directly.
    app.config["RECORDING_KEY_PASSPHRASE"] = "test-passphrase"
    app.config["RECORDING_ENABLED"] = True


def test_create_endpoint_policy(client, app, super_admin_token):
    _enable_recording(app)
    ep = _enroll(client, super_admin_token, "REC-A")

    r = client.post("/api/recordings/policies",
                    json={"targetType": "ENDPOINT", "targetId": ep,
                          "mode": "DIFFERENTIAL", "fps": 5, "retentionDays": 30},
                    headers=auth_header(super_admin_token))
    assert r.status_code == 201
    body = r.get_json()
    assert body["enabled"] is True
    assert body["mode"] == "DIFFERENTIAL"

    assert db.session.query(AuditLog).filter(
        AuditLog.action == CHANGE_RECORDING_POLICY).count() == 1
    # A policy that names no target stores locally (storageTargetId is null).
    assert body["storageTargetId"] is None


def _make_ftp_target(client, super_admin_token, name="NAS"):
    return client.post("/api/storage/targets",
                       json={"name": name, "backend": "FTP", "host": "127.0.0.1",
                             "port": 21, "username": "u", "password": "p",
                             "basePath": "eem", "useTls": False},
                       headers=auth_header(super_admin_token)).get_json()


def test_policy_storage_target_persists(client, app, super_admin_token):
    _enable_recording(app)
    ep = _enroll(client, super_admin_token, "REC-STORE")
    target = _make_ftp_target(client, super_admin_token)

    r = client.post("/api/recordings/policies",
                    json={"targetType": "ENDPOINT", "targetId": ep,
                          "storageTargetId": target["id"]},
                    headers=auth_header(super_admin_token))
    assert r.status_code == 201
    pid = r.get_json()["id"]
    assert r.get_json()["storageTargetId"] == target["id"]
    assert r.get_json()["storageTargetName"] == "NAS"

    # And it can be switched back to local (null).
    u = client.patch(f"/api/recordings/policies/{pid}", json={"storageTargetId": None},
                     headers=auth_header(super_admin_token))
    assert u.status_code == 200
    assert u.get_json()["storageTargetId"] is None


def test_policy_rejects_unknown_storage_target(client, app, super_admin_token):
    _enable_recording(app)
    ep = _enroll(client, super_admin_token, "REC-BADSTORE")
    r = client.post("/api/recordings/policies",
                    json={"targetType": "ENDPOINT", "targetId": ep,
                          "storageTargetId": "does-not-exist"},
                    headers=auth_header(super_admin_token))
    assert r.status_code == 400


def test_recording_disabled_rejects_policy(client, app, super_admin_token):
    app.config["RECORDING_KEY_PASSPHRASE"] = None  # not configured
    ep = _enroll(client, super_admin_token, "REC-OFF")
    r = client.post("/api/recordings/policies",
                    json={"targetType": "ENDPOINT", "targetId": ep},
                    headers=auth_header(super_admin_token))
    assert r.status_code == 409


def test_duplicate_policy_rejected(client, app, super_admin_token):
    _enable_recording(app)
    ep = _enroll(client, super_admin_token, "REC-DUP")
    body = {"targetType": "ENDPOINT", "targetId": ep}
    assert client.post("/api/recordings/policies", json=body,
                       headers=auth_header(super_admin_token)).status_code == 201
    assert client.post("/api/recordings/policies", json=body,
                       headers=auth_header(super_admin_token)).status_code == 409


def test_plain_admin_cannot_manage_policies(client, app, super_admin_token,
                                            plain_admin_token):
    _enable_recording(app)
    ep = _enroll(client, super_admin_token, "REC-P")
    assert client.post("/api/recordings/policies",
                       json={"targetType": "ENDPOINT", "targetId": ep},
                       headers=auth_header(plain_admin_token)).status_code == 403
    assert client.get("/api/recordings/policies",
                      headers=auth_header(plain_admin_token)).status_code == 403


def test_toggle_and_delete_policy(client, app, super_admin_token):
    _enable_recording(app)
    ep = _enroll(client, super_admin_token, "REC-T")
    pid = client.post("/api/recordings/policies",
                      json={"targetType": "ENDPOINT", "targetId": ep},
                      headers=auth_header(super_admin_token)).get_json()["id"]

    off = client.patch(f"/api/recordings/policies/{pid}", json={"enabled": False},
                       headers=auth_header(super_admin_token))
    assert off.status_code == 200 and off.get_json()["enabled"] is False

    assert client.delete(f"/api/recordings/policies/{pid}",
                         headers=auth_header(super_admin_token)).status_code == 200
    assert db.session.get(RecordingPolicy, pid) is None


def test_group_policy_resolves_for_member(client, app, super_admin_token):
    _enable_recording(app)
    ep = _enroll(client, super_admin_token, "REC-G")
    gid = client.post("/api/groups", json={"name": "RecGroup"},
                      headers=auth_header(super_admin_token)).get_json()["id"]
    client.put(f"/api/groups/{gid}/members", json={"endpointIds": [ep]},
               headers=auth_header(super_admin_token))
    client.post("/api/recordings/policies",
                json={"targetType": "GROUP", "targetId": gid, "mode": "FULL"},
                headers=auth_header(super_admin_token))

    # The endpoint should resolve to the group policy.
    policy = resolve_policy(ep)
    assert policy is not None
    assert policy.mode == "FULL"


def test_endpoint_policy_wins_over_group(client, app, super_admin_token):
    _enable_recording(app)
    ep = _enroll(client, super_admin_token, "REC-W")
    gid = client.post("/api/groups", json={"name": "RecG2"},
                      headers=auth_header(super_admin_token)).get_json()["id"]
    client.put(f"/api/groups/{gid}/members", json={"endpointIds": [ep]},
               headers=auth_header(super_admin_token))
    client.post("/api/recordings/policies",
                json={"targetType": "GROUP", "targetId": gid, "mode": "FULL"},
                headers=auth_header(super_admin_token))
    client.post("/api/recordings/policies",
                json={"targetType": "ENDPOINT", "targetId": ep, "mode": "DIFFERENTIAL"},
                headers=auth_header(super_admin_token))

    policy = resolve_policy(ep)
    assert policy.mode == "DIFFERENTIAL"  # endpoint-level wins


def test_segments_listed_by_time_and_scope(client, app, super_admin, super_admin_token,
                                           plain_admin, plain_admin_token):
    ep = _enroll(client, super_admin_token, "REC-SEG")
    # Insert two segment index rows directly (no encoder needed).
    now = utcnow()
    for i in range(2):
        db.session.add(RecordingSegment(
            endpoint_id=ep, mode="DIFFERENTIAL", filename=f"{ep}/x{i}.mp4.enc",
            started_at=now - timedelta(minutes=10 - i), ended_at=now - timedelta(minutes=9 - i),
            size_bytes=1000, frame_count=0, expires_at=now + timedelta(days=30)))
    db.session.commit()

    listed = client.get(f"/api/recordings/endpoints/{ep}/segments",
                        headers=auth_header(super_admin_token)).get_json()
    assert listed["total"] == 2

    # A plain admin with no scope for this endpoint cannot list its recordings.
    assert client.get(f"/api/recordings/endpoints/{ep}/segments",
                      headers=auth_header(plain_admin_token)).status_code == 404


def test_retention_sweep_deletes_expired(client, app, super_admin_token, tmp_path):
    ep = _enroll(client, super_admin_token, "REC-EXP")
    app.config["RECORDING_DIR"] = str(tmp_path)
    # one expired, one live
    expired_file = tmp_path / "old.mp4.enc"
    expired_file.write_bytes(b"x")
    now = utcnow()
    db.session.add(RecordingSegment(
        endpoint_id=ep, mode="DIFFERENTIAL", filename="old.mp4.enc",
        started_at=now - timedelta(days=40), ended_at=now - timedelta(days=40),
        size_bytes=1, frame_count=0, expires_at=now - timedelta(days=1)))
    db.session.add(RecordingSegment(
        endpoint_id=ep, mode="DIFFERENTIAL", filename="new.mp4.enc",
        started_at=now, ended_at=now, size_bytes=1, frame_count=0,
        expires_at=now + timedelta(days=30)))
    db.session.commit()

    from app.services.retention import sweep_expired
    removed = sweep_expired(app)
    assert removed == 1
    assert not expired_file.exists()
    assert db.session.query(RecordingSegment).count() == 1


# --- export (multi-select download) -----------------------------------------

def _seed_segments(app, ep, tmp_path, count=2, *, passphrase="test-passphrase",
                   write_files=True):
    """Insert `count` segment rows for `ep` and (optionally) their encrypted files.

    Returns [(segment_id, plaintext_bytes)] in chronological order.
    """
    from app.services.recording_crypto import derive_key, encrypt_bytes

    app.config["RECORDING_DIR"] = str(tmp_path)
    app.config["RECORDING_KEY_PASSPHRASE"] = passphrase
    key = derive_key(passphrase)
    now = utcnow().replace(microsecond=0)
    out = []
    for i in range(count):
        plaintext = f"fake-mp4-{i}-".encode() * 100
        rel = f"{ep}/x{i}.mp4.enc"
        if write_files:
            path = tmp_path / ep / f"x{i}.mp4.enc"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encrypt_bytes(key, plaintext))
        seg = RecordingSegment(
            endpoint_id=ep, mode="DIFFERENTIAL", filename=rel,
            started_at=now - timedelta(minutes=10 - i * 5),
            ended_at=now - timedelta(minutes=5 - i * 5),
            size_bytes=len(plaintext), frame_count=0,
            expires_at=now + timedelta(days=30),
        )
        db.session.add(seg)
        db.session.flush()
        out.append((seg.id, plaintext))
    db.session.commit()
    return out


def _unzip(data: bytes):
    import io
    import zipfile
    return zipfile.ZipFile(io.BytesIO(data))


def test_export_streams_zip_of_decrypted_mp4s(client, app, super_admin, super_admin_token,
                                              tmp_path):
    ep = _enroll(client, super_admin_token, "REC-EXPORT")
    seeded = _seed_segments(app, ep, tmp_path, count=2)

    resp = client.post("/api/recordings/segments/export",
                       json={"segmentIds": [s[0] for s in reversed(seeded)],
                             "tzOffsetMinutes": 480},
                       headers=auth_header(super_admin_token))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.mimetype == "application/zip"
    assert "attachment" in resp.headers["Content-Disposition"]
    assert "recording_REC-EXPORT_" in resp.headers["Content-Disposition"]

    archive = _unzip(resp.get_data())
    assert archive.testzip() is None   # streamed (data-descriptor) archive is well-formed
    names = archive.namelist()
    mp4s = [n for n in names if n.endswith(".mp4")]
    assert len(mp4s) == 2 and "manifest.json" in names
    # Chronological, named after the device + local (UTC+8) time span.
    assert all(n.startswith("REC-EXPORT_") for n in mp4s)
    assert mp4s == sorted(mp4s)
    # Contents are the plaintext MP4 bytes, not the ciphertext.
    for name, (_, plaintext) in zip(mp4s, seeded):
        assert archive.read(name) == plaintext

    import json
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["exportedBy"] == super_admin.username
    assert manifest["endpointId"] == ep
    assert manifest["timezoneOffsetMinutes"] == 480
    assert [m["segmentId"] for m in manifest["segments"]] == [s[0] for s in seeded]
    assert manifest["missing"] == []
    assert all(m["integrity"] == "ok" for m in manifest["segments"])

    # One audit entry per export, naming every segment and the overall span.
    from app.models.audit import EXPORT_RECORDING
    entry = (db.session.query(AuditLog)
             .filter(AuditLog.action == EXPORT_RECORDING).one())
    assert entry.target_id == ep
    meta = entry.to_dict()["metadata"]
    assert meta["segmentCount"] == 2
    assert set(meta["segmentIds"]) == {s[0] for s in seeded}
    assert meta["from"] and meta["to"] and meta["deviceName"] == "REC-EXPORT"


def test_export_missing_file_is_listed_not_fatal(client, app, super_admin_token, tmp_path):
    ep = _enroll(client, super_admin_token, "REC-EXPORT-MISS")
    seeded = _seed_segments(app, ep, tmp_path, count=2)
    # Simulate retention having removed the first file out from under the index.
    (tmp_path / ep / "x0.mp4.enc").unlink()

    resp = client.post("/api/recordings/segments/export",
                       json={"segmentIds": [s[0] for s in seeded]},
                       headers=auth_header(super_admin_token))
    assert resp.status_code == 200
    archive = _unzip(resp.get_data())
    import json
    manifest = json.loads(archive.read("manifest.json"))
    assert len(manifest["segments"]) == 1
    assert [m["segmentId"] for m in manifest["missing"]] == [seeded[0][0]]
    assert manifest["missing"][0]["reason"] == "file_missing"
    # No tz offset given -> UTC naming, still a valid archive.
    assert manifest["timezoneOffsetMinutes"] == 0


def test_export_validation_and_scope(client, app, super_admin_token, plain_admin_token,
                                     tmp_path):
    ep_a = _enroll(client, super_admin_token, "REC-EXP-A")
    ep_b = _enroll(client, super_admin_token, "REC-EXP-B")
    seeded_a = _seed_segments(app, ep_a, tmp_path, count=1)
    seeded_b = _seed_segments(app, ep_b, tmp_path, count=1)
    headers = auth_header(super_admin_token)
    url = "/api/recordings/segments/export"

    # Empty / malformed selections.
    assert client.post(url, json={"segmentIds": []}, headers=headers).status_code == 400
    assert client.post(url, json={"segmentIds": "abc"}, headers=headers).status_code == 400
    assert client.post(url, json={"segmentIds": [1, 2]}, headers=headers).status_code == 400
    assert client.post(url, json={"segmentIds": [seeded_a[0][0]], "tzOffsetMinutes": "8"},
                       headers=headers).status_code == 400
    # Over the per-request cap.
    from app.services.recording_export import MAX_SEGMENTS_PER_EXPORT
    too_many = [f"id-{i}" for i in range(MAX_SEGMENTS_PER_EXPORT + 1)]
    assert client.post(url, json={"segmentIds": too_many}, headers=headers).status_code == 400
    # Unknown id -> 404 (same as a single missing segment).
    assert client.post(url, json={"segmentIds": ["does-not-exist"]},
                       headers=headers).status_code == 404
    # Mixing endpoints in one archive is refused.
    assert client.post(url, json={"segmentIds": [seeded_a[0][0], seeded_b[0][0]]},
                       headers=headers).status_code == 400
    # A plain admin without scope over the endpoint sees not-found, not the file.
    assert client.post(url, json={"segmentIds": [seeded_a[0][0]]},
                       headers=auth_header(plain_admin_token)).status_code == 404
    # Nothing was audited as exported for the refused calls.
    from app.models.audit import EXPORT_RECORDING
    assert db.session.query(AuditLog).filter(AuditLog.action == EXPORT_RECORDING).count() == 0


def test_export_without_key_conflicts(client, app, super_admin_token, tmp_path):
    ep = _enroll(client, super_admin_token, "REC-EXP-NOKEY")
    seeded = _seed_segments(app, ep, tmp_path, count=1)
    app.config["RECORDING_KEY_PASSPHRASE"] = None
    resp = client.post("/api/recordings/segments/export",
                       json={"segmentIds": [seeded[0][0]]},
                       headers=auth_header(super_admin_token))
    assert resp.status_code == 409
