"""Telegram webhook ingestion and update orchestration.

Pipeline for every delivered update:
  1. size + rate limits (per connection);
  2. webhook secret verification (constant-time, fails closed);
  3. JSON parse — malformed updates are acknowledged without processing;
  4. replay protection via ``interaction_processed_updates``;
  5. routing:
       /connect <token>            -> secure account linking
       /disconnect | /unlink       -> unlink the presenting identity
       messages from linked users  -> conversation + generic interaction request
       callback queries            -> approval transport decisions
       photos/documents            -> durable Lynewright media + media request

Unlinked chats never reach Lynewright state beyond a neutral refusal: no tools,
no conversations, no requests are created. Provider failures during outbound
acknowledgement never fail the webhook itself.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import get_settings
from app.database import get_connection, init_db
from app.services import choice_channel, image_review_channel
from app.services.application_logging import log_event
from app.services.interaction_actions import (
    ActionAlreadyResolvedError,
    ActionError,
    cancel_action,
    decode_callback_data,
    list_actions,
    resolve_action_from_channel,
)
from app.services.interaction_channels import ChannelCapability, default_registry
from app.services.interaction_conversations import (
    ensure_conversation,
    record_message,
    set_run_binding,
)
from app.services.interaction_linking import (
    LinkConflictError,
    LinkError,
    find_active_link,
    load_connection_any_tenant,
    redeem_link_token,
    revoke_link_for_external,
    verify_webhook_secret,
)
from app.services.interaction_requests import create_interaction_request
from app.services.interaction_schema import init_interaction_schema
from app.services.platform_services import MediaService
from app.services.integration_media_transfer import managed_media_root
from app.services.rate_limiter import RateLimiterUnavailable
from app.services.telegram_adapter import CHANNEL_TYPE


class WebhookRejected(Exception):
    """Raised when the webhook must refuse the delivery outright."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
_MAX_INBOUND_MEDIA_BYTES = 20_000_000
_RESET_TASK_COMMANDS = frozenset({"/new", "/break", "/breack"})
_APPROVE_TASK_REPLIES = frozenset({
    "continue", "/continue", "approve", "/approve", "yes", "ok",
    "تابع", "استمر", "موافق", "نعم",
})
ReviewUploadHandler = Callable[..., Awaitable[dict[str, Any]]]
_review_upload_handler: ReviewUploadHandler | None = None


def register_review_upload_handler(handler: ReviewUploadHandler) -> Callable[[], bool]:
    """Register optional review-image upload processing."""
    global _review_upload_handler
    previous = _review_upload_handler
    _review_upload_handler = handler

    def unregister() -> bool:
        global _review_upload_handler
        if _review_upload_handler is not handler:
            return False
        _review_upload_handler = previous
        return True

    return unregister



def _record_update(db: sqlite3.Connection, *, connection_id: str, update_id: int) -> bool:
    """True when first seen; False on replay."""

    try:
        db.execute(
            "INSERT INTO interaction_processed_updates (connection_id, update_id) VALUES (?,?)",
            (connection_id, update_id),
        )
    except sqlite3.IntegrityError:
        return False
    # Keep the dedupe table bounded; seven days of replay cover is generous.
    db.execute(
        "DELETE FROM interaction_processed_updates WHERE processed_at < datetime('now', '-7 days')"
    )
    return True


async def handle_webhook(*, connection_id: str, headers: Any, raw_body: bytes) -> dict[str, Any]:
    settings = get_settings()
    if not settings.telegram_enabled:
        raise WebhookRejected(404, "Telegram channel is not enabled on this deployment.")
    if len(raw_body) > settings.interaction_webhook_max_body_bytes:
        raise WebhookRejected(413, "Webhook body exceeds the allowed size.")
    try:
        allowed = get_rate_limiter().consume(
            namespace="telegram_webhook",
            key=connection_id,
            maximum=300,
            window_seconds=60,
        )
    except RateLimiterUnavailable as exc:
        raise WebhookRejected(503, str(exc)) from exc
    if not allowed:
        raise WebhookRejected(429, "Webhook rate limit exceeded.")
    try:
        connection = load_connection_any_tenant(connection_id)
    except LookupError as exc:
        raise WebhookRejected(404, "Unknown webhook endpoint.") from exc
    if connection.get("channel_type") != CHANNEL_TYPE:
        raise WebhookRejected(400, "Endpoint is not a Telegram channel.")
    if not connection.get("enabled"):
        raise WebhookRejected(409, "This connection is disabled.")
    presented_secret = _header(headers, TELEGRAM_SECRET_HEADER)
    if not verify_webhook_secret(
        stored_hash=str(connection.get("webhook_secret_hash") or ""),
        presented_secret=presented_secret,
    ):
        log_event(
            level="warning", category="interaction", source="telegram_webhook",
            message="Rejected Telegram webhook with an invalid secret token.",
            tenant_id=connection.get("tenant_id"),
            details={"connection_id": connection_id},
        )
        raise WebhookRejected(401, "Invalid webhook secret token.")
    try:
        update = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"ok": True, "ignored": "malformed_body"}
    if not isinstance(update, dict) or "update_id" not in update:
        return {"ok": True, "ignored": "malformed_update"}
    try:
        update_id = int(update["update_id"])
    except (TypeError, ValueError):
        return {"ok": True, "ignored": "malformed_update"}
    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        fresh = _record_update(db, connection_id=connection_id, update_id=update_id)
    if not fresh:
        return {"ok": True, "duplicate": True}
    outcome: dict[str, Any] = {"ok": True}
    try:
        if isinstance(update.get("callback_query"), dict):
            outcome.update(await _handle_callback_query(connection=connection, query=update["callback_query"]))
        elif isinstance(update.get("message"), dict):
            outcome.update(await _handle_message(connection=connection, message=update["message"]))
        else:
            outcome["ignored"] = "unsupported_update_type"
    except Exception as exc:  # one bad update must never 500 the bot route
        log_event(
            level="error", category="interaction", source="telegram_webhook",
            message="Failed to process a Telegram update.",
            tenant_id=connection.get("tenant_id"),
            details={"connection_id": connection_id, "error_type": type(exc).__name__},
            exc=exc,
        )
        outcome = {"ok": True, "error": "processing_failed"}
    return outcome


def get_rate_limiter():  # re-exported indirection keeps patching simple in tests
    from app.services.rate_limiter import get_rate_limiter as _get

    return _get()


def _header(headers: Any, name: str) -> str | None:
    """Read a header from Starlette Headers or a plain dict (case-tolerant)."""

    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name)
    if value is None and isinstance(headers, dict):
        value = getter(name.lower())
    return value


# ------------------------------------------------------------------- inbound

def _chat_ref(message: dict[str, Any]) -> str:
    chat = message.get("chat") or {}
    return str(chat.get("id") or "")


def _sender(message: dict[str, Any]) -> dict[str, Any]:
    sender = message.get("from")
    return sender if isinstance(sender, dict) else {}


def _is_command(text: str) -> bool:
    return text.startswith("/")


def _command_name_and_args(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", []
    return parts[0].split("@", 1)[0].lower(), parts[1:]


async def _handle_message(*, connection: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    text = str(message.get("text") or message.get("caption") or "")
    sender = _sender(message)
    external_user_id = str(sender.get("id") or "")
    chat_ref = _chat_ref(message)
    if not external_user_id or not chat_ref:
        return {"ignored": "missing_identity"}
    link = find_active_link(connection_id=str(connection["id"]), external_user_id=external_user_id)
    if link is None:
        return await _handle_unlinked(
            connection=connection, message=message, text=text,
            external_user_id=external_user_id, external_username=str(sender.get("username") or ""),
            chat_ref=chat_ref,
        )

    reply = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
    replied_message_id = str(reply.get("message_id") or "")
    has_media = isinstance(message.get("photo"), list) or isinstance(message.get("document"), dict)
    if has_media:
        file_payload = _review_image_file_payload(message)
        if file_payload is None:
            await _reply(connection=connection, chat_ref=chat_ref,
                         text="Send a supported image file for review.")
            return {"ok": True, "handled": "review_upload_rejected", "reason": "unsupported_type"}
        handler = _review_upload_handler
        if handler is None:
            await _reply(connection=connection, chat_ref=chat_ref, text="Review image uploads are not configured.")
            return {"ok": True, "handled": "review_upload_unavailable"}
        return await handler(
            connection=connection, link=link, chat_ref=chat_ref,
            reply_to_message_id=replied_message_id, file_payload=file_payload,
            message_id=str(message.get("message_id") or ""),
        )

    if replied_message_id and choice_channel.is_choice_reply(
        connection_id=str(connection["id"]), chat_ref=chat_ref, message_id=replied_message_id,
    ):
        await _reply(connection=connection, chat_ref=chat_ref,
                     text="Use the buttons on that choice message to answer it.")
        return {"ok": True, "handled": "choice_reply"}

    taken = image_review_channel.take_reason(
        connection_id=str(connection["id"]), chat_ref=chat_ref, link=link, text=text,
    )
    if taken is not None:
        session, item = taken["session"], taken["item"]
        await _refresh_approve_message(connection, session)
        await _reply(connection=connection, chat_ref=chat_ref,
                     text=(f"{item['label']} refused: {item['reason']}\n"
                           f"When you're done, tap \"{image_review_channel.approve_label(session['items'])}\"."))
        return {"ok": True, "handled": "image_review_reason"}

    lowered = text.strip().lower()
    if lowered.startswith("/disconnect") or lowered.startswith("/unlink"):
        removed = revoke_link_for_external(
            connection_id=str(connection["id"]), external_user_id=external_user_id,
        )
        await _reply(connection=connection, chat_ref=chat_ref,
                     text="This chat has been unlinked." if removed else "This chat was not linked.")
        return {"ok": True, "handled": "disconnect"}
    if lowered.startswith("/connect") or lowered.startswith("/start"):
        await _reply(connection=connection, chat_ref=chat_ref,
                     text="This chat is already linked. Send /disconnect first to link another workspace.")
        return {"ok": True, "handled": "already_linked"}

    adapter = default_registry().get(CHANNEL_TYPE)
    media_refs: list[dict[str, Any]] = []
    photo_entry = _largest_photo(message)
    document_entry = message.get("document") if isinstance(message.get("document"), dict) else None
    supports_files = adapter.supports(ChannelCapability.FILE_RECEIVE)  # type: ignore[attr-defined]
    if supports_files and photo_entry is not None:
        ref = await _store_inbound_media(
            connection=connection, project_id=str(link["project_id"]), file_payload=photo_entry,
            default_name="photo.jpg", default_mime="image/jpeg",
        )
        if ref:
            media_refs.append(ref.as_dict())
    elif supports_files and document_entry is not None:
        ref = await _store_inbound_media(
            connection=connection, project_id=str(link["project_id"]), file_payload=document_entry,
            default_name=str(document_entry.get("file_name") or "document.bin"),
            default_mime=str(document_entry.get("mime_type") or "application/octet-stream"),
        )
        if ref:
            media_refs.append(ref.as_dict())

    conversation = ensure_conversation(
        connection=connection, link_id=str(link["id"]), external_chat_id=chat_ref,
        title=_chat_title(message),
    )
    recorded = record_message(
        conversation=conversation, direction="inbound", actor="user",
        message_type="photo" if media_refs else "text", text=text[:8000], media_refs=media_refs,
        external_message_id=str(message.get("message_id") or ""), status="received",
    )
    if lowered in _RESET_TASK_COMMANDS:
        await _reset_active_task(connection=connection, conversation=conversation, chat_ref=chat_ref)
        return {"ok": True, "handled": "new_task", "conversation_id": conversation["id"]}
    if lowered in _APPROVE_TASK_REPLIES and str(conversation.get("run_state") or "") == "waiting_approval":
        pending = list_actions(
            tenant_id=str(connection["tenant_id"]), conversation_id=str(conversation["id"]),
            status="pending", limit=1,
        )
        if pending:
            try:
                action = resolve_action_from_channel(
                    action_id=str(pending[0]["id"]), decision="approve",
                    link_tenant_id=str(link["tenant_id"]), link_id=str(link["id"]),
                    note="Approved by plain-text Telegram reply.",
                )
            except (ActionAlreadyResolvedError, ActionError):
                action = None
            if action is not None:
                await _reply(connection=connection, chat_ref=chat_ref,
                             text="Approved. I'm continuing the same task.")
                return {"ok": True, "handled": "approval", "decision": "approve",
                        "action_id": action["id"], "conversation_id": conversation["id"]}
    command_name, _ = _command_name_and_args(text)
    if command_name:
        await _reply(connection=connection, chat_ref=chat_ref,
                     text="I can't run that command. Send an objective in plain text.")
        return {"ok": True, "handled": "unsupported_command", "command": command_name}
    request = create_interaction_request(
        conversation=conversation,
        kind="media" if media_refs else "message",
        text=text[:8000],
        payload={"message_row_id": recorded["id"], "media": media_refs,
                 "external_message_id": recorded.get("external_message_id")},
    )
    return {"accepted": True, "request_id": request["id"], "conversation_id": conversation["id"]}

def _review_image_file_payload(message: dict[str, Any]) -> dict[str, Any] | None:
    """Prefer an uploaded image document to Telegram's compressed photo."""
    document = message.get("document") if isinstance(message.get("document"), dict) else None
    if document:
        name = str(document.get("file_name") or "image")
        suffix = Path(name).suffix.lower()
        mime = str(document.get("mime_type") or "").split(";", 1)[0].strip().lower()
        types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
        mime_suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
        if suffix in types and (not mime or mime == types[suffix]):
            return {
                "file_id": str(document.get("file_id") or ""),
                "file_size": int(document.get("file_size") or 0),
                "file_name": name[:255], "mime_type": types[suffix],
            }
        if mime in mime_suffix and (not suffix or suffix not in types):
            safe_name = name if not suffix else f"image{mime_suffix[mime]}"
            return {
                "file_id": str(document.get("file_id") or ""),
                "file_size": int(document.get("file_size") or 0),
                "file_name": safe_name[:255], "mime_type": mime,
            }
    photo = _largest_photo(message)
    if photo:
        return {
            "file_id": str(photo.get("file_id") or ""),
            "file_size": int(photo.get("file_size") or 0),
            "file_name": "photo.jpg", "mime_type": "image/jpeg",
        }
    return None


async def _reset_active_task(*, connection: dict[str, Any], conversation: dict[str, Any],
                             chat_ref: str) -> None:
    """Cancel and detach the old task so later output cannot leak into the new one."""
    tenant_id = str(connection["tenant_id"])
    run_id = str(conversation.get("active_run_id") or "")
    for action in list_actions(
        tenant_id=tenant_id, conversation_id=str(conversation["id"]),
        status="pending", limit=20,
    ):
        try:
            cancel_action(
                tenant_id=tenant_id, action_id=str(action["id"]),
                reason="Telegram conversation started a new task.",
            )
        except (ActionAlreadyResolvedError, ActionError):
            pass
    if run_id:
        try:
            from app.services.operator_dispatch import discard_interaction_outcome
            from app.services.operator_runtime import OperatorService

            discard_interaction_outcome(run_id)
            OperatorService(tenant_id=tenant_id).cancel_run(
                run_id, reason="Telegram user started a new task.",
            )
        except Exception:
            # Reset still wins if the prior run already finished between reads.
            pass
    set_run_binding(
        tenant_id=tenant_id, conversation_id=str(conversation["id"]),
        run_id=None, run_state=None,
    )
    await _reply(
        connection=connection, chat_ref=chat_ref,
        text="Started a new task. Send the next objective.",
    )


def _chat_title(message: dict[str, Any]) -> str:
    chat = message.get("chat") or {}
    for key in ("title", "first_name", "username"):
        value = chat.get(key)
        if value:
            return f"{key}:{value}"
    return ""


def _largest_photo(message: dict[str, Any]) -> dict[str, Any] | None:
    photos = message.get("photo")
    if not isinstance(photos, list) or not photos:
        return None
    sized = [entry for entry in photos if isinstance(entry, dict) and entry.get("file_id")]
    if not sized:
        return None
    return max(sized, key=lambda entry: int(entry.get("file_size") or 0))


async def _handle_unlinked(
    *,
    connection: dict[str, Any],
    message: dict[str, Any],
    text: str,
    external_user_id: str,
    external_username: str,
    chat_ref: str,
) -> dict[str, Any]:
    command = text.strip()
    lowered = command.lower()
    if lowered.startswith("/connect"):
        parts = command.split(None, 1)
        token = parts[1].strip() if len(parts) > 1 else ""
        return await _handle_connect(
            connection=connection, token=token, external_user_id=external_user_id,
            external_username=external_username, chat_ref=chat_ref,
        )
    if lowered.startswith("/start"):
        parts = command.split(None, 1)
        if len(parts) > 1 and parts[1].strip():
            return await _handle_connect(
                connection=connection, token=parts[1].strip(),
                external_user_id=external_user_id, external_username=external_username,
                chat_ref=chat_ref,
            )
        await _reply(connection=connection, chat_ref=chat_ref, text=(
            "Welcome. Generate a linking code in Lynewright (Settings → Channels), then send "
            "/connect CODE to link this chat."
        ))
        return {"ok": True, "handled": "start"}
    if lowered.startswith("/disconnect") or lowered.startswith("/unlink"):
        removed = revoke_link_for_external(
            connection_id=str(connection["id"]), external_user_id=external_user_id
        )
        await _reply(
            connection=connection, chat_ref=chat_ref,
            text="This chat has been unlinked." if removed else "This chat was not linked.",
        )
        return {"ok": True, "handled": "disconnect"}
    # Never create conversations/requests or execute anything for unlinked chats.
    is_private = ((message.get("chat") or {}).get("type")) == "private"
    if is_private:
        await _reply(
            connection=connection, chat_ref=chat_ref,
            text="This chat is not linked to Lynewright. Send /connect CODE with a code from "
                 "Lynewright Settings → Channels.",
        )
    log_event(
        level="info", category="interaction", source="telegram_webhook",
        message="Ignored message from an unlinked Telegram chat.",
        tenant_id=connection.get("tenant_id"),
        details={"connection_id": str(connection.get("id")), "chat_type":
                 str((message.get("chat") or {}).get("type") or "")},
    )
    return {"ok": True, "ignored": "unlinked"}


async def _handle_connect(
    *,
    connection: dict[str, Any],
    token: str,
    external_user_id: str,
    external_username: str,
    chat_ref: str,
) -> dict[str, Any]:
    if not token:
        await _reply(
            connection=connection, chat_ref=chat_ref,
            text="Send /connect CODE using the code shown in Lynewright Settings → Channels.",
        )
        return {"ok": True, "handled": "connect_missing_token"}
    try:
        link = redeem_link_token(
            connection_id=str(connection["id"]),
            raw_token=token,
            external_user_id=external_user_id,
            external_username=external_username,
        )
    except LinkConflictError as exc:
        await _reply(connection=connection, chat_ref=chat_ref, text=f"Not linked: {exc}")
        return {"ok": True, "handled": "connect_conflict"}
    except LinkError as exc:
        await _reply(connection=connection, chat_ref=chat_ref, text=f"Not linked: {exc}")
        return {"ok": True, "handled": "connect_rejected"}
    await _reply(
        connection=connection, chat_ref=chat_ref,
        text="Linked. This Telegram chat can now receive Lynewright notifications and "
             "conversations for your workspace.",
    )
    log_event(
        level="info", category="interaction", source="telegram_webhook",
        message="Telegram chat linked via one-time code.",
        tenant_id=link.get("tenant_id"),
        details={"link_id": str(link.get("id")), "connection_id": str(connection.get("id"))},
    )
    return {"ok": True, "handled": "connected", "link_id": str(link.get("id"))}


async def _handle_callback_query(
    *, connection: dict[str, Any], query: dict[str, Any]
) -> dict[str, Any]:
    settings = get_settings()
    callback_id = str(query.get("id") or "")
    data = str(query.get("data") or "")
    sender = query.get("from") if isinstance(query.get("from"), dict) else {}
    external_user_id = str(sender.get("id") or "")
    decoded = decode_callback_data(data)

    async def answer(text: str, alert: bool = False) -> None:
        try:
            registry = default_registry()
            adapter = registry.get(CHANNEL_TYPE)
            client = adapter.client_for(connection)  # type: ignore[attr-defined]
            await client.answer_callback_query(callback_id, text=text, show_alert=alert)
        except Exception:
            pass

    if data.startswith(image_review_channel.CALLBACK_PREFIX):
        return await _handle_image_review_callback(
            connection=connection, query=query, data=data,
            external_user_id=external_user_id, answer=answer,
        )
    if data.startswith(choice_channel.CALLBACK_PREFIX):
        return await _handle_choice_callback(
            connection=connection, query=query, data=data,
            external_user_id=external_user_id, answer=answer,
        )
    if decoded is None:
        await answer("Unrecognized action.", alert=True)
        return {"ok": True, "ignored": "foreign_callback"}
    action_id, decision = decoded
    link = find_active_link(connection_id=str(connection["id"]), external_user_id=external_user_id)
    if link is None:
        await answer("This Telegram account is not linked to Lynewright.", alert=True)
        return {"ok": True, "rejected": "unlinked"}
    try:
        action = resolve_action_from_channel(
            action_id=action_id,
            decision=decision,
            link_tenant_id=str(link["tenant_id"]),
            link_id=str(link["id"]),
        )
    except ActionAlreadyResolvedError:
        await answer("That decision was already recorded.")
        return {"ok": True, "duplicate": True}
    except PermissionError:
        await answer("You are not allowed to act on this item.", alert=True)
        return {"ok": True, "rejected": "cross_tenant"}
    except ActionError as exc:
        await answer(str(exc), alert=True)
        return {"ok": True, "rejected": "invalid"}
    labels = {
        "approve": "Approved",
        "reject": "Refused",
        "request_changes": "Changes requested",
    }
    confirmation = labels[decision]
    await answer(confirmation)
    # Best-effort prompt edit so stale buttons disappear.
    note = str(action.get("resolution_note") or "")
    prompt_ref = note.removeprefix("prompt:") if note.startswith("prompt:") else ""
    if prompt_ref:
        registry = default_registry()
        adapter = registry.get(CHANNEL_TYPE)
        try:
            await adapter.answer_action_prompt(  # type: ignore[attr-defined]
                connection=connection, prompt_ref=prompt_ref,
                outcome=f"{confirmation} via Telegram.",
            )
        except Exception:
            pass
    log_event(
        level="info", category="interaction", source="telegram_webhook",
        message=f"Action {action_id} resolved via Telegram: {decision}.",
        tenant_id=action.get("tenant_id"),
        details={"event": "interaction.action.resolved", "action_id": action_id,
                 "decision": decision, "payload_ref": str(action.get("payload_ref") or "")},
    )
    return {"ok": True, "resolved": True, "decision": decision, "action_id": action_id}


# --------------------------------------------------------------- image review

async def _set_buttons(connection: dict[str, Any], chat_ref: str, message_id: str,
                       buttons: tuple[tuple[str, str], ...]) -> None:
    """Best-effort: swap the buttons under a photo or message."""

    try:
        adapter = default_registry().get(CHANNEL_TYPE)
        client = adapter.client_for(connection)  # type: ignore[attr-defined]
        keyboard = adapter._inline_keyboard(buttons) if buttons else None  # type: ignore[attr-defined]
        await client.edit_message_reply_markup(chat_ref, message_id, inline_keyboard=keyboard)
    except Exception:
        pass


async def _refresh_approve_message(connection: dict[str, Any], session: dict[str, Any],
                                   text: str | None = None, *, final: bool = False) -> None:
    if not session.get("approve_message_id"):
        return
    from app.services.interaction_channels import OutboundMessage

    try:
        adapter = default_registry().get(CHANNEL_TYPE)
        await adapter.send_message(  # type: ignore[attr-defined]
            connection=connection, chat_ref=str(session["chat_ref"]),
            message=OutboundMessage(
                text=text or image_review_channel._summary_text(session),
                buttons=() if final else (image_review_channel._approve_button(session),),
                edit_external_message_id=str(session["approve_message_id"]),
            ),
        )
    except Exception:
        pass


async def _handle_image_review_callback(
    *, connection: dict[str, Any], query: dict[str, Any], data: str,
    external_user_id: str, answer: Any,
) -> dict[str, Any]:
    link = find_active_link(connection_id=str(connection["id"]), external_user_id=external_user_id)
    if link is None:
        await answer("This Telegram account is not linked to Lynewright.", alert=True)
        return {"ok": True, "rejected": "unlinked"}
    parts = data[len(image_review_channel.CALLBACK_PREFIX):].split(":")
    try:
        if parts[0] == "r" and len(parts) == 3:
            changed = image_review_channel.toggle_refusal(session_id=parts[1], index=int(parts[2]), link=link)
            session, item = changed["session"], changed["item"]
            await _set_buttons(connection, str(session["chat_ref"]), str(item["message_id"]),
                               image_review_channel._item_buttons(session, changed["index"], item))
            await _refresh_approve_message(connection, session)
            if item["refused"]:
                await answer(f"{item['label']} refused.")
                await _reply(connection=connection, chat_ref=str(session["chat_ref"]),
                             text=f"Why refuse {item['label']}? Send what should change as your next message.")
            else:
                await answer(f"{item['label']} is back in the approved set.")
            return {"ok": True, "handled": "image_review_refusal", "refused": item["refused"]}
        if parts[0] == "a" and len(parts) == 2:
            done = image_review_channel.approve(session_id=parts[1], link=link)
            session = done["session"]
            await answer("Review recorded.")
            for item in session["items"]:
                if item.get("message_id"):
                    await _set_buttons(connection, str(session["chat_ref"]), str(item["message_id"]), ())
            await _refresh_approve_message(
                connection, session, image_review_channel.submitted_text(done["result"]), final=True,
            )
            return {"ok": True, "handled": "image_review_submitted"}

    except PermissionError:
        await answer("You are not allowed to act on this item.", alert=True)
        return {"ok": True, "rejected": "cross_tenant"}
    except (image_review_channel.ImageReviewError, ValueError) as exc:
        await answer(str(exc)[:190], alert=True)
        return {"ok": True, "rejected": "invalid"}
    except Exception as exc:  # the record operation itself failed
        log_event(level="error", category="interaction", source="image_review",
                  message="Recording a Telegram image review failed.", tenant_id=link.get("tenant_id"),
                  details={"detail": f"{type(exc).__name__}: {exc}"[:300]})
        await answer("The review could not be recorded. Nothing was saved; try again.", alert=True)
        return {"ok": True, "rejected": "record_failed"}
    await answer("Unrecognized action.", alert=True)
    return {"ok": True, "ignored": "foreign_callback"}


# --------------------------------------------------------------------- choice

async def _refresh_choice_message(connection: dict[str, Any], session: dict[str, Any], text: str) -> None:
    if not session.get("message_id"):
        return
    from app.services.interaction_channels import OutboundMessage

    try:
        adapter = default_registry().get(CHANNEL_TYPE)
        await adapter.send_message(  # type: ignore[attr-defined]
            connection=connection, chat_ref=str(session["chat_ref"]),
            message=OutboundMessage(text=text, buttons=(), edit_external_message_id=str(session["message_id"])),
        )
    except Exception:
        pass


async def _handle_choice_callback(
    *, connection: dict[str, Any], query: dict[str, Any], data: str,
    external_user_id: str, answer: Any,
) -> dict[str, Any]:
    link = find_active_link(connection_id=str(connection["id"]), external_user_id=external_user_id)
    if link is None:
        await answer("This Telegram account is not linked to Lynewright.", alert=True)
        return {"ok": True, "rejected": "unlinked"}
    parts = data[len(choice_channel.CALLBACK_PREFIX):].split(":")
    try:
        if parts[0] == "p" and len(parts) == 3:
            done = choice_channel.pick(session_id=parts[1], index=int(parts[2]), link=link)
            session = done["session"]
            await answer("Choice recorded.")
            await _refresh_choice_message(connection, session, choice_channel.picked_text(done["result"]))
            return {"ok": True, "handled": "choice_picked"}
        if parts[0] == "n" and len(parts) == 2:
            done = choice_channel.none(session_id=parts[1], link=link)
            session = done["session"]
            await answer("Choice recorded.")
            await _refresh_choice_message(connection, session, choice_channel.picked_text(done["result"]))
            return {"ok": True, "handled": "choice_none"}
    except PermissionError:
        await answer("You are not allowed to act on this item.", alert=True)
        return {"ok": True, "rejected": "cross_tenant"}
    except (choice_channel.ChoiceError, ValueError) as exc:
        await answer(str(exc)[:190], alert=True)
        return {"ok": True, "rejected": "invalid"}
    except Exception as exc:  # the submit operation itself failed
        log_event(level="error", category="interaction", source="choice_channel",
                  message="Recording a Telegram choice failed.", tenant_id=link.get("tenant_id"),
                  details={"detail": f"{type(exc).__name__}: {exc}"[:300]})
        await answer("The choice could not be recorded. Nothing was saved; try again.", alert=True)
        return {"ok": True, "rejected": "record_failed"}
    await answer("Unrecognized action.", alert=True)
    return {"ok": True, "ignored": "foreign_callback"}


# --------------------------------------------------------------------- sends

async def _reply(*, connection: dict[str, Any], chat_ref: str, text: str) -> None:
    try:
        registry = default_registry()
        adapter = registry.get(CHANNEL_TYPE)
        from app.services.interaction_channels import OutboundMessage

        await adapter.send_message(  # type: ignore[attr-defined]
            connection=connection, chat_ref=chat_ref, message=OutboundMessage(text=text),
        )
    except Exception:
        pass  # acknowledgement failures must not fail the webhook


def _inbound_media_error(exc: Exception) -> dict[str, Any]:
    return {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"[:300]}


async def _store_inbound_media(
    *,
    connection: dict[str, Any],
    project_id: str,
    file_payload: dict[str, Any],
    default_name: str,
    default_mime: str,
):
    """Persist inbound Telegram bytes as durable Lynewright media.

    Downloads come only from the pinned Bot API host (see telegram_client);
    nothing provider-supplied is ever fetched directly.
    """

    from app.services.interaction_channels import InboundMediaRef

    file_id = str(file_payload.get("file_id") or "")
    if not file_id:
        return None
    declared_size = int(file_payload.get("file_size") or 0)
    if declared_size > _MAX_INBOUND_MEDIA_BYTES:
        log_event(
            level="warning", category="interaction", source="telegram_webhook",
            message="Skipped oversized inbound Telegram media.",
            tenant_id=connection.get("tenant_id"),
            details={"declared_size": declared_size},
        )
        return None
    registry = default_registry()
    adapter = registry.get(CHANNEL_TYPE)
    try:
        data, meta = await adapter.fetch_media(  # type: ignore[attr-defined]
            connection=connection, file_id=file_id, max_bytes=_MAX_INBOUND_MEDIA_BYTES,
        )
    except Exception as exc:
        log_event(
            level="warning", category="interaction", source="telegram_webhook",
            message="Inbound Telegram media could not be fetched.",
            tenant_id=connection.get("tenant_id"),
            details=_inbound_media_error(exc),
        )
        return None
    extension = Path(meta.get("file_path", "")).suffix[:10]
    safe_ext = extension if all(ch.isalnum() for ch in extension.lstrip(".")) else ""
    stored_as = f"tg_{secrets.token_hex(12)}{safe_ext}.stored"
    root = managed_media_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / stored_as).write_bytes(data)
    media = MediaService(tenant_id=str(connection["tenant_id"]))
    row = media.create_managed(
        name=default_name[:255],
        content_type=str(file_payload.get("mime_type") or default_mime)[:120],
        metadata={
            "stored_as": stored_as,
            "source": "telegram",
            "source_file_id": file_id,
            "bytes": len(data),
        },
        project_id=project_id,
    )
    return InboundMediaRef(
        media_id=str(row.get("id")),
        name=default_name[:200],
        content_type=str(row.get("content_type") or default_mime),
        size_bytes=len(data),
    )


# ------------------------------------------------------------------ utilities

def describe_webhook_endpoint(*, connection: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    return {
        "url_hint": f"{settings.public_url}/api/interactions/telegram/{connection['id']}/webhook",
        "secret_header": TELEGRAM_SECRET_HEADER,
        "enabled_channel": bool(settings.telegram_enabled),
        "max_body_bytes": settings.interaction_webhook_max_body_bytes,
    }
