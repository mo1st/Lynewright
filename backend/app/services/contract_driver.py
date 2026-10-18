"""Best-effort automatic driving of a stepped contract operation.

Some contract operations advance a stored run one bounded step at a time (a
whole run cannot fit inside one call's execution limit). Without this, the
caller who starts such a run would have to keep calling its step operation
itself. This module does that in the background instead: call it once, and a
daemon thread keeps stepping until the run stops being "running" -- it waits
for a human, finishes, or fails.

Core; imports no integration. The contract, operation and payload are plain
data, so this has no idea what it is driving.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from app.services.application_logging import log_event


InvokeFn = Callable[[str, str, dict[str, Any], str, Any], Any]
ResultFn = Callable[[Any], bool | None]

# Runs currently being driven, so the same one is never driven twice at once.
_live_keys: set[str] = set()
_live_keys_lock = threading.Lock()


def _default_invoke(contract: str, operation: str, payload: dict[str, Any], project_id: str, actor: Any) -> Any:
    from app.services.operator_dispatch import _contract_registry

    return _contract_registry().invoke(contract, operation, payload, project_id=project_id, actor=actor)


def _key(contract: str, operation: str, payload: dict[str, Any], dedupe_key: str = "") -> str:
    return json.dumps([contract, operation, payload, dedupe_key], sort_keys=True, ensure_ascii=False)


def drive_in_background(
    *,
    contract: str,
    operation: str,
    payload: dict[str, Any],
    project_id: str,
    actor: Any,
    max_calls: int = 200,
    dedupe_key: str = "",
    invoke: InvokeFn | None = None,
    on_failure: Callable[[BaseException], None] | None = None,
    on_result: ResultFn | None = None,
) -> threading.Thread | None:
    """Call ``invoke(contract, operation, payload, project_id, actor)`` repeatedly
    while its result's ``status == "running"``, at most ``max_calls`` times.

    Returns the started ``Thread``, or ``None`` when the same
    (contract, operation, payload) is already being driven -- that call is a
    no-op, not a second driver for the same run.

    ``on_failure`` (Phase 16j), when given, is called with the exception if
    the drive stops on an error, so a caller with no other way to observe a
    background failure -- a firing already marked "dispatched" before this
    thread even started -- can still make it visible. Its own failure is
    swallowed: an observer must never turn a logged error into a crash.
    """
    resolved_invoke = invoke or _default_invoke
    key = _key(contract, operation, payload, dedupe_key)

    with _live_keys_lock:
        if key in _live_keys:
            return None
        _live_keys.add(key)

    def run() -> None:
        try:
            for _ in range(max_calls):
                result = resolved_invoke(contract, operation, payload, project_id, actor)
                if on_result is not None:
                    try:
                        should_continue = on_result(result)
                    except Exception:
                        should_continue = None
                else:
                    should_continue = None
                if should_continue is False:
                    break
                if not isinstance(result, dict) or result.get("status") != "running":
                    break
                try:
                    delay = max(0.0, min(float(result.get("requeue_after_seconds") or 0), 300.0))
                except (TypeError, ValueError):
                    delay = 0.0
                if delay:
                    time.sleep(delay)
        except Exception as exc:
            log_event(
                level="error", category="integration", source="contract_driver",
                message=f"Automatic driving of {contract}.{operation} stopped: {exc}",
                details={"contract": contract, "operation": operation, "payload": payload},
            )
            if on_failure is not None:
                try:
                    on_failure(exc)
                except Exception:
                    pass
        finally:
            with _live_keys_lock:
                _live_keys.discard(key)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


__all__ = ["drive_in_background"]
