"""Application log store — the operator-facing record of what went wrong.

Two rules make an entry useful rather than merely present:

- **An error log carries the exception.** Callers may pass `exc=`, but they do
  not have to: at `error`/`critical` level the currently-handled exception is
  captured automatically (`sys.exc_info`), so every `except: log_event(...)`
  site in the codebase records type, cause chain and traceback without each one
  having to remember to. `details` supplied by the caller always wins.
- **An error log says which request produced it.** The HTTP middleware binds a
  correlation id, method and path into a context variable, so a failure logged
  five layers down joins to its request without threading arguments through.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import traceback
from contextlib import closing
from typing import Any

from app import database
from app.services.tenant_schema import DEFAULT_TENANT_ID


LEVELS = {"debug", "info", "warning", "error", "critical"}
SENSITIVE_PARTS = {"key", "token", "secret", "password", "authorization", "cookie"}
_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:api[_-]?key|key|token|secret|password|authorization|access[_-]?token)=)[^&#\s]+"
)
_PY_LEVELS = {
    "debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
    "error": logging.ERROR, "critical": logging.CRITICAL,
}
# Mirrored to stdout as well as SQLite: on stateless hosts (Cloud Run) the log
# table is wiped on every restart, so the platform log is the only copy that
# survives the incident being diagnosed.
_stream_logger = logging.getLogger("lynewright.events")
# Deepest frames first — the immediate cause is what an operator reads.
_TRACEBACK_FRAME_LIMIT = 12
# Levels that always get exception context, explicit or ambient.
_FAILURE_LEVELS = frozenset({"error", "critical"})
# Fields whose whole value is the diagnostic; the generic string cap would cut
# the frame that matters off the end.
_LONG_TEXT_KEYS = frozenset({
    "traceback", "provider_response", "response_body", "stdout", "stderr", "detail",
})
_LONG_TEXT_LIMIT = 6000

_request_context: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "lynewright_request_context", default={},
)


def bind_request_context(**fields: str) -> contextvars.Token:
    """Attach request identity to every log written while handling it."""
    return _request_context.set({key: str(value) for key, value in fields.items() if value})


def reset_request_context(token: contextvars.Token) -> None:
    try:
        _request_context.reset(token)
    except ValueError:  # a token from another context is not an error worth raising
        return


def current_request_context() -> dict[str, str]:
    return dict(_request_context.get() or {})


def _exception_details(exc: BaseException, *, ambient: bool = False) -> dict[str, Any]:
    """Type, message, cause chain and a bounded traceback for one exception."""
    details: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "error_message": _sanitize_message(str(exc))[:1000],
    }
    if ambient:
        # The caller did not hand us this exception; say so, so nobody reads a
        # coincidental in-flight error as the logged failure's proven cause.
        details["error_capture"] = "active_exception"
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        details["error_cause"] = f"{type(cause).__name__}: {_sanitize_message(str(cause))[:300]}"
    try:
        frames = traceback.format_exception(type(exc), exc, exc.__traceback__)
    except Exception:  # a malformed traceback must not lose the rest of the log
        return details
    details["traceback"] = _sanitize_message("".join(frames[-_TRACEBACK_FRAME_LIMIT:]))[:_LONG_TEXT_LIMIT]
    return details


def log_event(
    *, level: str, category: str, source: str, message: str,
    details: dict[str, Any] | None = None, correlation_id: str = "",
    request_path: str = "", project_id: str | None = None,
    tenant_id: str | None = None, exc: BaseException | None = None,
    capture_active_exception: bool = True,
) -> None:
    normalized_level = level if level in LEVELS else "info"
    merged = dict(details or {})
    if exc is None and capture_active_exception and normalized_level in _FAILURE_LEVELS:
        active = sys.exc_info()[1]
        if active is not None:
            merged = {**_exception_details(active, ambient=True), **merged}
    elif exc is not None:
        # Explicit details win: a caller-supplied field is more specific than
        # the generic exception projection.
        merged = {**_exception_details(exc), **merged}
    request = current_request_context()
    if request:
        correlation_id = correlation_id or request.get("correlation_id", "")
        request_path = request_path or request.get("request_path", "")
        for key in ("http_method", "client_kind"):
            if request.get(key) and key not in merged:
                merged[key] = request[key]
    safe_details = _sanitize(merged)
    encoded = json.dumps(safe_details, ensure_ascii=False, default=str)
    if len(encoded) > 16_000:
        encoded = json.dumps({"truncated": True, "preview": encoded[:15_500]})
    _mirror_to_stream(normalized_level, category, source, message, encoded, correlation_id, request_path)
    try:
        database.init_db()
        # The log write runs on its own budget, far smaller than the request
        # path's `BUSY_TIMEOUT_MS`: it happens after the operation its entry
        # records has already failed, and reopening a 30s window there turns one
        # contention stall into two at the moment the DB can least absorb it.
        with closing(database.get_connection(
            busy_timeout_ms=database.LOG_BUSY_TIMEOUT_MS,
        )) as connection, connection:
            resolved_tenant_id = (tenant_id or "").strip()
            if not resolved_tenant_id and project_id:
                row = connection.execute(
                    "SELECT tenant_id FROM tenant_projects WHERE project_id=? LIMIT 1",
                    (project_id,),
                ).fetchone()
                if row is not None:
                    resolved_tenant_id = str(row["tenant_id"] or "")
            resolved_tenant_id = resolved_tenant_id or DEFAULT_TENANT_ID
            connection.execute(
                """INSERT INTO application_logs (
                   level, category, source, message, details_json, correlation_id,
                   request_path, project_id, tenant_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (normalized_level, category[:80], source[:120], _sanitize_message(message)[:2000], encoded,
                 correlation_id[:120], request_path[:500], project_id, resolved_tenant_id),
            )
            connection.execute(
                """DELETE FROM application_logs WHERE id NOT IN (
                   SELECT id FROM application_logs ORDER BY id DESC LIMIT 5000
                )"""
            )
    except Exception as store_error:
        # Logging must never take down the operation that is being diagnosed —
        # but a log store that is silently failing hides every later error too,
        # so the failure itself goes to the platform log.
        try:
            _stream_logger.error(
                "application log write failed: %s: %s",
                type(store_error).__name__, store_error, exc_info=False,
            )
        except Exception:
            pass
        return


_loop_failure_streaks: dict[str, int] = {}


def log_loop_failure(
    *, category: str, source: str, message: str, exc: BaseException,
    details: dict[str, Any] | None = None, tick_seconds: float | None = None,
    project_id: str | None = None, tenant_id: str | None = None,
) -> None:
    """Record a failure inside a background loop, with its failure streak.

    A scheduler that fails forever and a scheduler that hiccuped once produce the
    same single log line otherwise, and the difference is the whole diagnosis.
    """
    key = f"{category}:{source}"
    streak = _loop_failure_streaks.get(key, 0) + 1
    _loop_failure_streaks[key] = streak
    payload: dict[str, Any] = {
        "consecutive_failures": streak,
        "loop": source,
        **(details or {}),
    }
    if tick_seconds is not None:
        payload["retry_in_seconds"] = tick_seconds
    log_event(
        # A loop that never recovers has stopped being a warning.
        level="critical" if streak >= 5 else "error",
        category=category, source=source,
        message=f"{message} ({streak} consecutive)" if streak > 1 else message,
        details=payload, exc=exc, project_id=project_id, tenant_id=tenant_id,
    )


def clear_loop_failures(*, category: str, source: str) -> None:
    """Called after a successful iteration so the next streak starts at one."""
    _loop_failure_streaks.pop(f"{category}:{source}", None)


def _mirror_to_stream(
    level: str, category: str, source: str, message: str,
    encoded_details: str, correlation_id: str, request_path: str,
) -> None:
    try:
        payload = {
            "severity": level.upper(), "category": category, "source": source,
            "message": _sanitize_message(message)[:2000], "details": encoded_details,
        }
        if correlation_id:
            payload["correlation_id"] = correlation_id
        if request_path:
            payload["request_path"] = request_path
        _stream_logger.log(_PY_LEVELS.get(level, logging.INFO), json.dumps(payload, default=str))
    except Exception:
        return


def list_logs(
    *, level: str | None = None, category: str | None = None,
    search: str | None = None, limit: int = 100, offset: int = 0,
    tenant_id: str | None = None, project_id: str | None = None,
) -> dict[str, Any]:
    database.init_db()
    clauses: list[str] = []
    params: list[Any] = []
    scope_clauses: list[str] = []
    scope_params: list[Any] = []
    if tenant_id:
        clauses.append("tenant_id = ?"); params.append(tenant_id)
        scope_clauses.append("tenant_id = ?"); scope_params.append(tenant_id)
    if project_id:
        clauses.append("project_id = ?"); params.append(project_id)
        scope_clauses.append("project_id = ?"); scope_params.append(project_id)
    if level:
        clauses.append("level = ?"); params.append(level)
    if category:
        clauses.append("category = ?"); params.append(category)
    if search:
        clauses.append("(message LIKE ? OR source LIKE ? OR details_json LIKE ?)")
        pattern = f"%{search}%"; params.extend([pattern, pattern, pattern])
    where = " AND ".join(clauses) if clauses else "1 = 1"
    with closing(database.get_connection()) as connection, connection:
        total = int(connection.execute(
            f"SELECT COUNT(*) AS count FROM application_logs WHERE {where}", params
        ).fetchone()["count"])
        rows = connection.execute(
            f"""SELECT * FROM application_logs WHERE {where}
                ORDER BY id DESC LIMIT ? OFFSET ?""", (*params, min(limit, 500), offset)
        ).fetchall()
        scope_where = " AND ".join(scope_clauses) if scope_clauses else "1 = 1"
        count_rows = connection.execute(
            f"SELECT level, COUNT(*) AS count FROM application_logs WHERE {scope_where} GROUP BY level",
            scope_params,
        ).fetchall()
    return {
        "items": [_row(row) for row in rows], "total": total,
        "counts": {str(row["level"]): int(row["count"]) for row in count_rows},
    }


def clear_logs(*, tenant_id: str | None = None, project_id: str | None = None) -> int:
    database.init_db()
    clauses: list[str] = []
    params: list[Any] = []
    if tenant_id:
        clauses.append("tenant_id = ?"); params.append(tenant_id)
    if project_id:
        clauses.append("project_id = ?"); params.append(project_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(database.get_connection()) as connection, connection:
        cursor = connection.execute(f"DELETE FROM application_logs{where}", params)
    return int(cursor.rowcount)


def _row(row) -> dict[str, Any]:
    value = dict(row)
    try:
        value["details"] = json.loads(value.pop("details_json") or "{}")
    except json.JSONDecodeError:
        value["details"] = {"parse_error": True}
        value.pop("details_json", None)
    return value


def _sanitize(value: Any, key: str = "") -> Any:
    if any(part in key.lower() for part in SENSITIVE_PARTS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key)[:120]: _sanitize(item_value, str(item_key)) for item_key, item_value in list(value.items())[:100]}
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:_LONG_TEXT_LIMIT] if key in _LONG_TEXT_KEYS else value[:4000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:4000]


def _sanitize_message(value: str) -> str:
    sanitized = re.sub(r"(?i)bearer\s+[a-z0-9._~+/-]+", "Bearer [redacted]", value)
    sanitized = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[redacted-key]", sanitized)
    sanitized = re.sub(r"\bAIza[A-Za-z0-9_-]{20,}\b", "[redacted-key]", sanitized)
    sanitized = _SENSITIVE_QUERY_VALUE.sub(r"\1[redacted]", sanitized)
    return sanitized
