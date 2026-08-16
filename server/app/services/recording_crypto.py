"""AES-GCM encryption for recording segments at rest (spec section 24).

Segments are small (a few MB of H.264), so each file is encrypted whole:
``nonce(12) || ciphertext+tag``. The key is derived once from the configured
passphrase; that passphrase is an environment secret and is expected to be
high-entropy, so a single SHA-256 is an adequate derivation here.
"""

from __future__ import annotations

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


def derive_key(passphrase: str) -> bytes:
    """32-byte AES-256 key from the passphrase."""
    return hashlib.sha256(passphrase.encode("utf-8")).digest()


def encrypt_bytes(key: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def decrypt_bytes(key: bytes, blob: bytes) -> bytes:
    return AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], None)


def encrypt_file(key: bytes, src_path: str, dst_path: str) -> int:
    """Encrypt src -> dst. Returns the ciphertext size."""
    with open(src_path, "rb") as handle:
        data = handle.read()
    blob = encrypt_bytes(key, data)
    tmp = dst_path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(blob)
    os.replace(tmp, dst_path)
    return len(blob)


def decrypt_file(key: bytes, src_path: str) -> bytes:
    with open(src_path, "rb") as handle:
        return decrypt_bytes(key, handle.read())
