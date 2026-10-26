"""The package form must never default to localhost (spec section 18).

The installer's finish page opens the console as https://localhost. A package
built from that page used to carry https://localhost as the management server
URL, so every endpoint tried to call itself. The server now works out the
address endpoints can actually reach and the console pre-fills that.
"""

from __future__ import annotations

from app.api.packages import _resolve_server_url
from app.services import packaging


def test_explicit_public_url_wins(app, monkeypatch):
    monkeypatch.setattr(packaging, "_primary_ipv4", lambda: "192.168.1.20")
    app.config["PUBLIC_URL"] = "https://eem.example.com"
    with app.test_request_context("/", base_url="https://localhost"):
        assert packaging.suggested_server_url() == "https://eem.example.com"


def test_lan_address_keeps_the_scheme_and_port_the_console_used(app, monkeypatch):
    monkeypatch.setattr(packaging, "_primary_ipv4", lambda: "192.168.1.20")
    app.config["PUBLIC_URL"] = None
    with app.test_request_context("/", base_url="https://localhost:8443"):
        assert packaging.suggested_server_url() == "https://192.168.1.20:8443"
    with app.test_request_context("/", base_url="https://localhost"):
        assert packaging.suggested_server_url() == "https://192.168.1.20"


def test_no_address_means_no_suggestion(app, monkeypatch):
    monkeypatch.setattr(packaging, "_primary_ipv4", lambda: None)
    app.config["PUBLIC_URL"] = None
    with app.test_request_context("/", base_url="https://localhost"):
        assert packaging.suggested_server_url() is None


def test_toolchain_tells_the_console_the_address(client, super_admin_token, app, monkeypatch):
    monkeypatch.setattr(packaging, "_primary_ipv4", lambda: "192.168.1.20")
    app.config["PUBLIC_URL"] = None
    response = client.get(
        "/api/packages/toolchain",
        headers={"Authorization": f"Bearer {super_admin_token}"},
        base_url="https://localhost",
    )
    assert response.status_code == 200
    assert response.get_json()["serverUrl"] == "https://192.168.1.20"


def test_default_server_url_is_never_localhost(app, monkeypatch):
    monkeypatch.setattr(packaging, "_primary_ipv4", lambda: "192.168.1.20")
    app.config["PUBLIC_URL"] = None
    with app.test_request_context("/api/packages", base_url="https://localhost"):
        assert _resolve_server_url({}) == "https://192.168.1.20"


def test_a_supplied_server_url_is_left_alone(app, monkeypatch):
    monkeypatch.setattr(packaging, "_primary_ipv4", lambda: "192.168.1.20")
    with app.test_request_context("/api/packages", base_url="https://localhost"):
        assert _resolve_server_url({"serverUrl": "https://eem.corp.local"}) == "https://eem.corp.local"


def test_a_non_loopback_request_keeps_its_own_address(app, monkeypatch):
    monkeypatch.setattr(packaging, "_primary_ipv4", lambda: "192.168.1.20")
    app.config["PUBLIC_URL"] = None
    with app.test_request_context("/api/packages", base_url="https://eem.corp.local"):
        assert _resolve_server_url({}) == "https://eem.corp.local"


def test_primary_ipv4_is_never_loopback():
    address = packaging._primary_ipv4()
    assert address is None or not address.startswith("127.")
