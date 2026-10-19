"""Password hashing and session-token helpers built only on the standard library.

Passwords use scrypt (memory-hard) with a per-user random salt; the stored form
is ``scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>`` so parameters travel with the
hash and can be tuned later without invalidating old records. Session tokens are
opaque random strings; only their SHA-256 hash is persisted, so a database leak
never exposes a usable token.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

# scrypt cost parameters. Conservative defaults that stay well under the default
# 32 MiB scrypt maxmem ceiling (128 * N * r = 16 MiB here).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

MIN_PASSWORD_LENGTH = 12
# scrypt work grows with input length; this caps it without limiting real passphrases.
MAX_PASSWORD_LENGTH = 1024
_KNOWN_PLACEHOLDERS = frozenset({
    "change-this-placeholder-before-first-login", "change-me-please", "changeme123456",
})


def password_policy_error(password: str) -> str:
    """Return why a new password is unacceptable, or an empty string."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    if password.strip().lower() in _KNOWN_PLACEHOLDERS:
        return "That password is a published placeholder; choose your own."
    return ""


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Password must not be empty.")
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n_raw, r_raw, p_raw, salt_hex, hash_hex = stored.split("$")
    except (ValueError, AttributeError):
        return False
    if scheme != "scrypt":
        return False
    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_raw), r=int(r_raw), p=int(p_raw),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), hash_hex)


def generate_session_token() -> str:
    """A fresh opaque token to hand to the client (never stored in plaintext)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Stable hash of a session token for storage and constant-time lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
