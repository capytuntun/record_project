"""Screen-data storage facade (multi-target).

Recorder, screenshots API, playback and retention all go through here rather
than touching disk directly. The deployment defines many named storage targets
(FTP/SMB NAS); a recording policy picks one by id, and screenshots use the
default target. ``None``/``LOCAL`` means the built-in local disk.

Reliability: files are always *produced* locally (FFmpeg needs a local file;
screenshots are encrypted in memory). ``store_file`` then publishes the
ciphertext to the chosen target; if a remote target is unreachable it falls back
to local disk and reports that. Each file records *where it actually landed* --
either the target id, or ``LOCAL`` -- so reads and deletes go to the right place.
"""

from __future__ import annotations

import logging

from .base import StorageBackend, StorageError  # noqa: F401 (re-exported)
from .ftp import FtpBackend
from .local import LocalBackend
from .smb import SmbBackend

logger = logging.getLogger("eem.storage")

LOCAL = "LOCAL"


def _local(app) -> LocalBackend:
    return LocalBackend(app.config["RECORDING_DIR"], app.config["SCREENSHOT_DIR"])


def _read_target(app, target_id: str):
    """Fetch a target's fields as a plain dict (its own app context)."""
    from ...models import db
    from ...models.storage import StorageTarget

    with app.app_context():
        row = db.session.get(StorageTarget, target_id)
        if row is None:
            return None
        return {
            "backend": row.backend, "host": row.host, "port": row.port,
            "share": row.share, "base_path": row.base_path,
            "username": row.username, "domain": row.domain,
            "use_tls": row.use_tls, "secret_sealed": row.secret_sealed,
        }


def default_target_id(app) -> str | None:
    """Id of the target flagged default (used by screenshots), or None = local."""
    from ...models import db
    from ...models.storage import StorageTarget

    with app.app_context():
        row = (
            db.session.query(StorageTarget)
            .filter(StorageTarget.is_default.is_(True))
            .first()
        )
        return row.id if row else None


def build_backend(values: dict, password: str | None) -> StorageBackend:
    """Construct a remote backend from a target's values + plaintext password."""
    from ...models.storage import BACKEND_FTP, BACKEND_SMB

    backend = (values or {}).get("backend")
    if backend == BACKEND_FTP:
        return FtpBackend(
            host=values.get("host") or "", port=values.get("port"),
            username=values.get("username") or "", password=password or "",
            base_path=values.get("base_path") or "",
            use_tls=bool(values.get("use_tls", True)),
        )
    if backend == BACKEND_SMB:
        return SmbBackend(
            server=values.get("host") or "", port=values.get("port"),
            share=values.get("share") or "", username=values.get("username") or "",
            password=password or "", domain=values.get("domain") or "",
            base_path=values.get("base_path") or "",
        )
    raise StorageError("未知的儲存目標型別。")


def backend_for_target(app, target_id: str | None) -> StorageBackend:
    """The backend for a target id; None/LOCAL -> local disk (never raises here)."""
    if not target_id or target_id == LOCAL:
        return _local(app)
    values = _read_target(app, target_id)
    if values is None:
        raise StorageError("儲存目標不存在或已刪除。")
    password = None
    if values.get("secret_sealed"):
        from .secrets import unseal
        password = unseal(app.config["SECRET_KEY"], values["secret_sealed"])
    return build_backend(values, password)


def store_file(app, kind: str, filename: str, data: bytes,
               target_id: str | None = None) -> tuple[str, int]:
    """Publish ciphertext to the chosen target.

    Returns (location, size) where location is the target id, or 'LOCAL'. On
    remote failure, falls back to local so a NAS outage never loses a file.
    """
    if not target_id or target_id == LOCAL:
        return LOCAL, _local(app).put(kind, filename, data)
    try:
        backend = backend_for_target(app, target_id)
        size = backend.put(kind, filename, data)
        return target_id, size
    except Exception:  # noqa: BLE001
        logger.exception("remote store failed (target %s); keeping %s locally",
                         target_id, filename)
        return LOCAL, _local(app).put(kind, filename, data)


def load_file(app, kind: str, filename: str, location: str | None) -> bytes:
    """Read a file back from where it was stored (a target id, or LOCAL)."""
    if not location or location == LOCAL:
        return _local(app).get(kind, filename)
    return backend_for_target(app, location).get(kind, filename)


def remove_file(app, kind: str, filename: str, location: str | None) -> None:
    """Delete a file from where it was stored (best effort)."""
    try:
        if not location or location == LOCAL:
            _local(app).remove(kind, filename)
        else:
            backend_for_target(app, location).remove(kind, filename)
    except Exception:  # noqa: BLE001
        logger.warning("could not remove %s/%s from %s", kind, filename, location)
