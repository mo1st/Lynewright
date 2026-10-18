from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_PREFIX = "enc:v1:"


def _master_material() -> bytes:
    raw = os.getenv("LYNEWRIGHT_SECRET_KEY", "").strip()
    if not raw:
        settings = get_settings()
        if settings.is_production:
            raise RuntimeError("LYNEWRIGHT_SECRET_KEY is required in production.")
        raw = "lynewright-development-only-secret"
    return raw.encode("utf-8")


def _fernet() -> Fernet:
    digest = hashlib.sha256(_master_material()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if text.startswith(_PREFIX):
        return text
    return _PREFIX + _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if not text.startswith(_PREFIX):
        # Backward compatibility for phase-1 development databases.
        return text
    try:
        return _fernet().decrypt(text[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Stored secret could not be decrypted with LYNEWRIGHT_SECRET_KEY.") from exc


def mask_secret(value: str) -> str:
    text = decrypt_secret(value) if value else ""
    if not text:
        return ""
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:4]}…{text[-4:]}"
