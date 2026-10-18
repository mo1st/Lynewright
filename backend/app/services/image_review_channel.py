"""Review a set of images in Telegram: refuse any with a reason, approve the rest at once.

The flow a reviewer sees:

  * every image arrives as its own photo, captioned with what it is for (for a
    item context and title), with a "Refuse this image" button under it;
  * tapping Refuse asks for the reason; the next text the reviewer sends in that
    chat is the reason (tapping again undoes the refusal);
  * one final message carries a single button -- "Approve all 9" or
    "Approve 8, refuse 1" -- that records the whole set in one decision.

This module is transport and state handling only. It never decides what a review
means: the requester names a contract operation, and on approval that operation
is invoked with the approved keys and the refusals. Core never imports the
integration that asked for the review.

Sessions live in one small table created here, following the interaction
schema's own pattern (no SQL migration).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

from app.database import get_connection, init_db
from app.services.application_logging import log_event

CALLBACK_PREFIX = "v:"
MAX_ITEMS = 50

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS image_review_sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    connection_id TEXT NOT NULL DEFAULT '',
    chat_ref TEXT NOT NULL DEFAULT '',
    set_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    items_json TEXT NOT NULL,
    submit_json TEXT NOT NULL,
    awaiting_index INTEGER,
    approve_message_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'sending',
    result_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_image_review_sessions_chat
    ON image_review_sessions(connection_id, chat_ref, status);
CREATE INDEX IF NOT EXISTS idx_image_review_sessions_set
    ON image_review_sessions(tenant_id, project_id, set_key, status);
CREATE TABLE IF NOT EXISTS image_review_upload_choices (
    token TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    chat_ref TEXT NOT NULL,
    media_json TEXT NOT NULL,
    options_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_image_review_upload_choices_chat
    ON image_review_upload_choices(connection_id, chat_ref, status);
"""

# A session is "live" while the reviewer can still act on it, or while a
# sender is actively delivering it (Phase 26: request_review's own dedupe
# check must never create a second session while one is mid-send).
_LIVE = ("sending", "open", "delivering", "upload_sending", "upload_delivering", "awaiting_upload")

# A session stuck "delivering" for longer than this means its sender died
# mid-send; it may be reclaimed and resumed (Phase 26).
_STALE_DELIVERY_MINUTES = 2


class ImageReviewError(ValueError):
    pass


def _db():
    init_db()
    db = get_connection()
    db.executescript(_SCHEMA_SQL)
    return db


def _load(row: Any) -> dict[str, Any]:
    session = dict(row)
    session["items"] = json.loads(session.pop("items_json") or "[]")
    session["submit"] = json.loads(session.pop("submit_json") or "{}")
    return session


def _save_items(db: Any, session_id: str, items: list[dict[str, Any]], **fields: Any) -> None:
    assignments = ", ".join(f"{name}=?" for name in fields)
    db.execute(
        f"UPDATE image_review_sessions SET items_json=?, {assignments + ', ' if assignments else ''}"
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (json.dumps(items), *fields.values(), session_id),
    )


def get_session(session_id: str) -> dict[str, Any] | None:
    with closing(_db()) as db:
        row = db.execute("SELECT * FROM image_review_sessions WHERE id=?", (session_id,)).fetchone()
        return _load(row) if row else None


# ------------------------------------------------------------------ labels

def approve_label(items: list[dict[str, Any]]) -> str:
    refused = sum(1 for item in items if item.get("refused"))
    approved = len(items) - refused
    if not refused:
        return f"Approve all {approved}" if approved != 1 else "Approve the image"
    return f"Approve {approved}, refuse {refused}"


def _summary_text(session: dict[str, Any]) -> str:
    items = session["items"]
    lines = [f"Review {len(items)} image{'s' if len(items) != 1 else ''}"
             + (f" for {session['title']}" if session.get("title") else "") + "."]
    rules = session["submit"].get("rules") or []
    if rules:
        lines.append("Rules every picture must follow:")
        lines += [f"• {rule}" for rule in rules]
    intro = str(session["submit"].get("intro") or "").strip()
    if intro:
        lines.append(intro)             # the line's own language: does the image match its description? (Phase 36)
    lines.append(
        "Look closely at each picture: hands, legs, fingers and faces (the right number, the right shape) "
        "and objects in a sensible place."
    )
    lines.append("Tap \"Refuse this image\" under any picture that needs fixing, then approve the rest here.")
    refused = [item for item in items if item.get("refused")]
    for item in refused:
        reason = item.get("reason") or "(waiting for your reason)"
        lines.append(f"• {item['label']} refused: {reason}")
    return "\n".join(lines)


def _refuse_button(session_id: str, index: int, refused: bool) -> tuple[str, str]:
    label = "Undo refusal" if refused else "Refuse this image"
    return (label, f"{CALLBACK_PREFIX}r:{session_id}:{index}")


def _approve_button(session: dict[str, Any]) -> tuple[str, str]:
    return (approve_label(session["items"]), f"{CALLBACK_PREFIX}a:{session['id']}")


def _prompt_button(session_id: str, index: int) -> tuple[str, str]:
    return ("Prompt", f"{CALLBACK_PREFIX}p:{session_id}:{index}")


def _item_buttons(session: dict[str, Any], index: int, item: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    buttons: list[tuple[str, str]] = []
    if session.get("status") in {"sending", "open", "delivering"}:
        buttons.append(_refuse_button(str(session["id"]), index, bool(item.get("refused"))))
    if str(item.get("prompt") or "").strip():
        buttons.append(_prompt_button(str(session["id"]), index))
    return tuple(buttons)


# ------------------------------------------------------------------ request

def request_review(
    *,
    tenant_id: str,
    project_id: str,
    items: list[dict[str, Any]],
    submit: dict[str, Any],
    title: str = "",
    background: bool = True,
) -> dict[str, Any]:
    """Send ``items`` to the project's linked Telegram chat for review.

    ``items``: [{"key": opaque id (e.g. sha256), "label": short id shown to
    the reviewer, "caption": text under the photo, "path": local image file}].
    ``submit``: {"contract", "operation", "payload", "then"?} -- invoked on
    approval with ``asset_sha256s`` (every key) and ``refusals``
    ([{asset_sha256, feedback}]) added to ``payload``. ``then``, if present, is
    a second ``{"contract", "operation", "payload"?}`` started in the
    background (best-effort) right after that invoke succeeds -- the resumed
    run continuing on, for one, rather than needing anyone to notice the
    approval and call it themselves.

    The same set is never sent twice while a review of it is still live, so a
    build that is re-run while waiting does not flood the chat.
    """

    if not items:
        raise ImageReviewError("Nothing to review.")
    if len(items) > MAX_ITEMS:
        raise ImageReviewError(f"A Telegram review covers at most {MAX_ITEMS} images.")
    for field in ("contract", "operation"):
        if not str(submit.get(field) or "").strip():
            raise ImageReviewError(f"submit.{field} is required.")
    clean = [
        {
            "key": str(item["key"]), "label": str(item.get("label") or item["key"])[:80],
            "caption": str(item.get("caption") or item.get("label") or "")[:900],
            "path": str(item["path"]), "refused": False, "reason": "", "message_id": "",
            "prompt": str(item.get("prompt") or "")[:1000],
            "asset_id": str(item.get("asset_id") or "")[:80],
            "prompt_message_id": "",
        }
        for item in items
    ]
    set_key = ",".join(sorted(item["key"] for item in clean))
    with closing(_db()) as db, db:
        live = db.execute(
            f"SELECT * FROM image_review_sessions WHERE tenant_id=? AND project_id=? AND set_key=? "
            f"AND status IN ({','.join('?' * len(_LIVE))})",
            (tenant_id, project_id, set_key, *_LIVE),
        ).fetchone()
        if live:
            return {"session_id": live["id"], "status": "already_sent"}
        session_id = f"irv_{secrets.token_hex(8)}"
        db.execute(
            "INSERT INTO image_review_sessions (id,tenant_id,project_id,set_key,title,items_json,submit_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, tenant_id, project_id, set_key, title[:200], json.dumps(clean), json.dumps(submit)),
        )
    if background:
        # Callers are synchronous (a line run); the sends are not, and must
        # never hold the render up or fail it. One lock over every send: a line
        # too big for one review is sent as several sessions started back to
        # back (Phase 16u), and their photos must arrive part by part, not
        # interleaved in the chat.
        threading.Thread(target=_send_in_turn, args=(session_id,), daemon=True).start()
        return {"session_id": session_id, "status": "sending"}
    result = _send_in_turn(session_id)
    return {"session_id": session_id, **result}








_SEND_LOCK = threading.Lock()


def _send_in_turn(session_id: str) -> dict[str, Any]:
    with _SEND_LOCK:
        return asyncio.run(send_session(session_id))


def recover_stale_deliveries(*, limit: int = 50) -> list[dict[str, Any]]:
    """Resume stale image reviews from the runtime worker's periodic sweep."""
    bounded_limit = max(1, min(int(limit), 500))
    with closing(_db()) as db:
        rows = db.execute(
            "SELECT id,status FROM image_review_sessions "
            "WHERE status IN ('sending','delivering') "
            "AND updated_at < datetime('now', ?) "
            "ORDER BY updated_at,id LIMIT ?",
            (f"-{_STALE_DELIVERY_MINUTES} minutes", bounded_limit),
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        session_id, status = str(row["id"]), str(row["status"])
        if status == "upload_delivering":
            with closing(_db()) as db, db:
                db.execute(
                    "UPDATE image_review_sessions SET status='upload_sending' WHERE id=? AND status='upload_delivering'",
                    (session_id,),
                )
        result = _send_uploads_in_turn(session_id) if status.startswith("upload_") else _send_in_turn(session_id)
        results.append({"session_id": session_id, **result})
    return results


def _telegram_target(tenant_id: str, project_id: str) -> tuple[dict[str, Any], str] | None:
    from app.services.interaction_conversations import list_conversations

    for conversation in list_conversations(tenant_id=tenant_id, project_id=project_id, channel_type="telegram"):
        if not conversation.get("external_chat_id"):
            continue
        with closing(get_connection()) as db:
            row = db.execute(
                "SELECT * FROM interaction_connections WHERE id=?", (conversation["connection_id"],)
            ).fetchone()
        if row and dict(row).get("enabled"):
            return dict(row), str(conversation["external_chat_id"])
    return None


async def send_session(session_id: str) -> dict[str, Any]:
    """Deliver a session's photos and its approve message. Idempotent per photo.

    Claims the session atomically (sending -> delivering) before sending
    anything: a second concurrent caller -- a racing background thread, a
    live-check script "making sure it is delivered" -- sees the claim fail
    and does nothing, rather than both re-sending everything (found live,
    Phase 26: interleaved senders gave one photo set two message ids each).
    A session stuck delivering for over _STALE_DELIVERY_MINUTES (its sender
    died mid-send) may be reclaimed; the existing per-photo message_id skip
    then resumes only the unsent items.
    """

    from app.services.interaction_channels import OutboundMessage, default_registry

    session = get_session(session_id)
    if session is None or session["status"] not in _LIVE:
        return {"status": "not_live"}
    with closing(_db()) as db, db:
        cursor = db.execute(
            "UPDATE image_review_sessions SET status='delivering', updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND (status='sending' OR "
            "(status='delivering' AND updated_at < datetime('now', ?)))",
            (session_id, f"-{_STALE_DELIVERY_MINUTES} minutes"),
        )
        if cursor.rowcount == 0:
            return {"status": "already_delivering"}
    try:
        target = _telegram_target(session["tenant_id"], session["project_id"])
    except Exception as exc:  # an unknown project has no chat either
        target = None
        detail = f"{type(exc).__name__}: {exc}"[:300]
    else:
        detail = "No linked Telegram chat for this project."
    if target is None:
        _finish(session_id, "no_chat", {"detail": detail})
        return {"status": "no_chat"}
    connection, chat_ref = target
    adapter = default_registry().get("telegram")
    items = session["items"]
    try:
        for index, item in enumerate(items):
            if item.get("message_id"):
                continue
            path = Path(item["path"])
            sent = await adapter.send_photo_file(  # type: ignore[attr-defined]
                connection=connection, chat_ref=chat_ref,
                path=str(path), filename=path.name,
                caption=item["caption"], buttons=_item_buttons(session, index, item),
            )
            if not sent.ok:
                raise ImageReviewError(sent.detail or "photo not delivered")
            item["message_id"] = sent.external_message_id
            with closing(_db()) as db, db:
                _save_items(db, session_id, items)
        session["items"] = items
        result = await adapter.send_message(  # type: ignore[attr-defined]
            connection=connection, chat_ref=chat_ref,
            message=OutboundMessage(text=_summary_text(session), buttons=(_approve_button(session),)),
        )
        if not result.ok:
            raise ImageReviewError(result.detail or "approve message not delivered")
    except Exception as exc:  # delivery must never fail the requester
        _finish(session_id, "send_failed", {"detail": f"{type(exc).__name__}: {exc}"[:300]})
        log_event(level="warning", category="interaction", source="image_review",
                  message="Image review could not be sent to Telegram.",
                  tenant_id=session["tenant_id"],
                  details={"session_id": session_id, "detail": str(exc)[:300]})
        return {"status": "send_failed"}
    with closing(_db()) as db, db:
        db.execute(
            "UPDATE image_review_sessions SET status='open', connection_id=?, chat_ref=?, "
            "approve_message_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(connection["id"]), chat_ref, result.external_message_id, session_id),
        )
    return {"status": "open", "session_id": session_id}






















def _finish(session_id: str, status: str, result: dict[str, Any]) -> None:
    with closing(_db()) as db, db:
        db.execute(
            "UPDATE image_review_sessions SET status=?, result_json=?, awaiting_index=NULL, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, json.dumps(result)[:4000], session_id),
        )


# ------------------------------------------------------------------ reviewer actions

def _require_open(session_id: str, link: dict[str, Any]) -> dict[str, Any]:
    session = get_session(session_id)
    if session is None:
        raise ImageReviewError("This review no longer exists.")
    if str(session["tenant_id"]) != str(link.get("tenant_id")):
        raise PermissionError("This review does not belong to your workspace.")
    if session["status"] != "open":
        raise ImageReviewError("This review was already submitted.")
    return session


def toggle_refusal(*, session_id: str, index: int, link: dict[str, Any]) -> dict[str, Any]:
    """Refuse an image (a reason is asked for next) or undo a refusal."""

    session = _require_open(session_id, link)
    items = session["items"]
    if not 0 <= index < len(items):
        raise ImageReviewError("Unknown image.")
    item = items[index]
    item["refused"] = not item["refused"]
    item["reason"] = ""
    awaiting = index if item["refused"] else (
        None if session.get("awaiting_index") == index else session.get("awaiting_index")
    )
    with closing(_db()) as db, db:
        _save_items(db, session_id, items, awaiting_index=awaiting)
    session.update(items=items, awaiting_index=awaiting)
    return {"session": session, "item": item, "index": index}


def take_reason(*, connection_id: str, chat_ref: str, link: dict[str, Any], text: str) -> dict[str, Any] | None:
    """Use this chat message as the reason for the image awaiting one, if any."""

    reason = text.strip()
    if not reason or reason.startswith("/"):
        return None
    with closing(_db()) as db:
        row = db.execute(
            "SELECT * FROM image_review_sessions WHERE connection_id=? AND chat_ref=? AND status='open' "
            "AND awaiting_index IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
            (connection_id, chat_ref),
        ).fetchone()
    if row is None or str(row["tenant_id"]) != str(link.get("tenant_id")):
        return None
    session = _load(row)
    index = int(session["awaiting_index"])
    items = session["items"]
    items[index]["reason"] = reason[:1000]
    items[index]["refused"] = True
    with closing(_db()) as db, db:
        _save_items(db, session["id"], items, awaiting_index=None)
    session.update(items=items, awaiting_index=None)
    return {"session": session, "item": items[index], "index": index}


def _reviewer_actor(link: dict[str, Any]) -> Any:
    from app.services.platform_capabilities import CapabilityActor

    who = str(link.get("external_username") or link.get("external_user_id") or link.get("id") or "")
    return CapabilityActor(
        client_id=f"telegram:{link.get('id')}",
        name=f"Telegram reviewer {who}".strip(),
        client_type="external",
        permission_mode="custom",
        granted_capabilities=frozenset({"publishing:write", "storage:read", "storage:write", "media:read"}),
    )


def approve(*, session_id: str, link: dict[str, Any], invoke: Any = None) -> dict[str, Any]:
    """Record the whole set: refused images with their reasons, every other one approved."""

    session = _require_open(session_id, link)
    items = session["items"]
    waiting = [item["label"] for item in items if item["refused"] and not item["reason"]]
    if waiting:
        raise ImageReviewError(f"Send the reason for {', '.join(waiting)} first, or undo the refusal.")
    submit = session["submit"]
    payload = {
        **(submit.get("payload") or {}),
        "asset_sha256s": [item["key"] for item in items],
        "decision": "pass",
    }
    refusals = [{"asset_sha256": item["key"], "feedback": item["reason"]} for item in items if item["refused"]]
    if refusals:
        payload["refusals"] = refusals
    custom_invoke = invoke is not None
    if invoke is None:
        from app.services.operator_dispatch import _contract_registry

        def invoke(contract: str, operation: str, body: dict[str, Any], project_id: str, actor: Any) -> Any:
            return _contract_registry().invoke(contract, operation, body, project_id=project_id, actor=actor)

    try:
        outcome = invoke(submit["contract"], submit["operation"], payload, session["project_id"], _reviewer_actor(link))
    except Exception as exc:
        if "already reviewed" in str(exc).lower():
            raise ImageReviewError("This picture was already reviewed.") from exc
        raise
    then = submit.get("then")
    if isinstance(then, dict) and then.get("line_run_id"):
        try:
            from app.services.line_engine import ProductionLineEngine

            ProductionLineEngine(tenant_id=str(session["tenant_id"])).continue_run(
                str(then["line_run_id"]), value={"review": outcome if isinstance(outcome, dict) else {}},
                actor_id=_reviewer_actor(link).client_id,
            )
        except Exception as exc:
            # The image records have already been written. Another channel
            # may have claimed the line continuation while this callback ran.
            if "already chosen" not in str(exc).lower() and "not waiting" not in str(exc).lower():
                log_event(level="error", category="integration", source="image_review",
                          message=f"Could not resume the production line after review: {exc}",
                          tenant_id=session["tenant_id"], details={"session_id": session_id})
    elif isinstance(then, dict) and then.get("contract") and then.get("operation"):
        # Best-effort: a run that cannot be resumed automatically is not an
        # approval failure -- the record above already stands.
        try:
            from app.services import line_runner

            then_payload = then.get("payload") if isinstance(then.get("payload"), dict) else {}
            then_contract = str(then["contract"])
            then_operation = str(then["operation"])
            dispatch_invoke = invoke if custom_invoke or not line_runner._worker_is_configured() else None
            line_runner.schedule_step(
                contract=then_contract, operation=then_operation, payload=then_payload,
                run_key=str(then.get("run_key") or line_runner.run_key_for(then_contract, then_operation, then_payload)),
                project_id=session["project_id"], actor=_reviewer_actor(link),
                tenant_id=str(session["tenant_id"]), resume=True,
                invoke=dispatch_invoke,
            )
        except Exception as exc:
            log_event(level="error", category="integration", source="image_review",
                      message=f"Could not resume the then operation after approval: {exc}",
                      tenant_id=session["tenant_id"], details={"session_id": session_id})
    result = {
        "approved": [item["label"] for item in items if not item["refused"]],
        "refused": [{"label": item["label"], "reason": item["reason"]} for item in items if item["refused"]],
        "reviewer": _reviewer_actor(link).name,
        "outcome": outcome if isinstance(outcome, dict) else {},
    }
    _finish(session_id, "submitted", result)
    log_event(level="info", category="interaction", source="image_review",
              message="Image review submitted from Telegram.", tenant_id=session["tenant_id"],
              details={"session_id": session_id, "approved": len(result["approved"]),
                       "refused": len(result["refused"])})
    session["status"] = "submitted"
    return {"session": session, "result": result}


def submitted_text(result: dict[str, Any]) -> str:
    lines = [f"Review recorded: {len(result['approved'])} approved, {len(result['refused'])} refused."]
    lines += [f"• {entry['label']}: {entry['reason']}" for entry in result["refused"]]
    if result["refused"]:
        lines.append("Each reason is saved with its image, for whoever makes the replacement.")
    return "\n".join(lines)
