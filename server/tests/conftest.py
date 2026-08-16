"""Shared fixtures. Every test runs against a fresh in-memory database."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("EEM_SECRET_KEY", "test-secret-key-not-for-production-use-only")

from app import create_app  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.models import User, db  # noqa: E402
from app.models.user import ROLE_ADMIN, ROLE_SUPER_ADMIN  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402

SUPER_ADMIN_PASSWORD = "Sup3r-Admin-Passw0rd!"
ADMIN_PASSWORD = "Plain-Admin-Passw0rd!"


@pytest.fixture
def app():
    application = create_app(TestConfig())
    with application.app_context():
        db.create_all()
        # The screen hub and ticket store are process-global; clear them so one
        # test's connections never leak into the next.
        from app.services.screen_hub import hub
        from app.services.screen_tickets import tickets
        hub.reset()
        tickets.reset()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _make_user(username: str, password: str, role: str) -> User:
    user = User(username=username, password_hash=hash_password(password), role=role)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def super_admin(app):
    return _make_user("root.admin", SUPER_ADMIN_PASSWORD, ROLE_SUPER_ADMIN)


@pytest.fixture
def plain_admin(app):
    return _make_user("plain.admin", ADMIN_PASSWORD, ROLE_ADMIN)


def login(client, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def super_admin_token(client, super_admin):
    return login(client, super_admin.username, SUPER_ADMIN_PASSWORD)["accessToken"]


@pytest.fixture
def plain_admin_token(client, plain_admin):
    return login(client, plain_admin.username, ADMIN_PASSWORD)["accessToken"]
