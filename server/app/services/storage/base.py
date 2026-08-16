"""Storage backend interface.

A backend stores already-encrypted screen-data files (recording segments and
screenshots) under two logical kinds: ``recordings`` and ``screenshots``. Files
are addressed by a relative ``filename`` (the same value indexed in the DB), so
the backend only decides *where* those bytes physically live -- local disk, or a
NAS reached over FTP/SMB. The bytes are ciphertext either way; a backend never
sees plaintext (section 14, 24).
"""

from __future__ import annotations

KINDS = ("recordings", "screenshots")


class StorageError(Exception):
    """A storage operation failed (connect/auth/IO). Message is user-facing."""


class StorageBackend:
    type = "LOCAL"
    is_local = False

    def put(self, kind: str, filename: str, data: bytes) -> int:
        """Store ``data`` and return the number of bytes written."""
        raise NotImplementedError

    def get(self, kind: str, filename: str) -> bytes:
        raise NotImplementedError

    def remove(self, kind: str, filename: str) -> None:
        raise NotImplementedError

    def test(self) -> tuple[bool, str]:
        """Verify connectivity + write access. Returns (ok, message)."""
        raise NotImplementedError


def posix_join(*parts: str) -> str:
    """Join non-empty path parts with a single forward slash, no leading slash."""
    clean = [p.strip("/\\") for p in parts if p and p.strip("/\\")]
    return "/".join(clean)


# A short, harmless file used by test() to prove write+read+delete works.
PROBE_NAME = ".eem-write-test"
