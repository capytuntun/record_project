"""Serves the management console (spec section 20).

The console is a static page that talks to the same REST API as any other
client. It renders and hides controls based on the permission list the server
returns at login, but that is presentation only -- every call it makes is
re-authorized server-side (section 25).
"""

from __future__ import annotations

from flask import Blueprint, current_app, send_from_directory

bp = Blueprint("web", __name__)


@bp.get("/")
def console():
    return send_from_directory(current_app.static_folder, "index.html")


@bp.get("/favicon.ico")
def favicon():
    # Browsers request this unprompted; answer without a 404 in the log.
    return ("", 204)
