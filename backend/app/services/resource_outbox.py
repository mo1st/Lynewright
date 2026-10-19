"""Durable at-least-once delivery for canonical resource lifecycle work."""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformOperationError, PlatformStore, PlatformValidationError, _redact


class ResourceOutboxService:
    MAX_LEASE_SECONDS = 3_600
    _STATUSES = frozenset({"pending", "processing", "processed", "dead_letter", "superseded"})

    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = "ten_default") -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_resource_outbox (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT '',
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                aggregate_revision INTEGER NOT NULL DEFAULT 1,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                trace_id TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','processed','dead_letter','superseded')),
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_token TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT,
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                replay_count INTEGER NOT NULL DEFAULT 0,
                last_replayed_by TEXT NOT NULL DEFAULT '',
                last_replay_reason TEXT NOT NULL DEFAULT '',
                last_replayed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_outbox_idempotency
              ON platform_resource_outbox(tenant_id,idempotency_key);
            CREATE INDEX IF NOT EXISTS idx_resource_outbox_claim
              ON platform_resource_outbox(tenant_id,status,available_at,created_at);
            CREATE INDEX IF NOT EXISTS idx_resource_outbox_aggregate
              ON platform_resource_outbox(tenant_id,aggregate_type,aggregate_id,aggregate_revision,status);
            """
        )
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(platform_resource_outbox)").fetchall()}
        for name, definition in (
            ("replay_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_replayed_by", "TEXT NOT NULL DEFAULT ''"),
            ("last_replay_reason", "TEXT NOT NULL DEFAULT ''"),
            ("last_replayed_at", "TEXT"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE platform_resource_outbox ADD COLUMN {name} {definition}")

    def enqueue(
        self,
        *,
        project_id: str | None,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_revision: int,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str,
        trace_id: str = "",
        db: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        if aggregate_revision < 1 or not all((aggregate_type, aggregate_id, event_type, idempotency_key)):
            raise PlatformValidationError("Resource outbox identifiers are invalid.")
        if (
            len(str(aggregate_type)) > 120
            or len(str(aggregate_id)) > 256
            or len(str(event_type)) > 160
            or len(str(trace_id)) > 256
            or len(str(idempotency_key)) > 512
        ):
            raise PlatformValidationError("Resource outbox identifiers exceed their bounds.")
        if not isinstance(payload, dict):
            raise PlatformValidationError("Resource outbox payload must be an object.")
        try:
            encoded = json.dumps(_redact(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise PlatformValidationError("Resource outbox payload must be strict JSON.") from exc
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise PlatformValidationError("Resource outbox payload exceeds the 256 KiB limit.")
        values = (
            f"out_{secrets.token_hex(12)}", self.tenant_id, project_id or "", aggregate_type[:120],
            aggregate_id[:256], aggregate_revision, event_type[:160], encoded, trace_id[:256], idempotency_key[:512],
        )
        if db is not None:
            return self._insert_or_get(db, values)
        with self._connection() as connection, connection:
            return self._insert_or_get(connection, values)

    def claim_next(self, *, worker_id: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        if not worker_id.strip() or len(worker_id) > 120 or lease_seconds < 1 or lease_seconds > self.MAX_LEASE_SECONDS:
            raise PlatformValidationError("Outbox worker and lease are invalid.")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT candidate.* FROM platform_resource_outbox candidate
                WHERE candidate.tenant_id=? AND candidate.attempts < 50
                  AND ((candidate.status='pending' AND candidate.available_at<=CURRENT_TIMESTAMP)
                    OR (candidate.status='processing' AND candidate.lease_expires_at<=CURRENT_TIMESTAMP))
                  AND NOT EXISTS (
                    SELECT 1 FROM platform_resource_outbox earlier
                    WHERE earlier.tenant_id=candidate.tenant_id
                      AND earlier.aggregate_type=candidate.aggregate_type
                      AND earlier.aggregate_id=candidate.aggregate_id
                      AND earlier.aggregate_revision < candidate.aggregate_revision
                      AND earlier.status IN ('pending','processing')
                  )
                ORDER BY candidate.created_at,candidate.id LIMIT 1""",
                (self.tenant_id,),
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            token = secrets.token_hex(16)
            changed = db.execute(
                """UPDATE platform_resource_outbox
                SET status='processing',attempts=attempts+1,lease_owner=?,lease_token=?,
                    lease_expires_at=datetime('now','+' || ? || ' seconds')
                WHERE tenant_id=? AND id=? AND (status='pending' OR lease_expires_at<=CURRENT_TIMESTAMP)""",
                (worker_id[:120], token, lease_seconds, self.tenant_id, row["id"]),
            ).rowcount
            if not changed:
                db.rollback()
                return None
            claimed = dict(row)
            claimed.update({"status": "processing", "lease_owner": worker_id[:120], "lease_token": token, "attempts": int(row["attempts"]) + 1})
            try:
                decoded = json.loads(claimed.pop("payload_json") or "{}")
                claimed["payload"] = decoded if isinstance(decoded, dict) else {}
            except json.JSONDecodeError:
                claimed["payload"] = {}
            db.commit()
            return claimed

    def complete(self, *, outbox_id: str, worker_id: str, lease_token: str) -> dict[str, Any]:
        return self._finish(outbox_id, worker_id, lease_token, status="processed")

    def retry(self, *, outbox_id: str, worker_id: str, lease_token: str, error_code: str, error_message: str, delay_seconds: int = 5) -> dict[str, Any]:
        if delay_seconds < 0 or delay_seconds > 86_400:
            raise PlatformValidationError("Outbox retry delay is invalid.")
        with self._connection() as db, db:
            changed = db.execute(
                """UPDATE platform_resource_outbox SET status='pending',available_at=datetime('now','+' || ? || ' seconds'),
                lease_owner='',lease_token='',lease_expires_at=NULL,last_error_code=?,last_error_message=?
                WHERE tenant_id=? AND id=? AND status='processing' AND lease_owner=? AND lease_token=?""",
                (delay_seconds, error_code[:120], error_message[:2_000], self.tenant_id, outbox_id, worker_id, lease_token),
            ).rowcount
        if not changed:
            raise PlatformConflictError("Outbox lease is stale or item is not processing.")
        return self.get(outbox_id)

    def dead_letter(self, *, outbox_id: str, worker_id: str, lease_token: str, error_code: str, error_message: str) -> dict[str, Any]:
        return self._finish(outbox_id, worker_id, lease_token, status="dead_letter", error_code=error_code, error_message=error_message)

    def replay_dead_letter(self, *, outbox_id: str, operator_id: str, reason: str, max_replays: int = 10) -> dict[str, Any]:
        """Explicitly requeue one dead letter after an operator decision."""
        if not operator_id.strip() or len(operator_id) > 160 or not reason.strip() or len(reason) > 2_000:
            raise PlatformValidationError("Outbox replay requires a bounded operator id and reason.")
        if max_replays < 1 or max_replays > 50:
            raise PlatformValidationError("Outbox replay limit is invalid.")
        with self._connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,replay_count FROM platform_resource_outbox WHERE tenant_id=? AND id=?", (self.tenant_id, outbox_id)).fetchone()
            if row is None:
                raise PlatformNotFoundError("Resource outbox item was not found.")
            if row["status"] != "dead_letter":
                raise PlatformConflictError("Only dead-lettered work can be replayed.")
            if int(row["replay_count"] or 0) >= max_replays:
                raise PlatformConflictError("Outbox replay limit has been reached.")
            changed = db.execute(
                """UPDATE platform_resource_outbox SET status='pending',attempts=0,available_at=CURRENT_TIMESTAMP,
                lease_owner='',lease_token='',lease_expires_at=NULL,
                replay_count=replay_count+1,last_replayed_by=?,last_replay_reason=?,last_replayed_at=CURRENT_TIMESTAMP,
                processed_at=NULL WHERE tenant_id=? AND id=? AND status='dead_letter'""",
                (operator_id[:160], reason[:2_000], self.tenant_id, outbox_id),
            ).rowcount
            if changed != 1:
                raise PlatformConflictError("Outbox replay was superseded or already replayed.")
        replayed = self.get(outbox_id)
        self._record_replay_trace([outbox_id], operator_id=operator_id)
        return replayed

    def replay_dead_letters(
        self,
        *,
        outbox_ids: list[str],
        operator_id: str,
        reason: str,
        max_replays: int = 10,
    ) -> list[dict[str, Any]]:
        """Atomically requeue a bounded set of dead letters.

        Batch replay is deliberately all-or-nothing: an operator should never
        believe a remediation reason covered a subset while another item was
        silently skipped. Tenant scoping and the same per-item replay cap as
        the single-item path are enforced inside one immediate transaction.
        """
        if not isinstance(outbox_ids, list) or not outbox_ids or len(outbox_ids) > 100:
            raise PlatformValidationError("Outbox replay requires between one and 100 item ids.")
        normalized = [str(item).strip() for item in outbox_ids]
        if any(not item or len(item) > 256 for item in normalized) or len(set(normalized)) != len(normalized):
            raise PlatformValidationError("Outbox replay item ids are invalid or duplicated.")
        if not operator_id.strip() or len(operator_id) > 160 or not reason.strip() or len(reason) > 2_000:
            raise PlatformValidationError("Outbox replay requires a bounded operator id and reason.")
        if max_replays < 1 or max_replays > 50:
            raise PlatformValidationError("Outbox replay limit is invalid.")
        with self._connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,status,replay_count FROM platform_resource_outbox WHERE tenant_id=? AND id IN (%s)"
                % ",".join("?" for _ in normalized),
                [self.tenant_id, *normalized],
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            missing = [item for item in normalized if item not in by_id]
            if missing:
                raise PlatformNotFoundError("Resource outbox item was not found.")
            if any(row["status"] != "dead_letter" for row in rows):
                raise PlatformConflictError("Only dead-lettered work can be replayed.")
            if any(int(row["replay_count"] or 0) >= max_replays for row in rows):
                raise PlatformConflictError("Outbox replay limit has been reached.")
            for item in normalized:
                changed = db.execute(
                    """UPDATE platform_resource_outbox SET status='pending',attempts=0,available_at=CURRENT_TIMESTAMP,
                    lease_owner='',lease_token='',lease_expires_at=NULL,replay_count=replay_count+1,
                    last_replayed_by=?,last_replay_reason=?,last_replayed_at=CURRENT_TIMESTAMP,processed_at=NULL
                    WHERE tenant_id=? AND id=? AND status='dead_letter'""",
                    (operator_id[:160], reason[:2_000], self.tenant_id, item),
                ).rowcount
                if changed != 1:
                    raise PlatformConflictError("Outbox replay was superseded or already replayed.")
        replayed = [self.get(item) for item in normalized]
        self._record_replay_trace(normalized, operator_id=operator_id)
        return replayed

    def _record_replay_trace(self, outbox_ids: list[str], *, operator_id: str) -> None:
        """Record a best-effort operator span without affecting queue state."""
        try:
            from app.services.capability_context import CapabilityInvocationContext, new_trace_id
            from app.services.operation_trace import OperationTraceService

            context = CapabilityInvocationContext(
                tenant_id=self.tenant_id,
                actor_type="operator",
                actor_id=operator_id,
                client_id=operator_id,
                project_id=None,
                resource_type="outbox",
                resource_id=outbox_ids[0] if len(outbox_ids) == 1 else None,
                integration_id=None,
                connection_id=None,
                trace_id=new_trace_id(),
                parent_span_id=None,
                idempotency_key=None,
                entry_point="operator",
            )
            traces = OperationTraceService(self.store, tenant_id=self.tenant_id)
            traces.start(context, operation="resource.outbox.replay", metadata={"outbox_count": len(outbox_ids)})
            traces.record(
                context,
                stage="replay",
                category="operator",
                status="completed",
                metadata={"outbox_ids": outbox_ids[:100]},
            )
            traces.finish(context.trace_id, tenant_id=self.tenant_id, status="completed")
        except Exception:
            # Trace storage is observational; a schema/retention issue must not
            # turn a committed, audited replay into a false failure.
            return

    def get(self, outbox_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute("SELECT * FROM platform_resource_outbox WHERE tenant_id=? AND id=?", (self.tenant_id, outbox_id)).fetchone()
        if row is None:
            raise PlatformNotFoundError("Resource outbox item was not found.")
        return self._public(row)

    def list(self, *, status: str | None = None, aggregate_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Return a bounded, tenant-scoped operator view of lifecycle work."""
        if limit < 1 or limit > 1_000:
            raise PlatformValidationError("Outbox list limit is invalid.")
        if status is not None and status not in self._STATUSES:
            raise PlatformValidationError("Outbox status filter is invalid.")
        if aggregate_id is not None and len(str(aggregate_id)) > 256:
            raise PlatformValidationError("Outbox aggregate filter is invalid.")
        clauses = ["tenant_id=?"]
        params: list[Any] = [self.tenant_id]
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if aggregate_id is not None:
            clauses.append("aggregate_id=?")
            params.append(aggregate_id)
        params.append(limit)
        with self._connection() as db:
            rows = db.execute(
                f"SELECT * FROM platform_resource_outbox WHERE {' AND '.join(clauses)} ORDER BY created_at,id LIMIT ?",
                params,
            ).fetchall()
        return [self._public(row) for row in rows]

    def _insert_or_get(self, db: sqlite3.Connection, values: tuple[Any, ...]) -> dict[str, Any]:
        db.execute(
            """INSERT OR IGNORE INTO platform_resource_outbox
            (id,tenant_id,project_id,aggregate_type,aggregate_id,aggregate_revision,event_type,payload_json,trace_id,idempotency_key)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        row = db.execute("SELECT * FROM platform_resource_outbox WHERE tenant_id=? AND idempotency_key=?", (self.tenant_id, values[9])).fetchone()
        if row is None:
            raise PlatformConflictError("Resource outbox item could not be persisted.")
        return self._public(row)

    def _finish(self, outbox_id: str, worker_id: str, lease_token: str, *, status: str, error_code: str = "", error_message: str = "") -> dict[str, Any]:
        with self._connection() as db, db:
            changed = db.execute(
                """UPDATE platform_resource_outbox SET status=?,processed_at=CASE WHEN ?='processed' THEN CURRENT_TIMESTAMP ELSE NULL END,
                lease_owner='',lease_token='',lease_expires_at=NULL,last_error_code=?,last_error_message=?
                WHERE tenant_id=? AND id=? AND status='processing' AND lease_owner=? AND lease_token=?""",
                (status, status, error_code[:120], error_message[:2_000], self.tenant_id, outbox_id, worker_id, lease_token),
            ).rowcount
        if not changed:
            raise PlatformConflictError("Outbox lease is stale or item is not processing.")
        return self.get(outbox_id)

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        """Shape one row for a reader that is not the worker holding the lease.

        ``lease_token`` is the fencing credential that authorizes complete,
        retry, and dead-letter transitions. Only ``claim_next`` returns it, and
        only to the worker that just won the claim; operator diagnostics get
        the lease *owner* and expiry, which is what queue triage actually
        needs, without handing a reader the means to finish someone else's
        work.
        """
        result = dict(row)
        result.pop("lease_token", None)
        decoded: Any = {}
        try:
            decoded = json.loads(result.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            decoded = {}
        result["payload"] = decoded if isinstance(decoded, dict) else {}
        return result


__all__ = ["ResourceOutboxService"]
