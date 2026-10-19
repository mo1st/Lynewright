from __future__ import annotations

import json
import sqlite3

import pytest

from app.services import secret_store
from app.services.secret_store import decrypt_secret, encrypt_secret, migrate_legacy_secrets

_KEY = "k" * 48


def _legacy_token(plaintext: str) -> str:
    fernet = secret_store._fernet_for(secret_store._LEGACY_DEVELOPMENT_MATERIAL.encode("utf-8"))
    return secret_store._PREFIX + fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def test_short_secret_key_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError, match="at least 32"):
        encrypt_secret("value")


def test_production_without_key_is_refused(monkeypatch) -> None:
    monkeypatch.delenv("LYNEWRIGHT_SECRET_KEY", raising=False)
    monkeypatch.setenv("LYNEWRIGHT_ENV", "production")
    monkeypatch.setenv("LYNEWRIGHT_CORS_ORIGINS", "http://127.0.0.1:8080")
    with pytest.raises(RuntimeError, match="required in production"):
        secret_store.validate_secret_key_configuration()


def test_configured_key_does_not_open_legacy_tokens_without_migration(monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_SECRET_KEY", _KEY)
    with pytest.raises(RuntimeError):
        decrypt_secret(_legacy_token("api-key"))


def test_migration_reseals_plain_and_json_embedded_legacy_tokens(monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_SECRET_KEY", _KEY)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE credentials (name TEXT, encrypted_secret TEXT)")
    db.execute('CREATE TABLE "odd ""links" (config_json TEXT PRIMARY KEY) WITHOUT ROWID')
    legacy = _legacy_token("provider-key")
    current = encrypt_secret("already-new")
    db.execute("INSERT INTO credentials VALUES (?, ?)", ("legacy", legacy))
    db.execute("INSERT INTO credentials VALUES (?, ?)", ("current", current))
    db.execute('INSERT INTO "odd ""links" VALUES (?)', (json.dumps({"bot_token": _legacy_token("bot-secret"), "chat": 7}),))

    assert migrate_legacy_secrets(db) == 2

    rows = dict(db.execute("SELECT name, encrypted_secret FROM credentials").fetchall())
    assert rows["current"] == current
    assert rows["legacy"] != legacy
    assert decrypt_secret(rows["legacy"]) == "provider-key"
    embedded = json.loads(db.execute('SELECT config_json FROM "odd ""links"').fetchone()[0])
    assert decrypt_secret(embedded["bot_token"]) == "bot-secret"
    assert embedded["chat"] == 7
    # A second pass finds nothing left to migrate.
    assert migrate_legacy_secrets(db) == 0


def test_migration_is_a_no_op_without_a_configured_key(monkeypatch) -> None:
    monkeypatch.delenv("LYNEWRIGHT_SECRET_KEY", raising=False)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE credentials (encrypted_secret TEXT)")
    legacy = _legacy_token("provider-key")
    db.execute("INSERT INTO credentials VALUES (?)", (legacy,))
    assert migrate_legacy_secrets(db) == 0
    assert db.execute("SELECT encrypted_secret FROM credentials").fetchone()[0] == legacy
