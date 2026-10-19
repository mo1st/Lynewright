from __future__ import annotations

import base64
import hashlib
import os
import re
import sqlite3
from contextlib import closing

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_PREFIX = "enc:v1:"
# The development fallback is printed in the public source, so it protects
# nothing. It exists only for explicit development runs and for migrating
# databases that were written before installs generated their own key.
_LEGACY_DEVELOPMENT_MATERIAL = "lynewright-development-only-secret"
_MIN_KEY_LENGTH = 32
_TOKEN = re.compile(re.escape(_PREFIX) + r"([A-Za-z0-9_\-=]+)")


def _configured_material() -> str:
    raw = os.getenv("LYNEWRIGHT_SECRET_KEY", "").strip()
    if raw and len(raw) < _MIN_KEY_LENGTH:
        raise RuntimeError(f"LYNEWRIGHT_SECRET_KEY must be at least {_MIN_KEY_LENGTH} characters.")
    return raw


def _master_material() -> bytes:
    raw = _configured_material()
    if not raw:
        settings = get_settings()
        if settings.is_production:
            raise RuntimeError("LYNEWRIGHT_SECRET_KEY is required in production.")
        raw = _LEGACY_DEVELOPMENT_MATERIAL
    return raw.encode("utf-8")


def _fernet_for(material: bytes) -> Fernet:
    digest = hashlib.sha256(material).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _fernet() -> Fernet:
    return _fernet_for(_master_material())


def validate_secret_key_configuration() -> None:
    """Fail at startup, not at the first credential read."""
    _master_material()


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


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def migrate_legacy_secrets(db: sqlite3.Connection | None = None) -> int:
    """Re-encrypt every value still sealed with the public development key.

    Secrets live in several tables, some inside JSON documents, so this scans
    every text cell for encrypted tokens instead of naming columns. A token that
    already opens with the configured key is left alone, which keeps the pass
    idempotent. Returns the number of tokens re-encrypted.
    """
    raw = _configured_material()
    if not raw:
        return 0
    current = _fernet_for(raw.encode("utf-8"))
    legacy = _fernet_for(_LEGACY_DEVELOPMENT_MATERIAL.encode("utf-8"))
    migrated = 0

    def reseal(match: re.Match[str]) -> str:
        nonlocal migrated
        token = match.group(1).encode("ascii")
        try:
            current.decrypt(token)
            return match.group(0)
        except InvalidToken:
            pass
        try:
            plaintext = legacy.decrypt(token)
        except InvalidToken:
            return match.group(0)
        migrated += 1
        return _PREFIX + current.encrypt(plaintext).decode("ascii")

    def run(connection: sqlite3.Connection) -> None:
        tables = [
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quoted(table)})").fetchall()]
            for column in columns:
                name = _quoted(column)
                values = connection.execute(
                    f"SELECT DISTINCT {name} FROM {_quoted(table)} "
                    f"WHERE typeof({name})='text' AND instr({name}, ?) > 0",
                    (_PREFIX,),
                ).fetchall()
                for (value,) in values:
                    updated = _TOKEN.sub(reseal, str(value))
                    if updated != value:
                        connection.execute(
                            f"UPDATE {_quoted(table)} SET {name}=? WHERE {name}=?",
                            (updated, value),
                        )

    if db is not None:
        run(db)
        return migrated
    from app.database import get_connection

    with closing(get_connection()) as connection, connection:
        run(connection)
    return migrated
