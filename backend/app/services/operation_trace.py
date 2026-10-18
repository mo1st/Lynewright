"""Durable, redacted operation traces for cross-entry-point workflows."""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from app.services.capability_context import CapabilityInvocationContext
from app.services.platform_services import PlatformStore, PlatformValidationError, _redact


_MAX_MESSAGE = 2_000
_MAX_METADATA_BYTES = 64 * 1024
# Traces are operational evidence, not canonical truth: the plan's rule is that
# canonical tables stay authoritative and tracing carries its own retention.
# Thirty days comfortably covers the diagnosis window for a stuck binding or a
# dead-lettered outbox item, which is what these rows exist to explain.
DEFAULT_RETENTION_DAYS = 30


class OperationTraceService:
    """Stores trace metadata and ordered events without becoming event sourcing."""

    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str | None = None) -> None:
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
            CREATE TABLE IF NOT EXISTS platform_operation_traces (
                trace_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT,
                actor_type TEXT NOT NULL,
                actor_id TEXT,
                entry_point TEXT NOT NULL,
                operation TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_platform_operation_traces_scope
                ON platform_operation_traces(tenant_id, project_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS platform_operation_trace_events (
                trace_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                span_id TEXT NOT NULL,
                parent_span_id TEXT,
                tenant_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                code TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                resource_type TEXT,
                resource_id TEXT,
                integration_id TEXT,
                connection_id TEXT,
                job_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (trace_id, sequence),
                UNIQUE (trace_id, span_id)
            );
            CREATE INDEX IF NOT EXISTS idx_platform_operation_trace_events_tenant
                ON platform_operation_trace_events(tenant_id, trace_id, sequence);
            """
        )

    def start(self, context: CapabilityInvocationContext, *, operation: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        self._validate_operation(operation)
        self._assert_tenant(context)
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_operation_traces
                (trace_id,tenant_id,project_id,actor_type,actor_id,entry_point,operation)
                VALUES (?,?,?,?,?,?,?)""",
                (context.trace_id, context.tenant_id, context.project_id, context.actor_type,
                 context.actor_id, context.entry_point, operation[:256]),
            )
        if metadata:
            self.record(context, stage="start", category="lifecycle", status="running", metadata=metadata)
        return self.get(context.trace_id, tenant_id=context.tenant_id)

    def record(
        self,
        context: CapabilityInvocationContext,
        *,
        stage: str,
        category: str,
        status: str,
        code: str = "",
        message: str = "",
        metadata: dict[str, Any] | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        integration_id: str | None = None,
        connection_id: str | None = None,
        job_id: str | None = None,
        span_id: str | None = None,
    ) -> dict[str, Any]:
        self._assert_tenant(context)
        for label, value in (("stage", stage), ("category", category), ("status", status)):
            if not value or len(value) > 120:
                raise PlatformValidationError(f"Trace {label} is invalid.")
        safe_metadata = _redact(metadata or {})
        encoded = json.dumps(safe_metadata, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
            raise PlatformValidationError("Trace metadata exceeds the 64 KiB limit.")
        span = span_id or f"sp_{secrets.token_hex(12)}"
        with self._connection() as db, db:
            trace = db.execute(
                "SELECT tenant_id FROM platform_operation_traces WHERE trace_id=?",
                (context.trace_id,),
            ).fetchone()
            if trace is None or str(trace["tenant_id"]) != context.tenant_id:
                raise PlatformValidationError("Operation trace was not found in this tenant.")
            sequence = int(db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM platform_operation_trace_events WHERE trace_id=?",
                (context.trace_id,),
            ).fetchone()["next_sequence"])
            db.execute(
                """INSERT INTO platform_operation_trace_events
                (trace_id,sequence,span_id,parent_span_id,tenant_id,stage,category,status,code,message,
                 resource_type,resource_id,integration_id,connection_id,job_id,metadata_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (context.trace_id, sequence, span, context.parent_span_id, context.tenant_id,
                 stage[:120], category[:120], status[:40], code[:120], message[:_MAX_MESSAGE],
                 resource_type, resource_id, integration_id, connection_id, job_id, encoded),
            )
        return {"trace_id": context.trace_id, "span_id": span, "sequence": sequence}

    def finish(self, trace_id: str, *, tenant_id: str, status: str, code: str = "", message: str = "") -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled", "degraded"}:
            raise PlatformValidationError("Trace completion status is invalid.")
        with self._connection() as db, db:
            changed = db.execute(
                """UPDATE platform_operation_traces
                SET status=?,completed_at=CURRENT_TIMESTAMP,last_error_code=?,last_error_message=?
                WHERE trace_id=? AND tenant_id=?""",
                (status, code[:120], message[:_MAX_MESSAGE], trace_id, tenant_id),
            ).rowcount
        if not changed:
            raise PlatformValidationError("Operation trace was not found in this tenant.")
        return self.get(trace_id, tenant_id=tenant_id)

    def get(self, trace_id: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        expected_tenant = tenant_id or self.tenant_id
        with self._connection() as db:
            clauses = ["trace_id=?"]
            params: list[Any] = [trace_id]
            if expected_tenant is not None:
                clauses.append("tenant_id=?")
                params.append(expected_tenant)
            row = db.execute(f"SELECT * FROM platform_operation_traces WHERE {' AND '.join(clauses)}", params).fetchone()
            if row is None:
                raise PlatformValidationError("Operation trace was not found.")
            events = db.execute(
                "SELECT * FROM platform_operation_trace_events WHERE trace_id=? ORDER BY sequence",
                (trace_id,),
            ).fetchall()
        result = dict(row)
        result["events"] = []
        for event in events:
            value = dict(event)
            try:
                value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                value["metadata"] = {}
            result["events"].append(value)
        return result

    def purge_expired(
        self,
        *,
        tenant_id: str | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        limit: int = 500,
    ) -> int:
        """Delete a bounded batch of traces past the retention window.

        Both trace tables are additive and unbounded by construction, so
        without this they grow for the life of the database. Deletion is
        batched and optionally tenant-scoped so a startup pass or an operator
        task can never turn retention into an unbounded write or a
        cross-tenant mutation. Events are removed with their parent trace so
        the child table cannot outlive it.

        Returns the number of traces removed.
        """
        if retention_days < 1 or retention_days > 3_650:
            raise PlatformValidationError("Trace retention window is invalid.")
        if limit < 1 or limit > 5_000:
            raise PlatformValidationError("Trace purge limit is invalid.")
        if tenant_id is not None and not str(tenant_id).strip():
            raise PlatformValidationError("Trace purge tenant is invalid.")
        clauses = ["created_at <= datetime('now','-' || ? || ' days')"]
        params: list[Any] = [int(retention_days)]
        if tenant_id is not None:
            clauses.append("tenant_id=?")
            params.append(str(tenant_id))
        with self._connection() as db, db:
            rows = db.execute(
                f"SELECT trace_id FROM platform_operation_traces WHERE {' AND '.join(clauses)}"
                " ORDER BY created_at,trace_id LIMIT ?",
                [*params, limit],
            ).fetchall()
            if not rows:
                return 0
            trace_ids = [str(row["trace_id"]) for row in rows]
            placeholders = ",".join("?" for _ in trace_ids)
            db.execute(
                f"DELETE FROM platform_operation_trace_events WHERE trace_id IN ({placeholders})",
                trace_ids,
            )
            return int(db.execute(
                f"DELETE FROM platform_operation_traces WHERE trace_id IN ({placeholders})",
                trace_ids,
            ).rowcount)

    def _assert_tenant(self, context: CapabilityInvocationContext) -> None:
        if self.tenant_id is not None and self.tenant_id != context.tenant_id:
            raise PlatformValidationError("Operation trace tenant does not match invocation context.")

    @staticmethod
    def _validate_operation(operation: str) -> None:
        if not operation or len(operation) > 256:
            raise PlatformValidationError("Trace operation is invalid.")


__all__ = ["DEFAULT_RETENTION_DAYS", "OperationTraceService"]
