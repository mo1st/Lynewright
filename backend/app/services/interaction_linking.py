"""Interaction channel connections and secure account linking.

A *connection* is a tenant-scoped channel endpoint (for Telegram: one bot,
with its token stored encrypted and a per-connection webhook secret). A *link*
binds a verified external channel identity (Telegram user id) to a Lynewright
tenant/project/user. An external identity is never trusted on its own —
authorization flows exclusively through one-time, short-lived, hashed link
tokens created by an authenticated Lynewright user.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import time
from contextlib import closing
from collections.abc import Callable
from typing import Any

from app.config import get_settings
from app.database import get_connection, init_db
from app.passwords import hash_token
from app.services.interaction_channels import ChannelCapability, default_registry
from app.services.interaction_schema import init_interaction_schema
from app.services.secret_store import decrypt_secret, encrypt_secret
from app.services.tenancy import require_project_in_tenant


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


class LinkError(ValueError):
    """A linking request is invalid, expired, or conflicts with an existing bind."""


class ConnectionValidationError(ValueError):
    """A connection operation is invalid (name kept distinct from builtin)."""


# ---------------------------------------------------------------- connections

_SENSITIVE_CREDENTIAL_KEYS = {"bot_token"}


def _serialize_connection(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    item.pop("credentials_json", None)
    item.pop("webhook_secret_hash", None)
    if item.get("credentials"):
        item.pop("credentials", None)
    return item


def create_connection(
    *,
    tenant_id: str,
    project_id: str,
    channel_type: str,
    name: str,
    credentials: dict[str, Any] | None = None,
    operator_profile_id: str = "",
) -> dict[str, Any]:
    """Create a channel connection. Returns the row plus the raw webhook secret.

    The webhook secret is shown exactly once; only its SHA-256 hash persists.
    """

    init_db()
    registry = default_registry()
    if not registry.has(channel_type):
        raise ValueError(f"Unsupported interaction channel: {channel_type}")
    capabilities = registry.capabilities_for(channel_type)
    if not capabilities or ChannelCapability.MESSAGE_RECEIVE not in capabilities:
        raise ValueError("Channel cannot receive messages.")
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Connection name is required.")
    require_project_in_tenant(tenant_id=tenant_id, project_id=project_id)
    selected_profile_id = _validate_operator_profile_selection(
        tenant_id=tenant_id,
        project_id=project_id,
        operator_profile_id=operator_profile_id,
    )
    connection_id = _new_id("ic")
    webhook_secret = f"whsec_{secrets.token_urlsafe(24)}"
    encrypted_credentials = {
        key: (encrypt_secret(str(value)) if key in _SENSITIVE_CREDENTIAL_KEYS else value)
        for key, value in (credentials or {}).items()
    }
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        try:
            db.execute(
                """INSERT INTO interaction_connections
                   (id,tenant_id,project_id,channel_type,name,credentials_json,webhook_secret_hash,operator_profile_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    connection_id, tenant_id, project_id, channel_type, clean_name[:120],
                    json.dumps(encrypted_credentials), hash_token(webhook_secret), selected_profile_id,
                ),
            )
            row = db.execute(
                "SELECT * FROM interaction_connections WHERE id=? AND tenant_id=?",
                (connection_id, tenant_id),
            ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ConnectionValidationError("Could not create the connection.") from exc
    result = _serialize_connection(row)
    result["webhook_secret"] = webhook_secret
    return result


def _validate_operator_profile_selection(
    *, tenant_id: str, project_id: str, operator_profile_id: str | None,
) -> str:
    """Return a same-tenant, same-project profile id or the automatic value."""

    selected = str(operator_profile_id or "").strip()
    if not selected:
        return ""
    with closing(get_connection()) as db:
        row = db.execute(
            "SELECT project_id FROM operator_profiles WHERE id=? AND tenant_id=?",
            (selected, tenant_id),
        ).fetchone()
    if row is None:
        raise ValueError("Selected Operator profile was not found in this workspace.")
    profile_project_id = str(row["project_id"] or "")
    if profile_project_id and profile_project_id != project_id:
        raise ValueError("Selected Operator profile belongs to a different project.")
    return selected


def _load_connection(db: sqlite3.Connection, *, tenant_id: str, connection_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM interaction_connections WHERE id=? AND tenant_id=?",
        (connection_id, tenant_id),
    ).fetchone()
    if row is None:
        raise LookupError("Interaction connection not found.")
    return row


def get_connection_row(*, tenant_id: str, connection_id: str) -> dict[str, Any] | None:
    """Tenant-scoped connection lookup returning the full row internally."""

    init_db()
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT * FROM interaction_connections WHERE id=? AND tenant_id=?",
            (connection_id, tenant_id),
        ).fetchone()
    return dict(row) if row else None


def load_connection_any_tenant(connection_id: str) -> dict[str, Any]:
    """Internal loader for webhook routing (the secret check authorizes it)."""

    init_db()
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT * FROM interaction_connections WHERE id=?", (connection_id,)
        ).fetchone()
    if row is None:
        raise LookupError("Interaction connection not found.")
    return dict(row)


def require_tenant_connection(*, tenant_id: str, connection_id: str) -> dict[str, Any]:
    row = get_connection_row(tenant_id=tenant_id, connection_id=connection_id)
    if row is None:
        raise LookupError("Interaction connection not found.")
    return row


def list_connections(
    *, tenant_id: str, project_id: str | None = None
) -> list[dict[str, Any]]:
    init_db()
    query = "SELECT * FROM interaction_connections WHERE tenant_id=?"
    params: list[Any] = [tenant_id]
    if project_id:
        require_project_in_tenant(tenant_id=tenant_id, project_id=project_id)
        query += " AND project_id=?"
        params.append(project_id)
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(query + " ORDER BY created_at DESC, id DESC", params).fetchall()
    return [_serialize_connection(row) for row in rows]


def update_connection(
    *,
    tenant_id: str,
    connection_id: str,
    name: str | None = None,
    enabled: bool | None = None,
    bot_token: str | None = None,
    operator_profile_id: str | None = None,
) -> dict[str, Any]:
    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        connection = _load_connection(db, tenant_id=tenant_id, connection_id=connection_id)
        updates: list[str] = []
        params: list[Any] = []
        if name is not None:
            clean = name.strip()
            if not clean:
                raise ValueError("Connection name cannot be empty.")
            updates.append("name=?")
            params.append(clean[:120])
        if enabled is not None:
            updates.append("enabled=?")
            params.append(1 if enabled else 0)
        if bot_token is not None:
            if not bot_token.strip():
                raise ValueError("Bot token cannot be empty.")
            encrypted = json.loads(
                db.execute(
                    "SELECT credentials_json FROM interaction_connections WHERE id=?",
                    (connection_id,),
                ).fetchone()["credentials_json"]
                or "{}"
            )
            encrypted["bot_token"] = encrypt_secret(bot_token.strip())
            updates.append("credentials_json=?")
            params.append(json.dumps(encrypted))
        if operator_profile_id is not None:
            selected_profile_id = _validate_operator_profile_selection(
                tenant_id=tenant_id,
                project_id=str(connection["project_id"]),
                operator_profile_id=operator_profile_id,
            )
            updates.append("operator_profile_id=?")
            params.append(selected_profile_id)
        if not updates:
            return _serialize_connection(_load_connection(db, tenant_id=tenant_id, connection_id=connection_id))
        updates.append("updated_at=CURRENT_TIMESTAMP")
        params.extend([connection_id, tenant_id])
        db.execute(
            f"UPDATE interaction_connections SET {', '.join(updates)} WHERE id=? AND tenant_id=?",
            params,
        )
        row = _load_connection(db, tenant_id=tenant_id, connection_id=connection_id)
    return _serialize_connection(row)


def rotate_webhook_secret(*, tenant_id: str, connection_id: str) -> dict[str, Any]:
    """Replace the webhook secret; the raw value is returned exactly once."""

    init_db()
    webhook_secret = f"whsec_{secrets.token_urlsafe(24)}"
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        _load_connection(db, tenant_id=tenant_id, connection_id=connection_id)
        db.execute(
            "UPDATE interaction_connections SET webhook_secret_hash=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND tenant_id=?",
            (hash_token(webhook_secret), connection_id, tenant_id),
        )
    return {"id": connection_id, "webhook_secret": webhook_secret}


def delete_connection(*, tenant_id: str, connection_id: str) -> None:
    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        _load_connection(db, tenant_id=tenant_id, connection_id=connection_id)
        db.execute(
            "DELETE FROM interaction_connections WHERE id=? AND tenant_id=?",
            (connection_id, tenant_id),
        )


def verify_connection_identity(
    *, tenant_id: str, connection_id: str, external_identity: str
) -> dict[str, Any]:
    """Record the provider-verified bot identity and mark the connection verified."""

    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        _load_connection(db, tenant_id=tenant_id, connection_id=connection_id)
        db.execute(
            "UPDATE interaction_connections SET external_identity=?, verified_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
            (external_identity[:200], connection_id, tenant_id),
        )
        row = _load_connection(db, tenant_id=tenant_id, connection_id=connection_id)
    return _serialize_connection(row)


def verify_webhook_secret(*, stored_hash: str, presented_secret: str | None) -> bool:
    """Constant-time webhook secret verification. Fails closed."""

    if not stored_hash or not presented_secret:
        return False
    computed = hash_token(presented_secret)
    return hmac.compare_digest(computed, stored_hash)


def decrypt_connection_credentials(connection: dict[str, Any]) -> dict[str, Any]:
    """Decrypt a connection's credential map for adapter use (never logged)."""

    raw = json.loads(str(connection.get("credentials_json") or "{}"))
    decrypted: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, str) and value.startswith("enc:v1:"):
            decrypted[key] = decrypt_secret(value)
        else:
            decrypted[key] = value
    return decrypted


# ------------------------------------------------------------------- links

def serialize_link(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item.setdefault("status", "active")
    return item


def find_active_link(*, connection_id: str, external_user_id: str) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT * FROM interaction_channel_links "
            "WHERE connection_id=? AND external_user_id=? AND status='active'",
            (connection_id, external_user_id),
        ).fetchone()
    return dict(row) if row else None


def create_link_token(
    *,
    tenant_id: str,
    project_id: str,
    user_id: int,
    connection_id: str,
    ttl_seconds: int | None = None,
    now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Issue a one-time, short-lived, hashed link token for an authenticated user."""

    init_db()
    clock = now or time.time
    settings = get_settings()
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.interaction_link_token_ttl_seconds)
    if ttl < 30:
        raise ValueError("Link tokens must live at least 30 seconds.")
    require_project_in_tenant(tenant_id=tenant_id, project_id=project_id)
    require_tenant_connection(tenant_id=tenant_id, connection_id=connection_id)
    raw_token = secrets.token_hex(16)
    token_id = _new_id("ilt")
    expires_at = clock() + ttl
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        db.execute(
            """INSERT INTO interaction_link_tokens
               (id,token_hash,tenant_id,project_id,user_id,connection_id,expires_at_epoch)
               VALUES(?,?,?,?,?,?,?)""",
            (token_id, hash_token(raw_token), tenant_id, project_id, user_id, connection_id, expires_at),
        )
    return {
        "id": token_id,
        "token": raw_token,
        "connection_id": connection_id,
        "project_id": project_id,
        "expires_at_epoch": expires_at,
        "ttl_seconds": ttl,
    }


class LinkConflictError(LinkError):
    """The external identity is already bound to a different Lynewright account."""


def redeem_link_token(
    *,
    connection_id: str,
    raw_token: str,
    external_user_id: str,
    external_username: str = "",
    now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Consume a link token and bind the external identity to its Lynewright scope.

    Guards enforced atomically:
      * token must exist, match this connection, be unexpired and unconsumed;
      * an active bind of this external identity to a *different* tenant/user
        refuses the link (cross-account takeover protection);
      * rebinding the same tenant/user refreshes the existing active link.
    """

    init_db()
    clock = now or time.time
    if not raw_token or not external_user_id:
        raise LinkError("A link token and external identity are required.")
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        token_row = db.execute(
            "SELECT * FROM interaction_link_tokens WHERE token_hash=?",
            (hash_token(raw_token),),
        ).fetchone()
        if token_row is None:
            raise LinkError("This link code is not valid.")
        if str(token_row["connection_id"]) != connection_id:
            raise LinkError("This link code was issued for a different connection.")
        if token_row["consumed_at_epoch"] is not None:
            raise LinkError("This link code was already used.")
        if clock() >= float(token_row["expires_at_epoch"]):
            raise LinkError("This link code has expired.")
        tenant_id = str(token_row["tenant_id"])
        user_id = int(token_row["user_id"])
        existing = db.execute(
            "SELECT * FROM interaction_channel_links "
            "WHERE connection_id=? AND external_user_id=? AND status='active'",
            (connection_id, external_user_id),
        ).fetchone()
        if existing is not None:
            same_account = (
                str(existing["tenant_id"]) == tenant_id and int(existing["user_id"]) == user_id
            )
            if not same_account:
                raise LinkConflictError(
                    "This chat is already linked to a different Lynewright account."
                )
            db.execute(
                "UPDATE interaction_channel_links SET external_username=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (external_username[:120], existing["id"]),
            )
            db.execute(
                "UPDATE interaction_link_tokens SET consumed_at_epoch=? WHERE id=?",
                (clock(), token_row["id"]),
            )
            refreshed = db.execute(
                "SELECT * FROM interaction_channel_links WHERE id=?", (existing["id"],)
            ).fetchone()
            return serialize_link(refreshed)
        consumed = db.execute(
            "UPDATE interaction_link_tokens SET consumed_at_epoch=? "
            "WHERE id=? AND consumed_at_epoch IS NULL",
            (clock(), token_row["id"]),
        ).rowcount
        if consumed != 1:
            raise LinkError("This link code was already used.")
        link_id = _new_id("iln")
        try:
            db.execute(
                """INSERT INTO interaction_channel_links
                   (id,tenant_id,project_id,user_id,connection_id,channel_type,
                    external_user_id,external_username,status)
                   SELECT ?,?,?,?,?,channel_type,?,?,'active' FROM interaction_connections
                   WHERE id=?""",
                (
                    link_id, tenant_id, str(token_row["project_id"]), user_id, connection_id,
                    external_user_id, external_username[:120], connection_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LinkConflictError(
                "This chat is already linked to a different Lynewright account."
            ) from exc
        row = db.execute(
            "SELECT * FROM interaction_channel_links WHERE id=?", (link_id,)
        ).fetchone()
    return serialize_link(row)


def list_links(*, tenant_id: str, connection_id: str | None = None) -> list[dict[str, Any]]:
    init_db()
    query = "SELECT * FROM interaction_channel_links WHERE tenant_id=?"
    params: list[Any] = [tenant_id]
    if connection_id:
        query += " AND connection_id=?"
        params.append(connection_id)
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(query + " ORDER BY created_at DESC, id DESC", params).fetchall()
    return [serialize_link(row) for row in rows]


def revoke_link(*, tenant_id: str, link_id: str) -> dict[str, Any]:
    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT * FROM interaction_channel_links WHERE id=? AND tenant_id=?",
            (link_id, tenant_id),
        ).fetchone()
        if row is None:
            raise LookupError("Channel link not found.")
        db.execute(
            "UPDATE interaction_channel_links SET status='revoked', revoked_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
            (link_id, tenant_id),
        )
        updated = db.execute(
            "SELECT * FROM interaction_channel_links WHERE id=?", (link_id,)
        ).fetchone()
    return serialize_link(updated)


def revoke_link_for_external(*, connection_id: str, external_user_id: str) -> bool:
    """/disconnect support: unlink the presenting chat identity. Returns True if unlinked."""

    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT id FROM interaction_channel_links "
            "WHERE connection_id=? AND external_user_id=? AND status='active'",
            (connection_id, external_user_id),
        ).fetchone()
        if row is None:
            return False
        db.execute(
            "UPDATE interaction_channel_links SET status='revoked', revoked_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (row["id"],),
        )
    return True


def prune_expired_link_tokens(now: Callable[[], float] | None = None) -> int:
    """Housekeeping helper; safe to call from any loop or maintenance task."""

    init_db()
    clock = now or time.time
    with closing(get_connection()) as db, db:
        cursor = db.execute(
            "DELETE FROM interaction_link_tokens WHERE expires_at_epoch < ?", (clock() - 86_400,)
        )
    return cursor.rowcount
