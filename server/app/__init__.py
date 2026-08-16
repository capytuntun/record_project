"""Application factory for the Enterprise Endpoint Management server."""

from __future__ import annotations

import logging
import uuid

from flask import Flask, g, jsonify, request

from .config import Config
from .errors import register_error_handlers
from .extensions import db, limiter, migrate, sock


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config or Config())

    _configure_logging(app)

    db.init_app(app)
    migrate.init_app(app, db)
    limiter.init_app(app)
    sock.init_app(app)

    # Importing the models registers every table on db.metadata.
    from . import models  # noqa: F401
    from .api import register_blueprints
    from .api.screen_ws import register_screen_ws

    register_blueprints(app)
    register_screen_ws()
    register_error_handlers(app)
    _register_request_hooks(app)
    _register_cli(app)

    # Screen-data retention (recordings + screenshots) runs in-process. Skip
    # under TESTING (tests drive the sweep directly) and when neither recordings
    # nor screenshots can be stored (no encryption key configured).
    if (
        (app.config.get("RECORDING_ENABLED") or app.config.get("SCREENSHOT_ENABLED"))
        and not app.config.get("TESTING")
    ):
        from .services.retention import start_sweeper

        start_sweeper(app)

    @app.get("/api/health")
    def health():
        """Liveness probe. Reveals nothing about internals (section 26)."""
        return jsonify({"status": "ok"})

    return app


def _configure_logging(app: Flask) -> None:
    level = getattr(logging, app.config.get("LOG_LEVEL", "INFO"), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app.logger.setLevel(level)


def _register_request_hooks(app: Flask) -> None:
    @app.before_request
    def _start_request():
        # Correlates a client-visible error with the server-side detail.
        g.request_id = str(uuid.uuid4())
        # Explicitly clear the principal. Flask reuses an already-pushed app
        # context (the test client does exactly this), and a stale g.current_user
        # would let one request be authorized as the previous caller.
        g.current_user = None
        g.current_endpoint = None
        g.current_credential = None

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")

        # The console needs to load its own stylesheet and script; the API
        # needs nothing at all. Neither policy permits inline code, so the
        # console keeps its CSS and JS in separate files rather than relying
        # on 'unsafe-inline'.
        if request.path.startswith("/api/"):
            console_policy = "default-src 'none'; frame-ancestors 'none'"
        else:
            # img-src includes blob: because live screen frames arrive over the
            # WebSocket as binary and are rendered from blob: object URLs. They
            # are transient client-side blobs (never fetched from a URL), so
            # blob: is the minimum needed and adds no external-fetch surface.
            # media-src blob: lets the recording playback <video> element play
            # decrypted segments fetched as blobs (same pattern as live frames).
            console_policy = (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data: blob:; "
                "media-src 'self' blob:; font-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
        response.headers.setdefault("Content-Security-Policy", console_policy)
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if getattr(g, "request_id", None):
            response.headers["X-Request-Id"] = g.request_id
        return response

    @app.teardown_request
    def _rollback_on_error(exc):
        # An uncommitted transaction must not leak into the next request on a
        # pooled connection.
        if exc is not None:
            db.session.rollback()


def _register_cli(app: Flask) -> None:
    from .cli import register_commands

    register_commands(app)
