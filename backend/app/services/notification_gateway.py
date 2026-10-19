"""Provider-neutral notification gateway.

Unifies delivery *routing and transport* for deterministic Lynewright
notifications (no LLM involved) while leaving existing intelligence in place:
``alert_engine`` keeps deciding what an alert is, ``weekly_digest`` keeps
building digests — this layer only decides where a notification goes and
delivers it.

Delivery intents:
  * IMMEDIATE — push now through each routed channel.
  * DIGEST    — queue durably for the digest cadence to pick up later.
  * SILENT    — record only; no push.

Routing defaults (overridable per project through ``notification_routes``):
    critical     -> IMMEDIATE via telegram + email
    daily_summary-> DIGEST    via telegram
    approval     -> IMMEDIATE via telegram

Both system notifications and future agent-produced messages travel through
the same interface; the ``actor`` field distinguishes them.
"""

from __future__ import annotations

import json
import secrets
from contextlib import closing
from typing import Any, Awaitable, Callable

from app.database import get_connection, init_db
from app.services.interaction_channels import ChannelError, OutboundMessage, default_registry
from app.services.interaction_conversations import list_conversations
from app.services.interaction_schema import init_interaction_schema
from app.services.tenancy import require_project_in_tenant

INTENTS = ("IMMEDIATE", "DIGEST", "SILENT")
ChannelSender = Callable[..., Awaitable[tuple[str, dict[str, Any]]]]
_CHANNEL_SENDERS: dict[str, ChannelSender] = {}


def register_channel(name: str, sender: ChannelSender) -> Callable[[], bool]:
    """Register a notification transport; an empty registry is valid."""
    clean_name = str(name or "").strip().lower()
    if not clean_name or not clean_name.replace("_", "").isalnum():
        raise NotificationError("Channel names must be simple identifiers.")
    if not callable(sender):
        raise NotificationError("Channel sender must be callable.")
    previous = _CHANNEL_SENDERS.get(clean_name)
    _CHANNEL_SENDERS[clean_name] = sender

    def unregister() -> bool:
        if _CHANNEL_SENDERS.get(clean_name) is not sender:
            return False
        if previous is None:
            _CHANNEL_SENDERS.pop(clean_name, None)
        else:
            _CHANNEL_SENDERS[clean_name] = previous
        return True

    return unregister


DEFAULT_ROUTES: dict[str, dict[str, Any]] = {
    "critical": {
        "intent": "IMMEDIATE",
        "channels": [{"channel": "telegram"}],
    },
    "daily_summary": {
        "intent": "DIGEST",
        "channels": [{"channel": "telegram"}],
    },
    "approval": {
        "intent": "IMMEDIATE",
        "channels": [{"channel": "telegram"}],
    },
}


class NotificationError(ValueError):
    pass


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def _validate_channels(channels: Any) -> list[dict[str, Any]]:
    if not isinstance(channels, list) or not channels:
        raise NotificationError("channels must be a non-empty list.")
    validated: list[dict[str, Any]] = []
    for entry in channels:
        channel = str(entry.get("channel") or "").strip().lower() if isinstance(entry, dict) else ""
        if not channel or not channel.replace("_", "").isalnum():
            raise NotificationError("Each channel entry needs a simple channel name.")
        target = str(entry.get("target") or "")
        validated.append({"channel": channel, "target": target})
    return validated


def set_route(
    *,
    tenant_id: str,
    project_id: str,
    event_class: str,
    intent: str,
    channels: list[dict[str, Any]],
    enabled: bool = True,
) -> dict[str, Any]:
    init_db()
    clean_class = (event_class or "").strip().lower()
    if not clean_class or len(clean_class) > 100:
        raise NotificationError("event_class is required (max 100 chars).")
    if intent not in INTENTS:
        raise NotificationError(f"intent must be one of {'|'.join(INTENTS)}.")
    validated_channels = _validate_channels(channels)
    require_project_in_tenant(tenant_id=tenant_id, project_id=project_id)
    route_id = _new_id("nr")
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        db.execute(
            """INSERT INTO notification_routes
               (id,tenant_id,project_id,event_class,intent,channels_json,enabled)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(tenant_id, project_id, event_class) DO UPDATE SET
                 intent=excluded.intent,
                 channels_json=excluded.channels_json,
                 enabled=excluded.enabled,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                route_id, tenant_id, project_id, clean_class, intent,
                json.dumps(validated_channels), 1 if enabled else 0,
            ),
        )
        row = db.execute(
            "SELECT * FROM notification_routes WHERE tenant_id=? AND project_id=? AND event_class=?",
            (tenant_id, project_id, clean_class),
        ).fetchone()
        return _serialize_route(row)


def _serialize_route(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    try:
        item["channels"] = json.loads(str(item.pop("channels_json", "[]") or "[]"))
    except ValueError:
        item["channels"] = []
    return item


def get_route(*, tenant_id: str, project_id: str, event_class: str) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT * FROM notification_routes WHERE tenant_id=? AND project_id=? AND event_class=?",
            (tenant_id, project_id, (event_class or "").strip().lower()),
        ).fetchone()
    return _serialize_route(row) if row else None


def list_routes(*, tenant_id: str, project_id: str | None = None) -> list[dict[str, Any]]:
    init_db()
    query = "SELECT * FROM notification_routes WHERE tenant_id=?"
    params: list[Any] = [tenant_id]
    if project_id:
        require_project_in_tenant(tenant_id=tenant_id, project_id=project_id)
        query += " AND project_id=?"
        params.append(project_id)
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(query + " ORDER BY event_class", params).fetchall()
    return [_serialize_route(row) for row in rows]


def delete_route(*, tenant_id: str, project_id: str, event_class: str) -> bool:
    init_db()
    with closing(get_connection()) as db, db:
        cursor = db.execute(
            "DELETE FROM notification_routes WHERE tenant_id=? AND project_id=? AND event_class=?",
            (tenant_id, project_id, (event_class or "").strip().lower()),
        )
        return cursor.rowcount > 0


# ------------------------------------------------------------------ deliveries

def _record_delivery(
    *,
    tenant_id: str,
    project_id: str,
    notification_key: str,
    event_class: str,
    intent: str,
    channel_type: str,
    target: str,
    status: str,
    detail: str = "",
    conversation_id: str | None = None,
    connection_id: str | None = None,
) -> dict[str, Any] | None:
    """Insert one delivery attempt row. Deduped by key; returns None on dupes."""

    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        delivery_id = _new_id("nd")
        try:
            db.execute(
                """INSERT INTO notification_deliveries
                   (id,tenant_id,project_id,notification_key,event_class,intent,
                    channel_type,target,conversation_id,status,detail)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    delivery_id, tenant_id, project_id, notification_key, event_class,
                    intent, channel_type, target[:500], conversation_id, status, detail[:500],
                ),
            )
        except Exception:
            # Unique dedupe index hit (partial index only fires when a key is
            # present); any other insert failure is surfaced as suppressed.
            existing = None
            if notification_key:
                existing = db.execute(
                    "SELECT id FROM notification_deliveries WHERE tenant_id=? AND notification_key=? "
                    "AND channel_type=? AND target=?",
                    (tenant_id, notification_key, channel_type, target[:500]),
                ).fetchone()
            return None
        row = db.execute(
            "SELECT * FROM notification_deliveries WHERE id=?", (delivery_id,)
        ).fetchone()
        return dict(row)


def has_delivery(*, tenant_id: str, notification_key: str) -> bool:
    if not notification_key:
        return False
    init_db()
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT 1 FROM notification_deliveries WHERE tenant_id=? AND notification_key=? LIMIT 1",
            (tenant_id, notification_key),
        ).fetchone()
    return row is not None


def due_digest_items(
    *, tenant_id: str, project_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    init_db()
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(
            "SELECT * FROM notification_deliveries WHERE tenant_id=? AND project_id=? "
            "AND status='queued_digest' ORDER BY created_at ASC LIMIT ?",
            (tenant_id, project_id, max(1, min(int(limit), 500))),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_digest_flushed(delivery_ids: list[str]) -> int:
    init_db()
    if not delivery_ids:
        return 0
    updated = 0
    with closing(get_connection()) as db, db:
        for delivery_id in delivery_ids:
            updated += db.execute(
                "UPDATE notification_deliveries SET status='flushed', "
                "detail=CASE WHEN detail='' THEN 'digest flushed' ELSE detail END "
                "WHERE id=?",
                (delivery_id,),
            ).rowcount
    return updated


# -------------------------------------------------------------------- sending

def _render_text(*, title: str, body: str, actor: str) -> str:
    prefix = "[Lynewright Agent]" if actor == "agent" else "[Lynewright]"
    parts = [f"{prefix} {title}".strip()]
    if body:
        parts.append(body)
    return "\n".join(parts)[:4000]


async def _deliver_telegram(
    *, tenant_id: str, project_id: str, text: str, media_refs: list[dict[str, Any]],
    chat_ref: str | None, buttons: tuple[tuple[str, Any], ...] = (), parse_mode: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Deliver via Telegram. Returns (target, result dict)."""

    registry = default_registry()
    adapter = registry.get("telegram")
    conversations = [
        convo
        for convo in list_conversations(
            tenant_id=tenant_id, project_id=project_id, channel_type="telegram"
        )
        if convo.get("external_chat_id")
    ]
    if chat_ref:
        conversations = [c for c in conversations if c["external_chat_id"] == chat_ref]
        if not conversations:
            return chat_ref, {"status": "suppressed", "detail": "chat not linked"}
    if not conversations:
        return "", {"status": "suppressed", "detail": "no linked telegram chat"}
    last: dict[str, Any] = {"status": "suppressed", "detail": "no linked telegram chat"}
    from contextlib import closing as _closing
    from app.database import get_connection as _get_connection

    for conversation in conversations:
        connection_row = None
        with _closing(_get_connection()) as db:
            row = db.execute(
                "SELECT * FROM interaction_connections WHERE id=?", (conversation["connection_id"],)
            ).fetchone()
            connection_row = dict(row) if row else None
        if connection_row is None or not connection_row.get("enabled"):
            last = {"status": "suppressed", "detail": "connection disabled"}
            continue
        try:
            result = await adapter.send_message(  # type: ignore[attr-defined]
                connection=connection_row,
                chat_ref=str(conversation["external_chat_id"]),
                message=OutboundMessage(
                    text=text, media_refs=tuple(media_refs), buttons=buttons, parse_mode=parse_mode,
                ),
            )
        except ChannelError as exc:
            last = {"status": "failed", "detail": str(exc)[:300]}
            continue
        last = (
            {"status": "sent", "detail": "", "external_message_id": result.external_message_id}
            if result.ok
            else {"status": "failed", "detail": result.detail}
        )
    return str(conversations[-1]["external_chat_id"]) if conversations else "", last


async def _telegram_channel_sender(
    *, tenant_id: str, project_id: str, text: str, media_refs: list[dict[str, Any]],
    chat_ref: str | None, buttons: tuple[tuple[str, Any], ...], parse_mode: str | None,
    **_: Any,
) -> tuple[str, dict[str, Any]]:
    return await _deliver_telegram(
        tenant_id=tenant_id, project_id=project_id, text=text, media_refs=media_refs,
        chat_ref=chat_ref, buttons=buttons, parse_mode=parse_mode,
    )


register_channel("telegram", _telegram_channel_sender)


async def send_notification(
    *,
    tenant_id: str,
    project_id: str,
    event_class: str,
    title: str,
    body: str = "",
    intent: str | None = None,
    data: dict[str, Any] | None = None,
    dedupe_key: str = "",
    actor: str = "system",
    media_refs: list[dict[str, Any]] | None = None,
    raw_text: str | None = None,
    buttons: tuple[tuple[str, Any], ...] | list[tuple[str, Any]] = (),
    parse_mode: str | None = None,
    telegram_chat_ref: str | None = None,
) -> dict[str, Any]:
    """Send one deterministic Lynewright notification through the routed channels.

    Never raises on channel failures — every attempt is recorded and returned.
    Raises only for invalid arguments or unknown projects/tenants.
    """

    clean_class = (event_class or "").strip().lower()
    if not clean_class:
        raise NotificationError("event_class is required.")
    require_project_in_tenant(tenant_id=tenant_id, project_id=project_id)
    resolved_intent = (intent or "").strip().upper() or None
    if resolved_intent is not None and resolved_intent not in INTENTS:
        raise NotificationError(f"intent must be one of {'|'.join(INTENTS)}.")
    route = get_route(tenant_id=tenant_id, project_id=project_id, event_class=clean_class)
    if route:
        effective_intent = resolved_intent or str(route["intent"])
    else:
        effective_intent = resolved_intent or str(
            DEFAULT_ROUTES.get(clean_class, {}).get("intent", "IMMEDIATE")
        )
    channel_specs: list[dict[str, Any]] = []
    if route and route.get("enabled"):
        channel_specs = [dict(entry) for entry in (route.get("channels") or [])]
    elif not route:
        channel_specs = [
            dict(entry) for entry in DEFAULT_ROUTES.get(clean_class, {}).get("channels", [])
        ]
    if not channel_specs:
        channel_specs = [{"channel": "telegram"}]
    if dedupe_key and has_delivery(tenant_id=tenant_id, notification_key=dedupe_key):
        return {
            "duplicate": True,
            "event_class": clean_class,
            "intent": effective_intent,
            "results": [],
        }
    text = (
        str(raw_text) if raw_text is not None and parse_mode else
        (str(raw_text)[:4096] if raw_text is not None else _render_text(title=title, body=body, actor=actor))
    )
    rendered_buttons = tuple(buttons)
    media = list(media_refs or [])
    results: list[dict[str, Any]] = []
    for spec in channel_specs:
        channel = str(spec.get("channel"))
        target = str(spec.get("target") or "")
        if effective_intent == "SILENT":
            recorded = _record_delivery(
                tenant_id=tenant_id, project_id=project_id, notification_key=dedupe_key,
                event_class=clean_class, intent=effective_intent, channel_type=channel,
                target=target, status="silent", detail="silent intent; not pushed",
            )
            results.append({"channel": channel, "target": target, **({"status": "silent"} if recorded else {"status": "duplicate"})})
            continue
        if effective_intent == "DIGEST":
            recorded = _record_delivery(
                tenant_id=tenant_id, project_id=project_id, notification_key=dedupe_key,
                event_class=clean_class, intent=effective_intent, channel_type=channel,
                target=target, status="queued_digest", detail="queued for digest cadence",
            )
            results.append({
                "channel": channel, "target": target,
                **({"status": "queued_digest"} if recorded else {"status": "duplicate"}),
            })
            continue
        sender = _CHANNEL_SENDERS.get(channel)
        if sender is None:
            outcome = {"status": "suppressed", "detail": "channel is not registered"}
            delivered_target = target
        else:
            chat_ref = telegram_chat_ref or str(spec.get("chat_ref") or "") or None
            try:
                delivered_target, outcome = await sender(
                    tenant_id=tenant_id, project_id=project_id, text=text,
                    media_refs=media, chat_ref=chat_ref, buttons=rendered_buttons,
                    parse_mode=parse_mode, target=target, title=title, body=body,
                    actor=actor, data=data or {},
                )
            except Exception as exc:
                delivered_target = target
                outcome = {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"[:300]}
        _record_delivery(
            tenant_id=tenant_id, project_id=project_id, notification_key=dedupe_key,
            event_class=clean_class, intent=effective_intent, channel_type=channel,
            target=delivered_target, status=str(outcome.get("status") or "failed"),
            detail=outcome.get("detail", ""),
        )
        results.append({"channel": channel, "target": delivered_target, **outcome})
    return {
        "duplicate": False,
        "event_class": clean_class,
        "intent": effective_intent,
        "results": results,
    }


def list_deliveries(
    *, tenant_id: str, project_id: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    init_db()
    query = "SELECT * FROM notification_deliveries WHERE tenant_id=?"
    params: list[Any] = [tenant_id]
    if project_id:
        require_project_in_tenant(tenant_id=tenant_id, project_id=project_id)
        query += " AND project_id=?"
        params.append(project_id)
    params.append(max(1, min(int(limit), 500)))
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(query + " ORDER BY created_at DESC, id DESC LIMIT ?", params).fetchall()
    return [dict(row) for row in rows]
