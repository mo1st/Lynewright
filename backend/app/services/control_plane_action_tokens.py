"""Short-lived, one-time action tokens for Control Plane mutations."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
import json
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformStore, PlatformValidationError


class ControlPlaneActionTokenService:
    def __init__(self, store: PlatformStore | None = None) -> None:
        self.store = store or PlatformStore()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_control_plane_action_tokens (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                nonce_hash TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT '',
                resource_type TEXT NOT NULL DEFAULT '',
                resource_id TEXT NOT NULL DEFAULT '',
                integration_id TEXT NOT NULL DEFAULT '',
                connection_id TEXT NOT NULL DEFAULT '',
                surface_path TEXT NOT NULL,
                component_id TEXT NOT NULL,
                action TEXT NOT NULL,
                capability_id TEXT NOT NULL,
                context_revision TEXT NOT NULL DEFAULT '',
                expires_at_epoch REAL NOT NULL,
                consumed_at_epoch REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_control_plane_action_token_scope
              ON platform_control_plane_action_tokens(tenant_id,expires_at_epoch,consumed_at_epoch);
            """
        )

    def issue(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        surface_path: str,
        component_id: str,
        action: str,
        capability_id: str,
        project_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        integration_id: str | None = None,
        connection_id: str | None = None,
        context_revision: str | None = None,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        required_values = [tenant_id, actor_id, surface_path, component_id, action, capability_id]
        if any(not str(value or "").strip() or len(str(value)) > 256 for value in required_values):
            raise PlatformValidationError("Action token scope fields are invalid.")
        optional_values = {
            "project_id": project_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "integration_id": integration_id,
            "connection_id": connection_id,
            "context_revision": context_revision,
        }
        if any(len(str(value or "")) > 256 for value in optional_values.values()):
            raise PlatformValidationError("Action token scope fields are invalid.")
        if ttl_seconds < 1 or ttl_seconds > 600:
            raise PlatformValidationError("Action token TTL must be between 1 and 600 seconds.")
        raw = f"cpat_{secrets.token_urlsafe(32)}"
        nonce = secrets.token_urlsafe(18)
        expires = time.time() + ttl_seconds
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_control_plane_action_tokens
                (id,token_hash,nonce_hash,tenant_id,actor_id,project_id,resource_type,resource_id,
                 integration_id,connection_id,surface_path,component_id,action,capability_id,context_revision,expires_at_epoch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"cpat_{secrets.token_hex(12)}", self._hash(raw), self._hash(nonce), tenant_id, actor_id,
                 project_id or "", resource_type or "", resource_id or "", integration_id or "", connection_id or "",
                 surface_path, component_id, action, capability_id, context_revision or "", expires),
            )
        return {"token": raw, "nonce": nonce, "expires_at_epoch": expires, "tenant_id": tenant_id, "component_id": component_id, "action": action}

    def consume(self, token: str, *, tenant_id: str, actor_id: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not token or len(token) > 256 or not tenant_id:
            raise PlatformValidationError("Action token is invalid.")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM platform_control_plane_action_tokens WHERE token_hash=? AND tenant_id=?", (self._hash(token), tenant_id)).fetchone()
            if row is None:
                db.rollback()
                raise PlatformNotFoundError("Action token was not found.")
            if row["consumed_at_epoch"] is not None or float(row["expires_at_epoch"]) <= time.time():
                db.rollback()
                raise PlatformConflictError("Action token is expired or already consumed.")
            if actor_id is not None and str(row["actor_id"]) != str(actor_id):
                db.rollback()
                raise PlatformConflictError("Action token actor scope does not match.")
            expected = context or {}
            for column in ("project_id", "resource_type", "resource_id", "integration_id", "connection_id", "surface_path", "component_id", "action", "capability_id", "context_revision"):
                if column in expected and str(expected[column] or "") != str(row[column] or ""):
                    db.rollback()
                    raise PlatformConflictError("Action token context scope does not match.")
            changed = db.execute("UPDATE platform_control_plane_action_tokens SET consumed_at_epoch=? WHERE id=? AND consumed_at_epoch IS NULL", (time.time(), row["id"])).rowcount
            if changed != 1:
                db.rollback()
                raise PlatformConflictError("Action token was consumed concurrently.")
            db.commit()
            result = dict(row)
            result["consumed_at_epoch"] = time.time()
            result.pop("token_hash", None)
            result.pop("nonce_hash", None)
            return result

    def peek(self, token: str, *, tenant_id: str) -> dict[str, Any]:
        """Read unconsumed claims so a dispatcher can resolve policy before use."""
        if not token or len(token) > 256 or not tenant_id:
            raise PlatformValidationError("Action token is invalid.")
        with self._connection() as db:
            row = db.execute("SELECT * FROM platform_control_plane_action_tokens WHERE token_hash=? AND tenant_id=?", (self._hash(token), tenant_id)).fetchone()
        if row is None:
            raise PlatformNotFoundError("Action token was not found.")
        if row["consumed_at_epoch"] is not None or float(row["expires_at_epoch"]) <= time.time():
            raise PlatformConflictError("Action token is expired or already consumed.")
        result = dict(row)
        result.pop("token_hash", None)
        result.pop("nonce_hash", None)
        return result

    def purge_expired(self, *, tenant_id: str | None = None, limit: int = 500) -> int:
        """Delete a bounded batch of expired tokens.

        Tokens are intentionally retained until their expiry for one-time-use
        and replay diagnostics. Cleanup is bounded and optionally tenant-scoped
        so startup or an operator task cannot turn retention into an unbounded
        write or cross-tenant mutation.
        """
        if limit < 1 or limit > 5_000:
            raise PlatformValidationError("Action token purge limit is invalid.")
        if tenant_id is not None and not str(tenant_id).strip():
            raise PlatformValidationError("Action token purge tenant is invalid.")
        clauses = ["expires_at_epoch <= ?"]
        params: list[Any] = [time.time()]
        if tenant_id is not None:
            clauses.append("tenant_id=?")
            params.append(str(tenant_id))
        with self._connection() as db, db:
            rows = db.execute(
                f"SELECT id FROM platform_control_plane_action_tokens WHERE {' AND '.join(clauses)} ORDER BY expires_at_epoch,id LIMIT ?",
                [*params, limit],
            ).fetchall()
            if not rows:
                return 0
            ids = [str(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            return int(db.execute(
                f"DELETE FROM platform_control_plane_action_tokens WHERE id IN ({placeholders})",
                ids,
            ).rowcount)

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["ControlPlaneActionTokenService"]
