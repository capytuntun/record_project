"""Argon2id password hashing and password policy (spec section 5)."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..extensions import password_hasher

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

# Verifying against this costs the same as a real verification. Used on unknown
# usernames so login latency does not reveal whether an account exists.
_DUMMY_HASH = password_hasher.hash("dummy-password-for-constant-time-login-path")


class PasswordPolicyError(ValueError):
    """Raised when a proposed password does not meet policy."""


def validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise PasswordPolicyError("Password must be a string.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
        )
    classes = (
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    )
    if sum(classes) < 3:
        raise PasswordPolicyError(
            "Password must contain at least three of: lowercase, uppercase, digit, symbol."
        )


def hash_password(password: str) -> str:
    validate_password(password)
    return password_hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Verify a password, taking the same work regardless of account existence."""
    candidate = stored_hash or _DUMMY_HASH
    try:
        password_hasher.verify(candidate, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    # A real hash was supplied and matched.
    return stored_hash is not None


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than current policy."""
    try:
        return password_hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def generate_secret(nbytes: int = 32) -> str:
    """A URL-safe random secret for enrollment tokens and device credentials."""
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    """Lookup hash for high-entropy secrets.

    Argon2 is for user-chosen passwords. Enrollment tokens and device
    credentials are 256-bit random values, so they are not brute-forceable and
    need a fast, deterministic hash the database can index on.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
