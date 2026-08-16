"""Endpoint asset inventory: the agent reports it on the heartbeat, the console
reads it back, and the server sanitises whatever the agent sends."""

from __future__ import annotations

from .conftest import auth_header
from .test_endpoints import create_enrollment_token, enroll


def _enroll(client, super_admin_token):
    created = create_enrollment_token(client, super_admin_token)
    return enroll(client, created["token"]).get_json()


def _sample_inventory():
    return {
        "osBuild": "22631.4169",
        "cpu": "Intel Core i7-1265U",
        "cpuCores": 12,
        "memoryTotalMb": 16384,
        "memoryFreeMb": 8192,
        "diskTotalGb": 476,
        "diskFreeGb": 200,
        "diskFreePercent": 42,
        "uptimeSeconds": 90000,
        "softwareCount": 2,
        "software": [
            {"name": "Google Chrome", "version": "120.0", "publisher": "Google LLC"},
            {"name": "7-Zip", "version": "23.01", "publisher": "Igor Pavlov"},
        ],
    }


def test_heartbeat_stores_inventory_and_detail_returns_it(client, super_admin_token):
    enrolled = _enroll(client, super_admin_token)

    r = client.post(
        "/api/agent/heartbeat",
        json={"agentVersion": "0.2.0", "inventory": _sample_inventory()},
        headers=auth_header(enrolled["deviceCredential"]),
    )
    assert r.status_code == 200

    detail = client.get(
        f"/api/endpoints/{enrolled['endpointId']}", headers=auth_header(super_admin_token)
    ).get_json()
    inv = detail["inventory"]
    assert inv is not None
    assert inv["cpu"] == "Intel Core i7-1265U"
    assert inv["cpuCores"] == 12
    assert inv["diskFreePercent"] == 42
    assert inv["softwareCount"] == 2
    assert {s["name"] for s in inv["software"]} == {"Google Chrome", "7-Zip"}
    assert inv["collectedAt"]


def test_detail_inventory_is_null_before_any_report(client, super_admin_token):
    enrolled = _enroll(client, super_admin_token)
    client.post("/api/agent/heartbeat", json={}, headers=auth_header(enrolled["deviceCredential"]))

    detail = client.get(
        f"/api/endpoints/{enrolled['endpointId']}", headers=auth_header(super_admin_token)
    ).get_json()
    assert detail["inventory"] is None


def test_inventory_is_re_upserted_not_duplicated(client, super_admin_token):
    enrolled = _enroll(client, super_admin_token)
    hdr = auth_header(enrolled["deviceCredential"])

    client.post("/api/agent/heartbeat", json={"inventory": _sample_inventory()}, headers=hdr)
    second = _sample_inventory()
    second["diskFreePercent"] = 7
    second["software"] = [{"name": "Notepad++", "version": "8.6", "publisher": "Don Ho"}]
    client.post("/api/agent/heartbeat", json={"inventory": second}, headers=hdr)

    detail = client.get(
        f"/api/endpoints/{enrolled['endpointId']}", headers=auth_header(super_admin_token)
    ).get_json()
    inv = detail["inventory"]
    assert inv["diskFreePercent"] == 7
    assert [s["name"] for s in inv["software"]] == ["Notepad++"]


def test_server_sanitises_untrusted_inventory(client, super_admin_token):
    enrolled = _enroll(client, super_admin_token)
    hostile = {
        "diskFreePercent": 9999,           # clamped to 0..100
        "cpuCores": -5,                    # clamped to >= 0
        # Bad entries first so the 2000-cap does not slice them off before the
        # name check gets to reject them.
        "software": (
            ["not-a-dict", {"noname": True}]                                   # skipped
            + [{"name": "App " + str(i), "version": "1"} for i in range(5000)]  # capped
        ),
    }
    client.post(
        "/api/agent/heartbeat",
        json={"inventory": hostile},
        headers=auth_header(enrolled["deviceCredential"]),
    )

    detail = client.get(
        f"/api/endpoints/{enrolled['endpointId']}", headers=auth_header(super_admin_token)
    ).get_json()
    inv = detail["inventory"]
    assert inv["diskFreePercent"] == 100
    assert inv["cpuCores"] == 0
    assert len(inv["software"]) <= 2000
    # The non-dict and the name-less entry must not appear as real rows.
    assert all(s.get("name") for s in inv["software"])
