"""Bounded projections for resource drift and operator attention."""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

from app.services.platform_services import PlatformStore, PlatformValidationError, _redact
from app.services.resource_bindings import ResourceBindingService
from app.services.resource_outbox import ResourceOutboxService


class ResourceReconciliationService:
    """Projects observable lifecycle problems without changing canonical truth."""

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
            CREATE TABLE IF NOT EXISTS platform_resource_attention (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
                binding_id TEXT NOT NULL DEFAULT '',
                outbox_id TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_attention_fingerprint
              ON platform_resource_attention(tenant_id, fingerprint);
            CREATE INDEX IF NOT EXISTS idx_resource_attention_open
              ON platform_resource_attention(tenant_id,status,severity,updated_at DESC);
            """
        )

    def reconcile(self, *, bindings: ResourceBindingService, outbox: ResourceOutboxService, limit: int = 200) -> dict[str, int]:
        if limit < 1 or limit > 1_000:
            raise PlatformValidationError("Reconciliation limit is invalid.")
        observed: dict[str, dict[str, Any]] = {}
        for binding in bindings.list():
            desired, actual = str(binding.get("desired_fingerprint") or ""), str(binding.get("observed_fingerprint") or "")
            if desired and actual and desired != actual:
                self._upsert("binding:%s:drift" % binding["id"], "drift", "warning", binding_id=binding["id"], code="binding_drift", message="Desired and observed resource state differ.", payload={"desired_fingerprint": desired, "observed_fingerprint": actual})
            if binding.get("state") in {"SETUP_REQUIRED", "DEGRADED", "FAILED", "RETIRING"}:
                self._upsert("binding:%s:state" % binding["id"], "binding_state", "error" if binding["state"] in {"FAILED", "DEGRADED"} else "warning", binding_id=binding["id"], code="binding_attention", message="Resource binding requires operator attention.", payload={"state": binding["state"], "last_error_code": binding.get("last_error_code", "")})
        for item in outbox.list(limit=limit):
            if item.get("status") == "dead_letter":
                self._upsert("outbox:%s:dead_letter" % item["id"], "outbox_failure", "error", outbox_id=item["id"], code="outbox_dead_letter", message="Resource lifecycle work was dead-lettered.", payload={"event_type": item.get("event_type"), "last_error_code": item.get("last_error_code", "")})
            elif str(item.get("event_type") or "").endswith(".deleted") and item.get("status") in {"pending", "processing"}:
                self._upsert("outbox:%s:tombstone" % item["id"], "tombstone_pending", "warning", outbox_id=item["id"], code="tombstone_pending", message="Resource deletion is awaiting external detach/retire delivery.", payload={"status": item.get("status"), "event_type": item.get("event_type")})
        with self._connection() as db:
            rows = db.execute("SELECT status,COUNT(*) AS count FROM platform_resource_attention WHERE tenant_id=? GROUP BY status", (self.tenant_id,)).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def list_open(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1_000:
            raise PlatformValidationError("Attention list limit is invalid.")
        with self._connection() as db:
            rows = db.execute("SELECT * FROM platform_resource_attention WHERE tenant_id=? AND status='open' ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,updated_at DESC LIMIT ?", (self.tenant_id, limit)).fetchall()
        return [self._public(row) for row in rows]

    def _upsert(self, fingerprint: str, kind: str, severity: str, *, binding_id: str = "", outbox_id: str = "", code: str, message: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(_redact(payload), ensure_ascii=False, separators=(",", ":"))
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_resource_attention
                (id,tenant_id,fingerprint,kind,severity,binding_id,outbox_id,code,message,payload_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,fingerprint) DO UPDATE SET
                  severity=excluded.severity,code=excluded.code,message=excluded.message,
                  payload_json=excluded.payload_json,status='open',resolved_at=NULL,updated_at=CURRENT_TIMESTAMP""",
                (f"att_{secrets.token_hex(12)}", self.tenant_id, fingerprint, kind, severity, binding_id, outbox_id, code, message, encoded),
            )

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["payload"] = json.loads(result.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            result["payload"] = {}
        return result


__all__ = ["ResourceReconciliationService"]
