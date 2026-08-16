"""Retention sweep for recording segments (spec section 23).

Deletes segment files and their index rows once past ``expires_at``. Runs on a
periodic background thread and is also exposed as a CLI command for cron.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("eem.recording")


def sweep_expired(app) -> int:
    """Delete recordings past their retention. Returns how many were removed."""
    from ..models import RecordingSegment, db, utcnow

    from .storage import remove_file

    removed = 0
    with app.app_context():
        now = utcnow()
        expired = (
            db.session.query(RecordingSegment)
            .filter(RecordingSegment.expires_at.isnot(None), RecordingSegment.expires_at < now)
            .limit(1000)
            .all()
        )
        for segment in expired:
            remove_file(app, "recordings", segment.filename, segment.storage_backend)
            db.session.delete(segment)
            removed += 1
        if removed:
            db.session.commit()
            logger.info("retention: removed %s expired recording segments", removed)
        db.session.remove()
    return removed


def sweep_expired_screenshots(app) -> int:
    """Delete screenshots past their retention. Returns how many were removed."""
    from ..models import Screenshot, db, utcnow

    from .storage import remove_file

    removed = 0
    with app.app_context():
        now = utcnow()
        expired = (
            db.session.query(Screenshot)
            .filter(Screenshot.expires_at.isnot(None), Screenshot.expires_at < now)
            .limit(1000)
            .all()
        )
        for shot in expired:
            remove_file(app, "screenshots", shot.filename, shot.storage_backend)
            db.session.delete(shot)
            removed += 1
        if removed:
            db.session.commit()
            logger.info("retention: removed %s expired screenshots", removed)
        db.session.remove()
    return removed


def start_sweeper(app, interval_seconds: int = 3600) -> None:
    """Start the periodic retention thread (once per process)."""

    def loop() -> None:
        # A short initial delay so startup is not competing with the first sweep.
        time.sleep(60)
        while True:
            try:
                sweep_expired(app)
                sweep_expired_screenshots(app)
            except Exception:
                logger.exception("retention sweep failed")
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, name="recording-retention", daemon=True)
    thread.start()
    logger.info("recording retention sweeper started (every %ss)", interval_seconds)
