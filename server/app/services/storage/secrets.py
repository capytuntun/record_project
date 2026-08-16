"""Sealing for storage-target credentials (the NAS password).

The password for an FTP/SMB target is a secret (section 24), so it is never
stored in plaintext. It is AES-GCM sealed with a key derived from the server's
SECRET_KEY -- independent of the screen-data key -- and kept as base64 text.
"""

from __future__ import annotations

import base64
import hashlib

from ..recording_crypto import decrypt_bytes, encrypt_bytes


def _key(secret_key: str) -> bytes:
    return hashlib.sha256(("storage-cred|" + secret_key).encode("utf-8")).digest()


def seal(secret_key: str, plaintext: str) -> str:
    blob = encrypt_bytes(_key(secret_key), plaintext.encode("utf-8"))
    return base64.b64encode(blob).decode("ascii")


def unseal(secret_key: str, sealed: str) -> str:
    blob = base64.b64decode(sealed.encode("ascii"))
    return decrypt_bytes(_key(secret_key), blob).decode("utf-8")
