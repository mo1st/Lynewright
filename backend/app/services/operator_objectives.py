"""Durable "operator objective requested" records — the automation→operator boundary.

The internal Operator runtime lives on another branch and does not exist at
commit ``c7e1c03``. Nothing here may import it. Instead, every trigger writes a
durable objective request row and emits a durable ``operator.objective_requested``
event. A future integration session wires a dispatcher that:

    1. polls/claims pending requests (or subscribes to the event once the
       platform bus carries the ``operator`` namespace),
    2. starts or resumes an operator run,
    3. reports the outcome back through :meth:`claim` / :meth:`complete` /
       :meth:`fail` / :meth:`reject`.

Requests are tenant-scoped, deduplicated by explicit key, and auditable. The
request lifecycle uses the shared operator vocabulary where the states overlap:
``pending`` → ``accepted`` → ``completed`` | ``failed`` | ``rejected`` |
``cancelled``. An always-running process is never required: requests survive
restarts in SQLite.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from app.services.platform_services import (
    EventService,
    PlatformNotFoundError,
    PlatformStore,
    PlatformValidationError,
    _redact,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID


OBJECTIVE_REQUEST_STATUSES: tuple[str, ...] = (
    "pending", "accepted", "completed", "failed", "rejected", "cancelled",
)
OPEN_STATUSES: tuple[str, ...] = ("pending", "accepted")
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "rejected", "cancelled")
OBJECTIVE_SOURCES: tuple[str, ...] = ("manual", "schedule", "event", "condition")

_MAX_OBJECTIVE_CHARS = 8_000
_MAX_TITLE_CHARS = 200
_MAX_JSON_BYTES = 64 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


class OperatorObjectiveService:
    """Persists operator objective requests; no Operator runtime required."""

    def __init__(
        self,
        store: PlatformStore | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        events: EventService | None = None,
    ) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id
        self.events = events or EventService(self.store, tenant_id=self.tenant_id)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS operator_objective_requests (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT,
                source TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                objective TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                automation_id TEXT,
                trigger_type TEXT NOT NULL DEFAULT '',
                trigger_identity TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT,
                run_ref TEXT NOT NULL DEFAULT '',
                result_summary TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claimed_at TEXT,
                settled_at TEXT,
                UNIQUE(tenant_id, dedupe_key)
            );
            CREATE INDEX IF NOT EXISTS idx_operator_objective_requests_scope
                ON operator_objective_requests(tenant_id, project_id, status, requested_at DESC);
            CREATE INDEX IF NOT EXISTS idx_operator_objective_requests_automation
                ON operator_objective_requests(tenant_id, automation_id, requested_at DESC);
            """
        )

    # -- creation -----------------------------------------------------------

    def request(
        self,
        objective: str,
        *,
        project_id: str | None = None,
        source: str = "manual",
        title: str = "",
        trigger_type: str = "",
        trigger_identity: str = "",
        automation_id: str | None = None,
        context: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        """Persist one objective request; returns the public record.

        With ``dedupe_key``, an already-open request for the same key is
        returned unchanged (``created=False``) so scheduler restarts, event
        redelivery, and level-held conditions cannot pile up duplicates.
        """
        text = str(objective or "").strip()
        if not text:
            raise PlatformValidationError("Operator objectives require non-empty text.")
        if len(text) > _MAX_OBJECTIVE_CHARS:
            raise PlatformValidationError("Operator objectives are limited to 8,000 characters.")
        if source not in OBJECTIVE_SOURCES:
            raise PlatformValidationError(f"Unknown objective source: {source!r}.")
        if dedupe_key is not None:
            if not _ID_RE.fullmatch(str(dedupe_key)):
                raise PlatformValidationError("Objective dedupe keys must use a bounded identifier.")
            existing = self.find_open_by_dedupe_key(dedupe_key)
            if existing is not None:
                return {**existing, "created": False}
        request_id = f"opr_{secrets.token_hex(12)}"
        encoded_context = self._safe_context(context)
        record_title = str(title or "").strip()[:_MAX_TITLE_CHARS]
        with self._connection() as db, db:
            try:
                db.execute(
                    """INSERT INTO operator_objective_requests
                    (id,tenant_id,project_id,source,title,objective,context_json,status,
                     automation_id,trigger_type,trigger_identity,dedupe_key)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        request_id, self.tenant_id, project_id, source, record_title, text,
                        encoded_context, "pending", automation_id, str(trigger_type)[:64],
                        str(trigger_identity)[:160], dedupe_key,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # Lost a dedupe race with a concurrent process: that process's
                # request stands. Surface it so callers do not double-dispatch.
                raise PlatformValidationError("An open objective request with this dedupe key exists.") from exc
        record = self.get(request_id)
        self._emit("operator.objective_requested", record)
        return {**record, "created": True}

    # -- lifecycle transitions ---------------------------------------------

    def claim(self, request_id: str, *, run_ref: str) -> dict[str, Any]:
        """The Operator runtime accepted the request and started/resumed a run."""
        with self._connection() as db, db:
            cursor = db.execute(
                """UPDATE operator_objective_requests
                SET status='accepted', run_ref=?, claimed_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=? AND status='pending'""",
                (str(run_ref)[:160], request_id, self.tenant_id),
            )
            self._require_transition(cursor, request_id, "accepted", from_status="pending")
        updated = self.get(request_id)
        self._emit("operator.objective_updated", updated)
        return updated

    def complete(self, request_id: str, *, result_summary: str = "") -> dict[str, Any]:
        return self._settle(request_id, status="completed", summary=result_summary)

    def fail(self, request_id: str, *, error_code: str, message: str = "") -> dict[str, Any]:
        return self._settle(request_id, status="failed", summary=message, error_code=error_code)

    def reject(self, request_id: str, *, reason: str = "") -> dict[str, Any]:
        return self._settle(request_id, status="rejected", summary=reason)

    def cancel(self, request_id: str, *, reason: str = "") -> dict[str, Any]:
        return self._settle(request_id, status="cancelled", summary=reason)

    # -- reads ---------------------------------------------------------------

    def get(self, request_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM operator_objective_requests WHERE id=? AND tenant_id=?",
                (request_id, self.tenant_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Operator objective request not found.")
        return self._public(row)

    def list(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        automation_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in OBJECTIVE_REQUEST_STATUSES:
            raise PlatformValidationError(f"Unknown objective request status: {status!r}")
        limit = max(1, min(int(limit), 200))
        query = "SELECT * FROM operator_objective_requests WHERE tenant_id=?"
        params: list[Any] = [self.tenant_id]
        if project_id is not None:
            query += " AND project_id=?"
            params.append(project_id)
        if status is not None:
            query += " AND status=?"
            params.append(status)
        if automation_id is not None:
            query += " AND automation_id=?"
            params.append(automation_id)
        query += " ORDER BY requested_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        return [self._public(row) for row in rows]

    def find_open_by_dedupe_key(self, dedupe_key: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                f"""SELECT * FROM operator_objective_requests
                WHERE tenant_id=? AND dedupe_key=? AND status IN ({",".join("?" for _ in OPEN_STATUSES)})
                ORDER BY requested_at DESC LIMIT 1""",
                (self.tenant_id, dedupe_key, *OPEN_STATUSES),
            ).fetchone()
        return self._public(row) if row is not None else None

    def pending_count(self) -> int:
        with self._connection() as db:
            row = db.execute(
                "SELECT COUNT(*) AS total FROM operator_objective_requests WHERE tenant_id=? AND status='pending'",
                (self.tenant_id,),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    # -- internals ------------------------------------------------------------

    def _settle(self, request_id: str, *, status: str, summary: str, error_code: str = "") -> dict[str, Any]:
        current = self.get(request_id)
        if current["status"] not in OPEN_STATUSES:
            raise PlatformValidationError(
                f"Only open requests can be settled as {status}; this one is {current['status']}."
            )
        with self._connection() as db, db:
            if status == "failed":
                cursor = db.execute(
                    """UPDATE operator_objective_requests
                    SET status='failed', error_code=?, result_summary=?, settled_at=CURRENT_TIMESTAMP
                    WHERE id=? AND tenant_id=? AND status IN ('pending','accepted')""",
                    (str(error_code or "operator.failed")[:96], str(summary or "")[:2_000], request_id, self.tenant_id),
                )
            else:
                cursor = db.execute(
                    f"""UPDATE operator_objective_requests
                    SET status=?, result_summary=?, settled_at=CURRENT_TIMESTAMP
                    WHERE id=? AND tenant_id=? AND status IN ('pending','accepted')""",
                    (status, str(summary or "")[:2_000], request_id, self.tenant_id),
                )
            self._require_transition(cursor, request_id, status, from_status=current["status"])
        updated = self.get(request_id)
        self._emit("operator.objective_settled", updated)
        return updated

    def _require_transition(self, cursor: sqlite3.Cursor, request_id: str, to_status: str, *, from_status: str) -> None:
        if cursor.rowcount != 1:
            raise PlatformValidationError(
                f"Objective request {request_id} is no longer {from_status}; transition to {to_status} refused."
            )

    def _emit(self, event_type: str, record: dict[str, Any]) -> None:
        try:
            self.events.publish({
                "type": event_type,
                "payload": {
                    "request_id": record["id"],
                    "project_id": record.get("project_id"),
                    "source": record.get("source"),
                    "status": record.get("status"),
                    "title": record.get("title"),
                    "objective": "" if event_type != "operator.objective_requested" else record.get("objective"),
                    "automation_id": record.get("automation_id"),
                    "trigger_type": record.get("trigger_type"),
                    "trigger_identity": record.get("trigger_identity"),
                    "run_ref": record.get("run_ref"),
                    "dedupe_key": record.get("dedupe_key"),
                },
            })
        except Exception:
            # The durable row is the contract; event delivery is best-effort so
            # a broken notification surface can never block triggering.
            pass

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["context"] = json.loads(value.pop("context_json") or "{}")
        return value

    @staticmethod
    def _safe_context(context: dict[str, Any] | None) -> str:
        payload = _redact(context if isinstance(context, dict) else {})
        try:
            encoded = json.dumps(payload, sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("Objective context must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
            raise PlatformValidationError("Objective context exceeds the 64 KiB limit.")
        return encoded


__all__ = [
    "OBJECTIVE_REQUEST_STATUSES",
    "OBJECTIVE_SOURCES",
    "OPEN_STATUSES",
    "OperatorObjectiveService",
    "TERMINAL_STATUSES",
]
