"""Provider-neutral async operation envelopes.

Integrations may return an accepted/processing operation without exposing a
platform ``job_*`` handle.  This module turns a declarative provider contract
into a small, durable polling description while preserving the provider's
original response fields.  Nothing in here names a concrete integration.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


ACTIVE_STATES = frozenset({"accepted", "queued", "running", "processing", "pending", "in_progress"})
TERMINAL_STATES = frozenset({"completed", "succeeded", "failed", "cancelled", "canceled", "rejected"})


def _state(value: Any, *, active: set[str], terminal: set[str]) -> str:
    text = str(value or "").strip().lower()
    if text in terminal:
        return "completed" if text in {"completed", "succeeded"} else text
    if text in active:
        return "processing"
    # A provider that returns a task id but no state has accepted the work.
    return "accepted"


def decorate_async_result(
    result: Any,
    metadata: dict[str, Any] | None,
    operation: str,
    payload: dict[str, Any] | None = None,
    *,
    project_id: str | None = None,
) -> Any:
    """Attach ``async_operation`` when the provider declared one.

    The returned object keeps every original key so existing callers remain
    compatible.  A provider can opt out for a particular response by returning
    an explicit ``async_operation`` value (including ``None``).
    """
    if not isinstance(result, dict) or "async_operation" in result:
        return result
    declarations = (metadata or {}).get("async_operations") or {}
    declaration = declarations.get(operation) if isinstance(declarations, dict) else None
    if not isinstance(declaration, dict):
        # Capability aliases are common; match the terminal component too.
        if isinstance(declarations, dict):
            declaration = declarations.get(str(operation).split(".")[-1])
    if not isinstance(declaration, dict):
        return result
    task_field = str(declaration.get("task_id_field") or "id")
    task_id = str(result.get(task_field) or "").strip()
    if not task_id:
        return result
    state_field = str(declaration.get("state_field") or "state")
    active = {str(item).strip().lower() for item in declaration.get("active_states", ACTIVE_STATES)}
    terminal = {str(item).strip().lower() for item in declaration.get("terminal_states", TERMINAL_STATES)}
    state = _state(result.get(state_field) or result.get("status"), active=active, terminal=terminal)
    poll_tool = str(declaration.get("poll_tool") or "").strip()
    if not poll_tool:
        return result
    poll_argument = str(declaration.get("poll_argument") or task_field)
    poll_args = {poll_argument: task_id}
    # Business payloads intentionally exclude host scope fields before they
    # reach providers.  The registry still owns the authoritative project
    # scope, so carry it explicitly into the durable poll descriptor instead
    # of relying on a provider payload to echo it back.
    scoped_project_id = str(project_id or "").strip()
    if not scoped_project_id and isinstance(payload, dict):
        scoped_project_id = str(payload.get("project_id") or "").strip()
    if scoped_project_id:
        poll_args["project_id"] = scoped_project_id
    # The first poll is deliberately delayed very slightly.  This avoids a
    # hot loop when an interaction reconciler ticks several times per second.
    next_poll_at = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    enriched = dict(result)
    enriched["async_operation"] = {
        "execution_id": str(result.get("execution_id") or f"provider:{task_id}"),
        "task_id": task_id,
        "state": state,
        "terminal": state in {"completed", "succeeded", "failed", "cancelled", "canceled", "rejected"},
        "poll_tool": poll_tool,
        "poll_arguments": poll_args,
        "next_poll_at": next_poll_at,
    }
    return enriched


def is_terminal_async(value: dict[str, Any]) -> bool:
    operation = value.get("async_operation")
    return isinstance(operation, dict) and bool(operation.get("terminal"))


__all__ = ["ACTIVE_STATES", "TERMINAL_STATES", "decorate_async_result", "is_terminal_async"]
