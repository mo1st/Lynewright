"""Channel-level approval/action transport.

This module is deliberately *transport only*: it moves an approval request to a
human through a channel and carries the decision back. Business approval rules
and the authoritative approval record belong to Lynewright's core systems
(``payload_ref`` is an opaque reference into whichever system created the
request). Nothing here decides whether an action is allowed — only whether the
transport delivered and resolved it.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from typing import Any

from app.config import get_settings
from app.database import get_connection, init_db
from app.services.interaction_channels import OutboundMessage, default_registry
from app.services.interaction_conversations import record_message, require_tenant_conversation
from app.services.interaction_schema import init_interaction_schema

ACTION_TYPES = ("approval.request",)

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISION_REQUEST_CHANGES = "request_changes"
DECISIONS = (DECISION_APPROVE, DECISION_REJECT, DECISION_REQUEST_CHANGES)
_STATUS_FOR_DECISION = {
    DECISION_APPROVE: "approved",
    DECISION_REJECT: "rejected",
    DECISION_REQUEST_CHANGES: "changes_requested",
}

# Telegram callback_data is capped at 64 bytes; this encoding stays far below.
_CALLBACK_PREFIX = "q"


class ActionError(ValueError):
    pass


class ActionAlreadyResolvedError(ActionError):
    """The action was already decided; replays are ignored."""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def encode_callback_data(action_id: str, decision: str) -> str:
    if decision not in DECISIONS:
        raise ActionError(f"Unsupported decision: {decision}")
    data = f"{_CALLBACK_PREFIX}:{action_id}:{decision}"
    if len(data) > 64:
        raise ActionError("Callback payload exceeds the channel limit.")
    return data


def decode_callback_data(data: str) -> tuple[str, str] | None:
    """Return (action_id, decision), or None for foreign/unparseable data."""

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != _CALLBACK_PREFIX:
        return None
    action_id, decision = parts[1], parts[2]
    if not action_id or decision not in DECISIONS:
        return None
    return action_id, decision


def _serialize_action(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    try:
        item["decision_options"] = json.loads(
            str(item.pop("decision_options_json", "[]") or "[]")
        )
    except ValueError:
        item["decision_options"] = []
    return item


def create_action(
    *,
    tenant_id: str,
    project_id: str,
    conversation_id: str,
    payload_ref: str = "",
    summary: str = "",
    action_type: str = "approval.request",
    decision_options: list[str] | tuple[str, ...] | None = None,
    run_id: str | None = None,
    ttl_seconds: int | None = None,
    now_epoch: float | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Record a pending channel approval request.

    ``payload_ref`` points at the authoritative business approval; this row is
    only its transport envelope.
    """

    init_db()
    import time as _time

    if action_type not in ACTION_TYPES:
        raise ActionError(f"Unsupported action type: {action_type}")
    options = list(decision_options) if decision_options else [
        DECISION_APPROVE, DECISION_REJECT, DECISION_REQUEST_CHANGES
    ]
    for option in options:
        if option not in DECISIONS:
            raise ActionError(f"Unsupported decision option: {option}")
    conversation = require_tenant_conversation(tenant_id=tenant_id, conversation_id=conversation_id)
    settings = get_settings()
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.interaction_action_ttl_seconds)
    current_time = clock or _time.time
    expires_at = float(current_time() if now_epoch is None else now_epoch) + ttl
    action_id = _new_id("iax")
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        db.execute(
            """INSERT INTO interaction_actions
               (id,tenant_id,project_id,conversation_id,run_id,action_type,
                decision_options_json,payload_ref,summary,status,expires_at_epoch)
               VALUES(?,?,?,?,?,?,?,?,?,'pending',?)""",
            (
                action_id, tenant_id, project_id, conversation["id"], run_id, action_type,
                json.dumps(options), payload_ref[:500], summary[:2000], expires_at,
            ),
        )
        row = db.execute("SELECT * FROM interaction_actions WHERE id=?", (action_id,)).fetchone()
        return _serialize_action(row)


def get_action(*, tenant_id: str, action_id: str) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        row = db.execute(
            "SELECT * FROM interaction_actions WHERE id=? AND tenant_id=?",
            (action_id, tenant_id),
        ).fetchone()
    return dict(row) if row else None


def list_actions(
    *,
    tenant_id: str,
    conversation_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    init_db()
    where = ["tenant_id=?"]
    params: list[Any] = [tenant_id]
    if conversation_id:
        require_tenant_conversation(tenant_id=tenant_id, conversation_id=conversation_id)
        where.append("conversation_id=?")
        params.append(conversation_id)
    if status:
        where.append("status=?")
        params.append(status)
    params.append(max(1, min(int(limit), 500)))
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(
            f"SELECT * FROM interaction_actions WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [_serialize_action(row) for row in rows]


def buttons_for(action: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Render the neutral button set a channel adapter should present."""

    labels = {
        DECISION_APPROVE: "Approve",
        # Keep the wire decision as ``reject`` for backwards compatibility,
        # but make the destructive choice unambiguous to a human reviewer.
        DECISION_REJECT: "Refuse",
        DECISION_REQUEST_CHANGES: "Request changes",
    }
    return tuple(
        (labels[option], encode_callback_data(str(action["id"]), option))
        for option in action.get("decision_options", [])
    )


def find_pending_action_for_run(
    *, tenant_id: str, conversation_id: str, run_id: str,
) -> dict[str, Any] | None:
    """Return the transport envelope already waiting for one Operator run.

    Dispatch can be retried after Telegram is temporarily unavailable.  The
    authoritative Operator approval must not acquire a second pair of buttons
    on every retry, so the envelope is reused when its payload points at the
    same run.
    """

    for action in list_actions(
        tenant_id=tenant_id, conversation_id=conversation_id, status="pending", limit=100,
    ):
        try:
            payload = json.loads(str(action.get("payload_ref") or "{}"))
        except (TypeError, ValueError):
            continue
        if str(payload.get("run_id") or "") == str(run_id):
            return action
    return None


def find_pending_action_for_approval(
    *, tenant_id: str, conversation_id: str, run_id: str, approval_id: str,
) -> dict[str, Any] | None:
    """Find the one channel envelope bound to a specific operator approval.

    A run can pass through several approval boundaries. Looking up only by
    ``run_id`` would reuse the first prompt and leave later approvals without
    buttons, which is the failure mode this function prevents.
    """
    for action in list_actions(
        tenant_id=tenant_id, conversation_id=conversation_id, status="pending", limit=100,
    ):
        try:
            payload = json.loads(str(action.get("payload_ref") or "{}"))
        except (TypeError, ValueError):
            continue
        if (
            str(payload.get("run_id") or "") == str(run_id)
            and str(payload.get("approval_id") or "") == str(approval_id)
        ):
            return action
    return None


def bind_action_prompt(*, tenant_id: str, action_id: str, prompt_ref: str) -> bool:
    """Link the sent Telegram message to a pending action for later editing."""

    if not prompt_ref or len(prompt_ref) > 500:
        raise ActionError("An action prompt reference is invalid.")
    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        updated = db.execute(
            "UPDATE interaction_actions SET resolution_note=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND tenant_id=? AND status='pending' AND resolution_note=''",
            (f"prompt:{prompt_ref}", action_id, tenant_id),
        ).rowcount
    return updated == 1


def restrict_pending_action_to_approve_refuse(*, tenant_id: str, action_id: str) -> bool:
    """Migrate an older pending envelope to the current two-choice UI."""

    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        updated = db.execute(
            "UPDATE interaction_actions SET decision_options_json=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND tenant_id=? AND status='pending'",
            (json.dumps([DECISION_APPROVE, DECISION_REJECT]), action_id, tenant_id),
        ).rowcount
    return updated == 1


async def deliver_action(
    *,
    connection: dict[str, Any],
    action: dict[str, Any],
    intro_text: str = "",
) -> dict[str, Any]:
    """Send an action's buttons to its conversation via the channel adapter.

    Records the outbound prompt message and links it to the action so the
    prompt can be edited after resolution.
    """

    from app.services.interaction_conversations import get_conversation

    registry = default_registry()
    adapter = registry.get(connection["channel_type"])
    conversation = get_conversation(tenant_id=connection["tenant_id"], conversation_id=action["conversation_id"])
    if conversation is None:
        raise LookupError("Action conversation not found.")
    text = "\n".join(part for part in [intro_text, str(action.get("summary") or "")] if part)
    result = await adapter.send_message(  # type: ignore[attr-defined]
        connection=connection,
        chat_ref=str(conversation["external_chat_id"]),
        message=OutboundMessage(text=text or "Approval requested.", buttons=buttons_for(action)),
    )
    prompt_ref = (
        f"{conversation['external_chat_id']}:{result.external_message_id}" if result.ok else ""
    )
    message_row = record_message(
        conversation=conversation,
        direction="outbound",
        actor="system",
        message_type="approval_request",
        text=text or "Approval requested.",
        action_id=str(action["id"]),
        external_message_id=result.external_message_id,
        status="sent" if result.ok else "failed",
        detail=result.detail,
    )
    if prompt_ref:
        with closing(get_connection()) as db, db:
            init_interaction_schema(db)
            db.execute(
                "UPDATE interaction_actions SET resolution_note=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND resolution_note=''",
                (f"prompt:{prompt_ref}", action["id"]),
            )
    return {"action": action, "message": message_row, "send_result": {
        "ok": result.ok, "detail": result.detail, "external_message_id": result.external_message_id,
    }}


def resolve_action_from_channel(
    *,
    action_id: str,
    decision: str,
    link_tenant_id: str,
    link_id: str,
    note: str = "",
    now: Any = None,
) -> dict[str, Any]:
    """Resolve a pending action from a channel callback.

    Authorization model: the callback presenter must hold an *active link in
    the same tenant* as the action. Cross-tenant callbacks are refused without
    disclosing the action's existence. Replay-safe: only one transition out of
    'pending' ever succeeds.
    """

    import time as _time

    if decision not in DECISIONS:
        raise ActionError(f"Unsupported decision: {decision}")
    init_db()
    current_time = now or _time.time
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        action = db.execute(
            "SELECT * FROM interaction_actions WHERE id=?", (action_id,)
        ).fetchone()
        if action is None:
            raise ActionError("Action not found.")
        if str(action["tenant_id"]) != link_tenant_id:
            raise PermissionError("This action does not belong to your workspace.")
        if action["status"] != "pending":
            raise ActionAlreadyResolvedError("This action was already resolved.")
        expires = action["expires_at_epoch"]
        if expires is not None and current_time() >= float(expires):
            db.execute(
                "UPDATE interaction_actions SET status='expired', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='pending'",
                (action_id,),
            )
            raise ActionError("This action has expired.")
        updated = db.execute(
            "UPDATE interaction_actions SET status=?, resolved_by=?, resolved_via='telegram', "
            "resolution_note=CASE WHEN ?='' THEN resolution_note ELSE ? END, resolved_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
            (_STATUS_FOR_DECISION[decision], link_id[:120], note, note[:500], action_id),
        ).rowcount
        if updated != 1:
            raise ActionAlreadyResolvedError("This action was already resolved.")
        fresh = db.execute(
            "SELECT * FROM interaction_actions WHERE id=?", (action_id,)
        ).fetchone()
        return _serialize_action(fresh)


def resolve_action_via_api(
    *, tenant_id: str, action_id: str, decision: str, resolved_by: str, note: str = ""
) -> dict[str, Any]:
    """Business-system-side resolution (the business system stays authoritative)."""

    if decision not in DECISIONS:
        raise ActionError(f"Unsupported decision: {decision}")
    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        action = db.execute(
            "SELECT * FROM interaction_actions WHERE id=? AND tenant_id=?",
            (action_id, tenant_id),
        ).fetchone()
        if action is None:
            raise ActionError("Action not found.")
        if action["status"] != "pending":
            raise ActionAlreadyResolvedError("This action was already resolved.")
        updated = db.execute(
            "UPDATE interaction_actions SET status=?, resolved_by=?, resolved_via='api', "
            "resolution_note=?, resolved_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='pending'",
            (_STATUS_FOR_DECISION[decision], resolved_by[:120], note[:500], action_id),
        ).rowcount
        if updated != 1:
            raise ActionAlreadyResolvedError("This action was already resolved.")
        fresh = db.execute(
            "SELECT * FROM interaction_actions WHERE id=?", (action_id,)
        ).fetchone()
        return _serialize_action(fresh)


def cancel_action(*, tenant_id: str, action_id: str, reason: str = "") -> dict[str, Any]:
    """Withdraw a pending action (e.g. the underlying approval was withdrawn)."""

    init_db()
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        action = db.execute(
            "SELECT * FROM interaction_actions WHERE id=? AND tenant_id=?",
            (action_id, tenant_id),
        ).fetchone()
        if action is None:
            raise ActionError("Action not found.")
        if action["status"] != "pending":
            raise ActionAlreadyResolvedError("This action was already resolved.")
        db.execute(
            "UPDATE interaction_actions SET status='cancelled', resolved_by='system', "
            "resolved_via='api', resolution_note=?, resolved_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
            (reason[:500], action_id),
        )
        fresh = db.execute("SELECT * FROM interaction_actions WHERE id=?", (action_id,)).fetchone()
        return _serialize_action(fresh)


def expire_due_actions(now: Any = None) -> int:
    """Mark lapsed pending actions expired. Callable from any maintenance path."""

    import time as _time

    init_db()
    current_time = now or _time.time
    with closing(get_connection()) as db, db:
        cursor = db.execute(
            "UPDATE interaction_actions SET status='expired', updated_at=CURRENT_TIMESTAMP "
            "WHERE status='pending' AND expires_at_epoch IS NOT NULL AND expires_at_epoch <= ?",
            (current_time(),),
        )
        return cursor.rowcount
