"""Persistent interaction conversations and message history.

A conversation is the durable channel thread identity: tenant, project,
connection, external chat id, optional future operator run binding (nullable —
the Operator module does not exist yet and nothing here requires it), plus
timestamps/state. Messages store bounded metadata; media is referenced as
durable Lynewright media ids, never blobs.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from typing import Any

from app.database import get_connection, init_db
from app.services.interaction_channels import OPERATOR_RUN_STATES
from app.services.interaction_schema import init_interaction_schema
from app.services.tenancy import require_project_in_tenant


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def _serialize_conversation(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return dict(row)


def ensure_conversation(
    *,
    connection: dict[str, Any],
    link_id: str | None,
    external_chat_id: str,
    title: str = "",
) -> dict[str, Any]:
    """Get-or-create the persistent conversation for one external chat."""

    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT * FROM interaction_conversations WHERE connection_id=? AND external_chat_id=?",
            (connection["id"], external_chat_id),
        ).fetchone()
        if row is not None:
            updates: list[str] = []
            params: list[Any] = []
            if title and title != row["title"]:
                updates.append("title=?")
                params.append(title[:200])
            if link_id and link_id != row["link_id"]:
                updates.append("link_id=?")
                params.append(link_id)
            if updates:
                updates.append("updated_at=CURRENT_TIMESTAMP")
                params.extend([row["id"]])
                db.execute(
                    f"UPDATE interaction_conversations SET {', '.join(updates)} WHERE id=?", params
                )
                row = db.execute(
                    "SELECT * FROM interaction_conversations WHERE id=?", (row["id"],)
                ).fetchone()
            return _serialize_conversation(row)
        conversation_id = _new_id("ico")
        try:
            db.execute(
                """INSERT INTO interaction_conversations
                   (id,tenant_id,project_id,connection_id,channel_type,link_id,external_chat_id,title)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    conversation_id, connection["tenant_id"], connection["project_id"],
                    connection["id"], connection["channel_type"], link_id,
                    external_chat_id, (title or "")[:200],
                ),
            )
        except sqlite3.IntegrityError:
            # Raced with another webhook worker; return the winner.
            row = db.execute(
                "SELECT * FROM interaction_conversations WHERE connection_id=? AND external_chat_id=?",
                (connection["id"], external_chat_id),
            ).fetchone()
            if row is None:
                raise
            return _serialize_conversation(row)
        row = db.execute(
            "SELECT * FROM interaction_conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        return _serialize_conversation(row)


def get_conversation(*, tenant_id: str, conversation_id: str) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT * FROM interaction_conversations WHERE id=? AND tenant_id=?",
            (conversation_id, tenant_id),
        ).fetchone()
    return dict(row) if row else None


def require_tenant_conversation(*, tenant_id: str, conversation_id: str) -> dict[str, Any]:
    row = get_conversation(tenant_id=tenant_id, conversation_id=conversation_id)
    if row is None:
        raise LookupError("Interaction conversation not found.")
    return row


def list_conversations(
    *,
    tenant_id: str,
    project_id: str | None = None,
    channel_type: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    init_db()
    where = ["tenant_id=?"]
    params: list[Any] = [tenant_id]
    if project_id:
        require_project_in_tenant(tenant_id=tenant_id, project_id=project_id)
        where.append("project_id=?")
        params.append(project_id)
    if channel_type:
        where.append("channel_type=?")
        params.append(channel_type)
    if not include_archived:
        where.append("state='active'")
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(
            f"SELECT * FROM interaction_conversations WHERE {' AND '.join(where)} "
            "ORDER BY COALESCE(last_inbound_at, created_at) DESC, id DESC",
            params,
        ).fetchall()
    return [_serialize_conversation(row) for row in rows]


def archive_conversation(*, tenant_id: str, conversation_id: str) -> dict[str, Any]:
    init_db()
    require_tenant_conversation(tenant_id=tenant_id, conversation_id=conversation_id)
    with closing(get_connection()) as db, db:
        db.execute(
            "UPDATE interaction_conversations SET state='archived', updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND tenant_id=?",
            (conversation_id, tenant_id),
        )
        row = db.execute(
            "SELECT * FROM interaction_conversations WHERE id=?", (conversation_id,)
        ).fetchone()
    return dict(row)


def set_run_binding(
    *, tenant_id: str, conversation_id: str, run_id: str | None, run_state: str | None
) -> dict[str, Any]:
    """Bind a future operator run to this conversation.

    Uses the shared run-state vocabulary; both fields are nullable so the
    binding can be cleared. No operator module is required to exist.
    """

    init_db()
    if run_state is not None and run_state not in OPERATOR_RUN_STATES:
        raise ValueError(
            "run_state must use the shared operator vocabulary: "
            + ", ".join(sorted(OPERATOR_RUN_STATES))
        )
    require_tenant_conversation(tenant_id=tenant_id, conversation_id=conversation_id)
    with closing(get_connection()) as db, db:
        db.execute(
            "UPDATE interaction_conversations SET active_run_id=?, run_state=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
            (run_id, run_state, conversation_id, tenant_id),
        )
        row = db.execute(
            "SELECT * FROM interaction_conversations WHERE id=?", (conversation_id,)
        ).fetchone()
    return dict(row)


# ------------------------------------------------------------------ messages

def serialize_message(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    try:
        item["media"] = json.loads(str(item.pop("media_json", "[]") or "[]"))
    except ValueError:
        item["media"] = []
    return item


def record_message(
    *,
    conversation: dict[str, Any],
    direction: str,
    actor: str = "system",
    message_type: str = "text",
    text: str = "",
    media_refs: list[dict[str, Any]] | None = None,
    action_id: str | None = None,
    external_message_id: str = "",
    status: str = "sent",
    detail: str = "",
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Persist one message; inbound messages dedupe on external message id.

    Returns the stored row. For duplicate inbound deliveries the existing row
    comes back unchanged so callers can treat it as already processed.
    """

    if direction not in {"inbound", "outbound"}:
        raise ValueError("direction must be 'inbound' or 'outbound'.")
    if actor not in {"user", "agent", "system", "operator"}:
        raise ValueError("actor must be one of user|agent|system|operator.")
    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        if direction == "inbound" and external_message_id:
            existing = db.execute(
                "SELECT * FROM interaction_messages WHERE conversation_id=? AND direction='inbound' "
                "AND external_message_id=?",
                (conversation["id"], external_message_id),
            ).fetchone()
            if existing is not None:
                return serialize_message(existing)
        message_id = _new_id("imsg")
        db.execute(
            """INSERT INTO interaction_messages
               (id,tenant_id,project_id,conversation_id,direction,actor,message_type,text,
                media_json,action_id,external_message_id,status,detail)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                message_id, conversation["tenant_id"], conversation["project_id"],
                conversation["id"], direction, actor, message_type, text[:8000],
                json.dumps(media_refs or []), action_id, external_message_id[:120], status,
                detail[:500],
            ),
        )
        stamp = (
            "last_inbound_at" if direction == "inbound" else "last_outbound_at"
        )
        db.execute(
            f"UPDATE interaction_conversations SET {stamp}=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (conversation["id"],),
        )
        row = db.execute(
            "SELECT * FROM interaction_messages WHERE id=?", (message_id,)
        ).fetchone()
        return serialize_message(row)


def message_already_recorded(
    db: sqlite3.Connection, *, conversation_id: str, external_message_id: str
) -> bool:
    if not external_message_id:
        return False
    return (
        db.execute(
            "SELECT 1 FROM interaction_messages WHERE conversation_id=? AND direction='inbound' "
            "AND external_message_id=?",
            (conversation_id, external_message_id),
        ).fetchone()
        is not None
    )


def list_messages(
    *, tenant_id: str, conversation_id: str, limit: int = 100
) -> list[dict[str, Any]]:
    init_db()
    require_tenant_conversation(tenant_id=tenant_id, conversation_id=conversation_id)
    limit = max(1, min(int(limit), 500))
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(
            "SELECT * FROM interaction_messages WHERE conversation_id=? AND tenant_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (conversation_id, tenant_id, limit),
        ).fetchall()
    return [serialize_message(row) for row in reversed(rows)]
