"""Provider-neutral interpretation of explicit terminal operation results.

An integration may accept a request successfully and later report that the
requested work failed.  That is different from a transport exception, but it
must not be represented as a successful operation: doing so invites agents to
repeat a non-idempotent action, request duplicate approvals, or exhaust their
step budget.

This module deliberately recognises only explicit, conventional terminal
states.  It contains no provider, contract, tool, or business-domain names.
Providers remain free to return their complete native result alongside this
normalised outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


_FAILURE_STATES = frozenset({
    "failed", "failure", "error", "errored", "cancelled", "canceled",
    "aborted", "rejected", "denied", "timed_out", "timeout", "expired",
})
_STATE_KEYS = ("state", "status")
_REASON_KEYS = (
    "error", "error_message", "failure_reason", "reason", "message", "detail",
)
_ENVELOPE_KEYS = ("result", "data", "execution", "operation", "job", "task", "async_operation")


@dataclass(frozen=True, slots=True)
class TerminalOperationFailure:
    """A completed operation whose declared result is terminally unsuccessful."""

    state: str
    message: str
    path: str


def terminal_operation_failure(value: Any) -> TerminalOperationFailure | None:
    """Return an explicit terminal failure declared by an operation result.

    Only dictionaries on the result's canonical envelope path are examined;
    arbitrary list members are intentionally not treated as the state of the
    enclosing operation.  This lets a response include, for example, a list
    containing a previously failed item without turning a successful batch
    request into a failed one.
    """
    for candidate, path in _outcome_envelopes(value):
        state = _terminal_failure_state(candidate)
        if state is None:
            continue
        return TerminalOperationFailure(
            state=state,
            message=_failure_message(candidate, state),
            path=path,
        )
    return None


def _outcome_envelopes(value: Any, *, path: str = "result", depth: int = 0) -> Iterator[tuple[dict[str, Any], str]]:
    if not isinstance(value, dict):
        return
    yield value, path
    # Nested generic execution envelopes are common for MCP, OpenAPI and
    # durable-operation transports. Bound recursion keeps attacker-controlled
    # response documents from becoming a traversal cost.
    if depth >= 2:
        return
    for key in _ENVELOPE_KEYS:
        nested = value.get(key)
        if isinstance(nested, dict):
            yield from _outcome_envelopes(nested, path=f"{path}.{key}", depth=depth + 1)


def _terminal_failure_state(value: dict[str, Any]) -> str | None:
    for key in _STATE_KEYS:
        raw = value.get(key)
        if not isinstance(raw, str):
            continue
        state = raw.strip().casefold().replace("-", "_").replace(" ", "_")
        if state in _FAILURE_STATES:
            return state
    return None


def _failure_message(value: dict[str, Any], state: str) -> str:
    reason = ""
    for key in _REASON_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            reason = candidate.strip()
            break
    stage = value.get("failed_stage")
    stage_text = str(stage).strip() if stage is not None else ""
    summary = reason or f"Operation reported terminal state {state!r}."
    if stage_text and stage_text.casefold() not in summary.casefold():
        summary = f"{summary} (stage: {stage_text})"
    return summary[:2_000]


__all__ = ["TerminalOperationFailure", "terminal_operation_failure"]
