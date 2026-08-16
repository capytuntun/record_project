"""REST API blueprints (spec sections 4 and 28)."""

from flask import Flask

from .. import web
from . import (
    agent,
    alerts,
    audit_logs,
    auth,
    endpoints,
    enrollment,
    groups,
    packages,
    recordings,
    screen,
    screenshots,
    storage,
    users,
)

BLUEPRINTS = (
    auth.bp,
    users.bp,
    groups.bp,
    endpoints.bp,
    enrollment.bp,
    audit_logs.bp,
    packages.bp,
    recordings.bp,
    screen.bp,
    screenshots.bp,
    storage.bp,
    alerts.bp,
    agent.bp,
    web.bp,
)


def register_blueprints(app: Flask) -> None:
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)


__all__ = ["register_blueprints", "BLUEPRINTS"]
