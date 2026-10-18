"""Pick one of N options in Telegram: the same review chat, a different question.

The flow a user sees:

  * one message: the caller's own text (a list, a summary, whatever the
    caller wants explained), with one button per option plus, when the
    caller allows it, "None of these";
  * tapping an option invokes the caller's named contract operation with
    that option's key, then a best-effort "then" follow-up;
  * a second tap on an already-decided choice answers "That choice was
    already recorded."

This module is transport and state handling only, exactly like
``image_review_channel``: the requester names a contract operation, and on
a pick that operation is invoked with the chosen key. Core never imports the
integration that asked for the choice.

Sessions live in one small table created here, following the interaction
schema's own pattern (no SQL migration) -- the same idiom as
``image_review_sessions``.
"""

from __future__ import annotations

import json
import secrets
import threading
import asyncio
from contextlib import closing
from typing import Any

from app.database import get_connection, init_db
from app.services.application_logging import log_event

CALLBACK_PREFIX = "c:"
MAX_OPTIONS = 8

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS choice_sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    connection_id TEXT NOT NULL DEFAULT '',
    chat_ref TEXT NOT NULL DEFAULT '',
    set_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL,
    submit_json TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'sending',
    result_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_choice_sessions_chat
    ON choice_sessions(connection_id, chat_ref, status);
CREATE INDEX IF NOT EXISTS idx_choice_sessions_set
    ON choice_sessions(tenant_id, project_id, set_key, status);
"""

# A session is "live" while a tap can still act on it, or while a sender is
# actively delivering it (Phase 26: request_choice's own dedupe check must
# never create a second session while one is mid-send).
_LIVE = ("sending", "open", "delivering")

# A session stuck "delivering" for longer than this means its sender died
# mid-send; it may be reclaimed (Phase 26).
_STALE_DELIVERY_MINUTES = 2


class ChoiceError(ValueError):
    pass


def _db():
    init_db()
    db = get_connection()
    db.executescript(_SCHEMA_SQL)
    return db


def _load(row: Any) -> dict[str, Any]:
    session = dict(row)
    session["options"] = json.loads(session.pop("options_json") or "[]")
    session["submit"] = json.loads(session.pop("submit_json") or "{}")
    return session


def get_session(session_id: str) -> dict[str, Any] | None:
    with closing(_db()) as db:
        row = db.execute("SELECT * FROM choice_sessions WHERE id=?", (session_id,)).fetchone()
        return _load(row) if row else None


def is_choice_reply(*, connection_id: str, chat_ref: str, message_id: str) -> dict[str, Any] | None:
    """Return the choice a Telegram message replies to, if it is a choice message.

    Choice prompts are completed with their buttons. A free-text reply to one
    must never become a new operator instruction, including after the button
    has already been used.
    """
    if not connection_id or not chat_ref or not message_id:
        return None
    with closing(_db()) as db:
        row = db.execute(
            "SELECT id,tenant_id,project_id,title,status FROM choice_sessions "
            "WHERE connection_id=? AND chat_ref=? AND message_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (connection_id, chat_ref, message_id),
        ).fetchone()
    return dict(row) if row else None


def _pick_button(session_id: str, index: int, label: str) -> tuple[str, str]:
    return (label[:64], f"{CALLBACK_PREFIX}p:{session_id}:{index}")


def _none_button(session_id: str) -> tuple[str, str]:
    return ("None of these", f"{CALLBACK_PREFIX}n:{session_id}")


def request_choice(
    *,
    tenant_id: str,
    project_id: str,
    set_key: str,
    title: str = "",
    text: str,
    options: list[dict[str, Any]],
    submit: dict[str, Any],
    allow_none: bool = True,
    background: bool = True,
) -> dict[str, Any]:
    """Send one message with a button per option to the project's linked Telegram chat.

    ``options``: [{"key": opaque value passed back on pick, "label": button
    text}], at most ``MAX_OPTIONS``. ``submit``: {"contract", "operation",
    "payload", "then"?} -- invoked on a pick with ``choice_key`` (the picked
    option's key, or ``None`` for "None of these") added to ``payload``.
    ``then``, if present, is a second ``{"contract", "operation", "payload"?}``
    started in the background (best-effort) right after that invoke succeeds.

    Idempotent per ``set_key`` while a choice for it is still live, exactly
    like ``image_review_channel.request_review``.
    """
    if not options:
        raise ChoiceError("Nothing to choose from.")
    if len(options) > MAX_OPTIONS:
        raise ChoiceError(f"A Telegram choice covers at most {MAX_OPTIONS} options.")
    for field in ("contract", "operation"):
        if not str(submit.get(field) or "").strip():
            raise ChoiceError(f"submit.{field} is required.")
    clean = [{"key": option["key"], "label": str(option.get("label") or "")[:80]} for option in options]
    with closing(_db()) as db, db:
        live = db.execute(
            f"SELECT * FROM choice_sessions WHERE tenant_id=? AND project_id=? AND set_key=? "
            f"AND status IN ({','.join('?' * len(_LIVE))})",
            (tenant_id, project_id, set_key, *_LIVE),
        ).fetchone()
        if live:
            return {"session_id": live["id"], "status": "already_sent"}
        session_id = f"chc_{secrets.token_hex(8)}"
        db.execute(
            "INSERT INTO choice_sessions (id,tenant_id,project_id,set_key,title,text,options_json,submit_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (session_id, tenant_id, project_id, set_key, title[:200], text, json.dumps(clean), json.dumps({**submit, "allow_none": allow_none})),
        )
    if background:
        threading.Thread(target=lambda: asyncio.run(send_session(session_id)), daemon=True).start()
        return {"session_id": session_id, "status": "sending"}
    result = asyncio.run(send_session(session_id))
    return {"session_id": session_id, **result}


def recover_stale_deliveries(*, limit: int = 50) -> list[dict[str, Any]]:
    """Resume stale operator choices from the runtime worker's periodic sweep."""
    bounded_limit = max(1, min(int(limit), 500))
    with closing(_db()) as db:
        rows = db.execute(
            "SELECT id FROM choice_sessions "
            "WHERE status IN ('sending','delivering') AND updated_at < datetime('now', ?) "
            "ORDER BY updated_at,id LIMIT ?",
            (f"-{_STALE_DELIVERY_MINUTES} minutes", bounded_limit),
        ).fetchall()
    return [
        {"session_id": str(row["id"]), **asyncio.run(send_session(str(row["id"])))}
        for row in rows
    ]


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


def _finish(session_id: str, status: str, result: dict[str, Any]) -> None:
    with closing(_db()) as db, db:
        db.execute(
            "UPDATE choice_sessions SET status=?, result_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, json.dumps(result)[:4000], session_id),
        )


async def send_session(session_id: str) -> dict[str, Any]:
    """Deliver a session's one message. Idempotent: a session already sent is left alone.

    Claims the session atomically (sending -> delivering) before sending
    anything, so a second concurrent caller does nothing rather than both
    sending the same message (Phase 26, the same fix as image_review_channel).
    """

    from app.services.interaction_channels import OutboundMessage, default_registry

    session = get_session(session_id)
    if session is None or session["status"] not in _LIVE:
        return {"status": "not_live"}
    with closing(_db()) as db, db:
        cursor = db.execute(
            "UPDATE choice_sessions SET status='delivering', updated_at=CURRENT_TIMESTAMP "
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
    buttons = [_pick_button(session_id, index, option["label"]) for index, option in enumerate(session["options"])]
    if session["submit"].get("allow_none", True):
        buttons.append(_none_button(session_id))
    try:
        result = await adapter.send_message(  # type: ignore[attr-defined]
            connection=connection, chat_ref=chat_ref,
            message=OutboundMessage(text=session["text"], buttons=tuple(buttons)),
        )
        if not result.ok:
            raise ChoiceError(result.detail or "choice message not delivered")
    except Exception as exc:  # delivery must never fail the requester
        _finish(session_id, "send_failed", {"detail": f"{type(exc).__name__}: {exc}"[:300]})
        log_event(level="warning", category="interaction", source="choice_channel",
                  message="A choice could not be sent to Telegram.",
                  tenant_id=session["tenant_id"],
                  details={"session_id": session_id, "detail": str(exc)[:300]})
        return {"status": "send_failed"}
    with closing(_db()) as db, db:
        db.execute(
            "UPDATE choice_sessions SET status='open', connection_id=?, chat_ref=?, "
            "message_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (str(connection["id"]), chat_ref, result.external_message_id, session_id),
        )
    return {"status": "open", "session_id": session_id}


def _require_open(session_id: str, link: dict[str, Any]) -> dict[str, Any]:
    session = get_session(session_id)
    if session is None:
        raise ChoiceError("This choice no longer exists.")
    if str(session["tenant_id"]) != str(link.get("tenant_id")):
        raise PermissionError("This choice does not belong to your workspace.")
    if session["status"] != "open":
        raise ChoiceError("That choice was already recorded.")
    return session


def _chooser_actor(link: dict[str, Any]) -> Any:
    from app.services.platform_capabilities import CapabilityActor

    who = str(link.get("external_username") or link.get("external_user_id") or link.get("id") or "")
    return CapabilityActor(
        client_id=f"telegram:{link.get('id')}",
        name=f"Telegram reviewer {who}".strip(),
        client_type="external",
        permission_mode="custom",
        granted_capabilities=frozenset({"publishing:write", "storage:read", "storage:write", "media:read"}),
    )


def _invoke_submit(session: dict[str, Any], *, choice_key: Any, link: dict[str, Any], invoke: Any) -> Any:
    submit = session["submit"]
    payload = {**(submit.get("payload") or {}), "choice_key": choice_key}
    line_run_id = str(payload.get("line_run_id") or "")
    if line_run_id and submit.get("contract") == "lynewright.production.lines" and submit.get("operation") == "continue":
        # The pick and dashboard continuation claim the same waiting stage.
        # ProductionLineEngine makes this a single-winner transition.
        from app.services.line_engine import ProductionLineEngine
        from app.services.platform_services import PlatformConflictError

        try:
            return ProductionLineEngine(tenant_id=str(session["tenant_id"])).continue_run(
                line_run_id, value={"choice": choice_key}, actor_id=_chooser_actor(link).client_id,
            )
        except PlatformConflictError as exc:
            raise ChoiceError("This choice was already submitted.") from exc
    custom_invoke = invoke is not None
    if invoke is None:
        from app.services.operator_dispatch import _contract_registry

        def invoke(contract: str, operation: str, body: dict[str, Any], project_id: str, actor: Any) -> Any:
            return _contract_registry().invoke(contract, operation, body, project_id=project_id, actor=actor)

    outcome = invoke(submit["contract"], submit["operation"], payload, session["project_id"], _chooser_actor(link))
    then = submit.get("then")
    if isinstance(then, dict) and then.get("contract") and then.get("operation"):
        # Best-effort: a follow-up run that cannot be resumed automatically
        # is not a pick failure -- the record above already stands.
        try:
            from app.services import line_runner

            then_payload = then.get("payload") if isinstance(then.get("payload"), dict) else {}
            then_contract = str(then["contract"])
            then_operation = str(then["operation"])
            dispatch_invoke = invoke if custom_invoke or not line_runner._worker_is_configured() else None
            line_runner.schedule_step(
                contract=then_contract, operation=then_operation, payload=then_payload,
                run_key=str(then.get("run_key") or line_runner.run_key_for(then_contract, then_operation, then_payload)),
                project_id=session["project_id"], actor=_chooser_actor(link),
                tenant_id=str(session["tenant_id"]), resume=True,
                invoke=dispatch_invoke,
            )
        except Exception as exc:
            log_event(level="error", category="integration", source="choice_channel",
                      message=f"Could not resume the then operation after a pick: {exc}",
                      tenant_id=session["tenant_id"], details={"session_id": session["id"]})
    return outcome


def pick(*, session_id: str, index: int, link: dict[str, Any], invoke: Any = None) -> dict[str, Any]:
    session = _require_open(session_id, link)
    options = session["options"]
    if not 0 <= index < len(options):
        raise ChoiceError("Unknown option.")
    option = options[index]
    outcome = _invoke_submit(session, choice_key=option["key"], link=link, invoke=invoke)
    result = {"picked": option, "reviewer": _chooser_actor(link).name, "outcome": outcome if isinstance(outcome, dict) else {}}
    _finish(session_id, "picked", result)
    log_event(level="info", category="interaction", source="choice_channel",
              message="A choice was picked from Telegram.", tenant_id=session["tenant_id"],
              details={"session_id": session_id, "label": option["label"]})
    session["status"] = "picked"
    return {"session": session, "result": result}


def none(*, session_id: str, link: dict[str, Any], invoke: Any = None) -> dict[str, Any]:
    session = _require_open(session_id, link)
    outcome = _invoke_submit(session, choice_key=None, link=link, invoke=invoke)
    result = {"picked": None, "reviewer": _chooser_actor(link).name, "outcome": outcome if isinstance(outcome, dict) else {}}
    _finish(session_id, "none", result)
    log_event(level="info", category="interaction", source="choice_channel",
              message="\"None of these\" was picked from Telegram.", tenant_id=session["tenant_id"],
              details={"session_id": session_id})
    session["status"] = "none"
    return {"session": session, "result": result}


def picked_text(result: dict[str, Any]) -> str:
    if result.get("picked") is None:
        return "Picked: None of these."
    picked = f"Picked: {result['picked']['label']}"
    outcome = result.get("outcome") if isinstance(result.get("outcome"), dict) else {}
    message = " ".join(str(outcome.get("message") or "").split())
    return f"{picked}. {message}" if message else picked
