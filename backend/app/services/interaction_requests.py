"""The cross-session interaction boundary.

Inbound: every normalized channel message that wants Lynewright (or, later, the
Internal Operator) to act becomes a persisted ``interaction_requests`` row plus
a structured log event. Nothing here imports or requires an Operator module —
a future integration layer drains pending requests and answers them through
``submit_response``.

Outbound: a response is persisted first (durable queue), then delivered back
to the originating conversation through the conversation's channel adapter.
Delivery failure never loses the response; it stays queued/failed with detail.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from typing import Any

from app.services.application_logging import log_event
from app.database import get_connection, init_db
from app.services.interaction_channels import (
    ChannelError,
    OutboundMessage,
    default_registry,
)
from app.services.interaction_conversations import (
    record_message,
    require_tenant_conversation,
)
from app.services.interaction_linking import require_tenant_connection
from app.services.interaction_schema import init_interaction_schema

REQUEST_KINDS = ("message", "command", "callback", "media")
REQUEST_STATUSES = ("pending", "dispatched", "completed", "failed", "unsupported")
RESPONSE_ACTORS = ("agent", "system", "operator")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def _serialize_request(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    try:
        item["payload"] = json.loads(str(item.pop("payload_json", "{}") or "{}"))
    except ValueError:
        item["payload"] = {}
    return item


def _serialize_response(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    try:
        item["media"] = json.loads(str(item.pop("media_json", "[]") or "[]"))
    except ValueError:
        item["media"] = []
    return item


# -------------------------------------------------------------------- inbound

def create_interaction_request(
    *,
    conversation: dict[str, Any],
    kind: str,
    text: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one generic inbound interaction request (the Session 3 handoff).

    The request sits in ``pending`` until a future integration layer claims it.
    This function is the stable boundary other sessions wire into — no Operator
    module is imported or required.
    """

    if kind not in REQUEST_KINDS:
        raise ValueError(f"Unsupported interaction request kind: {kind}")
    init_db()
    request_id = _new_id("irq")
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        db.execute(
            """INSERT INTO interaction_requests
               (id,tenant_id,project_id,conversation_id,kind,text,payload_json,status)
               VALUES(?,?,?,?,?,?,?,'pending')""",
            (
                request_id, conversation["tenant_id"], conversation["project_id"],
                conversation["id"], kind, text[:8000],
                json.dumps(payload or {}, default=str)[:16_000],
            ),
        )
        row = db.execute("SELECT * FROM interaction_requests WHERE id=?", (request_id,)).fetchone()
    serialized = _serialize_request(row)
    log_event(
        level="info",
        category="interaction",
        source="interaction_gateway",
        message="interaction.request.created",
        tenant_id=serialized.get("tenant_id"),
        details={
            "event": "interaction.request.created",
            "request_id": request_id,
            "conversation_id": conversation["id"],
            "kind": kind,
        },
    )
    return serialized


def get_request(*, tenant_id: str, request_id: str) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT * FROM interaction_requests WHERE id=? AND tenant_id=?",
            (request_id, tenant_id),
        ).fetchone()
    return dict(row) if row else None


def list_requests(
    *,
    tenant_id: str,
    status: str | None = None,
    conversation_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    init_db()
    where = ["tenant_id=?"]
    params: list[Any] = [tenant_id]
    if status:
        where.append("status=?")
        params.append(status)
    if conversation_id:
        require_tenant_conversation(tenant_id=tenant_id, conversation_id=conversation_id)
        where.append("conversation_id=?")
        params.append(conversation_id)
    params.append(max(1, min(int(limit), 500)))
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(
            f"SELECT * FROM interaction_requests WHERE {' AND '.join(where)} "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            params,
        ).fetchall()
    return [_serialize_request(row) for row in rows]


def claim_pending_requests(limit: int = 20, *, consumer: str = "") -> list[dict[str, Any]]:
    """Atomically claim pending requests for routing (future integration layer)."""

    init_db()
    claimed: list[dict[str, Any]] = []
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        rows = db.execute(
            "SELECT * FROM interaction_requests WHERE status='pending' "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        for row in rows:
            updated = db.execute(
                "UPDATE interaction_requests SET status='dispatched', dispatched_to=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                (consumer[:120], row["id"]),
            ).rowcount
            if updated == 1:
                claimed.append(_serialize_request(row))
    return claimed


def mark_request_completed(request_id: str, *, response_message_id: str | None = None) -> bool:
    init_db()
    with closing(get_connection()) as db, db:
        updated = db.execute(
            "UPDATE interaction_requests SET status='completed', response_message_id=?, "
            "error='', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('pending','dispatched')",
            (response_message_id, request_id),
        ).rowcount
    return updated == 1


def mark_request_failed(request_id: str, error: str) -> bool:
    init_db()
    with closing(get_connection()) as db, db:
        updated = db.execute(
            "UPDATE interaction_requests SET status='failed', error=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('pending','dispatched')",
            (error[:500], request_id),
        ).rowcount
    return updated == 1


# ------------------------------------------------------------------- outbound

async def submit_response(
    *,
    tenant_id: str,
    project_id: str,
    conversation_id: str,
    text: str,
    actor: str = "system",
    media_refs: list[dict[str, Any]] | None = None,
    request_id: str | None = None,
    buttons: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Deliver a generic response back to the originating conversation.

    Accepts both deterministic system output and future agent output: the
    ``actor`` field records who produced the message and the channel does not
    care. The response row persists before delivery is attempted.
    """

    if actor not in RESPONSE_ACTORS:
        raise ValueError(f"actor must be one of {'|'.join(RESPONSE_ACTORS)}.")
    conversation = require_tenant_conversation(tenant_id=tenant_id, conversation_id=conversation_id)
    # The conversation identifies the destination chat, but deliberately does
    # not carry channel credentials.  Load its connection separately so the
    # channel adapter can decrypt and use the configured bot token.
    connection = require_tenant_connection(
        tenant_id=tenant_id,
        connection_id=str(conversation["connection_id"]),
    )
    if not text.strip() and not media_refs:
        raise ValueError("A response needs text or media.")
    init_db()
    response_id = _new_id("irs")
    media = list(media_refs or [])
    if request_id:
        request_row = db_request(tenant_id=tenant_id, request_id=request_id)
        if request_row is None:
            raise LookupError("Interaction request not found.")
        if str(request_row["conversation_id"]) != conversation["id"]:
            raise ValueError("Response request does not belong to this conversation.")
    registry = default_registry()
    adapter = registry.get(conversation["channel_type"])
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        db.execute(
            """INSERT INTO interaction_responses
               (id,tenant_id,project_id,request_id,conversation_id,actor,text,media_json,delivery_status)
               VALUES(?,?,?,?,?,?,?,?,'queued')""",
            (
                response_id, tenant_id, project_id, request_id, conversation["id"],
                actor, text[:8000], json.dumps(media),
            ),
        )
        row = db.execute("SELECT * FROM interaction_responses WHERE id=?", (response_id,)).fetchone()

    send_result = {"ok": False, "detail": "", "external_message_id": ""}
    try:
        result = await adapter.send_message(  # type: ignore[attr-defined]
            connection=connection,
            chat_ref=str(conversation["external_chat_id"]),
            message=OutboundMessage(text=text, media_refs=tuple(media), buttons=tuple(buttons or ())),
        )
        send_result = {
            "ok": result.ok,
            "detail": result.detail,
            "external_message_id": result.external_message_id,
        }
    except ChannelError as exc:
        send_result = {"ok": False, "detail": str(exc)[:300], "external_message_id": ""}
    delivery_status = "sent" if send_result["ok"] else "failed"
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        db.execute(
            "UPDATE interaction_responses SET delivery_status=?, detail=?, "
            "sent_external_message_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (
                delivery_status, send_result["detail"][:500],
                send_result["external_message_id"][:120], response_id,
            ),
        )
        row = db.execute("SELECT * FROM interaction_responses WHERE id=?", (response_id,)).fetchone()
    record_message(
        conversation=conversation,
        direction="outbound",
        actor=actor,
        message_type="text",
        text=text,
        media_refs=media,
        external_message_id=send_result["external_message_id"],
        status=delivery_status,
        detail=send_result["detail"],
    )
    if request_id and send_result["ok"]:
        mark_request_completed(request_id, response_message_id=response_id)
    elif request_id and not send_result["ok"]:
        # Request returns to the queue so the integration layer can retry.
        _requeue_request(request_id)
    return _serialize_response(row)


def db_request(*, tenant_id: str, request_id: str) -> dict[str, Any] | None:
    return get_request(tenant_id=tenant_id, request_id=request_id)


def _requeue_request(request_id: str) -> None:
    init_db()
    with closing(get_connection()) as db, db:
        db.execute(
            "UPDATE interaction_requests SET status='pending', dispatched_to='', "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='dispatched'",
            (request_id,),
        )


def list_responses(
    *, tenant_id: str, conversation_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    init_db()
    where = ["tenant_id=?"]
    params: list[Any] = [tenant_id]
    if conversation_id:
        require_tenant_conversation(tenant_id=tenant_id, conversation_id=conversation_id)
        where.append("conversation_id=?")
        params.append(conversation_id)
    params.append(max(1, min(int(limit), 500)))
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(
            f"SELECT * FROM interaction_responses WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [_serialize_response(row) for row in reversed(rows)]
