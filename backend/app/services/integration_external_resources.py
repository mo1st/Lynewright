"""Tenant-scoped projection of resources currently exposed by integrations.

The inventory is deliberately separate from ``ResourceBindingService``. It is
safe to refresh, expire, and rebuild this cache without changing an operator's
intentional canonical-resource relationships.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from app.services.platform_services import (
    PlatformNotFoundError,
    PlatformStore,
    PlatformValidationError,
    _redact,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID


_STATES = frozenset({"ACTIVE", "STALE", "REMOTE_MISSING"})
_MAX_TEXT = 2_000
_MAX_ID = 256
_MAX_METADATA_BYTES = 64 * 1024


class ExternalResourceInventoryService:
    """Owns the bounded, refreshable remote-resource inventory projection."""

    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
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
            CREATE TABLE IF NOT EXISTS platform_integration_external_resources (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                parent_external_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                remote_status TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE','STALE','REMOTE_MISSING')),
                observed_fingerprint TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                available_actions_json TEXT NOT NULL DEFAULT '[]',
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT,
                last_discovered_at TEXT,
                stale_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (tenant_id,integration_id,connection_id,resource_type,external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_external_resources_scope
              ON platform_integration_external_resources(tenant_id,integration_id,connection_id,resource_type,state,name);
            """
        )

    def list(
        self,
        *,
        integration_id: str,
        connection_id: str | None = None,
        resource_type: str | None = None,
        search: str | None = None,
        cursor: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._validate_scope(integration_id, connection_id, resource_type)
        if cursor < 0 or limit < 1 or limit > 500:
            raise PlatformValidationError("External resource pagination is invalid.")
        search_value = (search or "").strip()
        if len(search_value) > 200:
            raise PlatformValidationError("External resource search is too long.")
        clauses = ["tenant_id=?", "integration_id=?"]
        params: list[Any] = [self.tenant_id, integration_id]
        if connection_id is not None:
            clauses.append("connection_id=?")
            params.append(connection_id)
        if resource_type is not None:
            clauses.append("resource_type=?")
            params.append(resource_type)
        if search_value:
            clauses.append("(name LIKE ? OR external_id LIKE ? OR url LIKE ?)")
            term = f"%{search_value}%"
            params.extend([term, term, term])
        params.extend([limit + 1, cursor])
        with self._connection() as db:
            rows = db.execute(
                f"SELECT * FROM platform_integration_external_resources WHERE {' AND '.join(clauses)} ORDER BY resource_type,name,external_id LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        has_more = len(rows) > limit
        items = [self._public(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": str(cursor + limit) if has_more else None}

    def get(self, inventory_id: str) -> dict[str, Any]:
        if not inventory_id or len(inventory_id) > _MAX_ID:
            raise PlatformValidationError("External resource id is invalid.")
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM platform_integration_external_resources WHERE tenant_id=? AND id=?",
                (self.tenant_id, inventory_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("External resource was not found.")
        return self._public(row)

    def refresh(
        self,
        *,
        integration_id: str,
        connection_id: str,
        resource_type: str,
        resources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically upsert a successful discovery and stale unseen rows.

        Callers must invoke this only after a remote discovery succeeds. A
        failed request therefore leaves the previous projection untouched.
        """
        self._validate_scope(integration_id, connection_id, resource_type)
        if not isinstance(resources, list) or len(resources) > 10_000:
            raise PlatformValidationError("External resource discovery returned too many resources.")
        normalized = [self._normalize(item, resource_type) for item in resources]
        seen_ids = [item["external_id"] for item in normalized]
        if len(set(seen_ids)) != len(seen_ids):
            raise PlatformValidationError("External resource discovery returned duplicate ids.")
        with self._connection() as db, db:
            for item in normalized:
                db.execute(
                    """INSERT INTO platform_integration_external_resources
                    (id,tenant_id,integration_id,connection_id,external_id,resource_type,parent_external_id,
                     name,url,remote_status,state,observed_fingerprint,metadata_json,available_actions_json,
                     first_seen_at,last_seen_at,last_discovered_at,stale_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,NULL)
                    ON CONFLICT(tenant_id,integration_id,connection_id,resource_type,external_id) DO UPDATE SET
                     parent_external_id=excluded.parent_external_id,name=excluded.name,url=excluded.url,
                     remote_status=excluded.remote_status,state='ACTIVE',observed_fingerprint=excluded.observed_fingerprint,
                     metadata_json=excluded.metadata_json,available_actions_json=excluded.available_actions_json,
                     last_seen_at=CURRENT_TIMESTAMP,last_discovered_at=CURRENT_TIMESTAMP,stale_at=NULL,
                     updated_at=CURRENT_TIMESTAMP""",
                    (
                        f"xres_{secrets.token_hex(12)}", self.tenant_id, integration_id, connection_id,
                        item["external_id"], resource_type, item["parent_external_id"], item["name"],
                        item["url"], item["remote_status"], "ACTIVE", item["observed_fingerprint"],
                        json.dumps(item["metadata"], ensure_ascii=False, separators=(",", ":")),
                        json.dumps(item["available_actions"], separators=(",", ":")),
                    ),
                )
            if seen_ids:
                placeholders = ",".join("?" for _ in seen_ids)
                params: list[Any] = [self.tenant_id, integration_id, connection_id, resource_type, *seen_ids]
                db.execute(
                    f"""UPDATE platform_integration_external_resources
                       SET state='STALE',stale_at=COALESCE(stale_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP
                       WHERE tenant_id=? AND integration_id=? AND connection_id=? AND resource_type=?
                         AND external_id NOT IN ({placeholders}) AND state='ACTIVE'""",
                    params,
                )
            else:
                db.execute(
                    """UPDATE platform_integration_external_resources
                       SET state='STALE',stale_at=COALESCE(stale_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP
                       WHERE tenant_id=? AND integration_id=? AND connection_id=? AND resource_type=? AND state='ACTIVE'""",
                    (self.tenant_id, integration_id, connection_id, resource_type),
                )
        return self.list(integration_id=integration_id, connection_id=connection_id, resource_type=resource_type)

    def mark_remote_missing(self, inventory_id: str, *, action: str) -> dict[str, Any]:
        """Record a confirmed remote retirement without deleting the projection."""
        if not action or len(action) > 80:
            raise PlatformValidationError("Remote action is invalid.")
        with self._connection() as db, db:
            changed = db.execute(
                """UPDATE platform_integration_external_resources
                   SET state='REMOTE_MISSING',remote_status=?,stale_at=COALESCE(stale_at,CURRENT_TIMESTAMP),
                       updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND id=?""",
                (f"removed:{action}", self.tenant_id, inventory_id),
            ).rowcount
        if not changed:
            raise PlatformNotFoundError("External resource was not found.")
        return self.get(inventory_id)

    def remove_integration(self, integration_id: str) -> int:
        """Delete the local inventory projection during a confirmed uninstall.

        This is never a remote action and does not affect bindings; callers must
        enforce their own no-active-binding guard before using it.
        """
        if not integration_id or len(integration_id) > _MAX_ID:
            raise PlatformValidationError("Integration id is invalid.")
        with self._connection() as db, db:
            return db.execute(
                "DELETE FROM platform_integration_external_resources WHERE tenant_id=? AND integration_id=?",
                (self.tenant_id, integration_id),
            ).rowcount

    @staticmethod
    def _normalize(value: dict[str, Any], resource_type: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PlatformValidationError("External resource entries must be objects.")
        external_id = str(value.get("external_id") or value.get("id") or value.get("website_id") or "").strip()
        if not external_id or len(external_id) > _MAX_ID:
            raise PlatformValidationError("External resource entries require a bounded external_id.")
        metadata = _redact(value.get("metadata") if isinstance(value.get("metadata"), dict) else {})
        try:
            if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > _MAX_METADATA_BYTES:
                raise PlatformValidationError("External resource metadata exceeds the 64 KiB limit.")
        except (TypeError, ValueError, RecursionError) as exc:
            raise PlatformValidationError("External resource metadata must be JSON serializable.") from exc
        actions = value.get("available_actions", value.get("actions", []))
        if not isinstance(actions, list) or any(not isinstance(action, str) for action in actions):
            raise PlatformValidationError("External resource available actions must be a list.")
        return {
            "external_id": external_id,
            "parent_external_id": str(value.get("parent_external_id") or value.get("parent_id") or "")[:_MAX_ID],
            "name": str(value.get("name") or value.get("label") or value.get("title") or external_id)[:_MAX_TEXT],
            "url": str(value.get("url") or value.get("external_resource_url") or "")[:_MAX_TEXT],
            "remote_status": str(value.get("remote_status") or value.get("status") or "")[:120],
            "observed_fingerprint": str(value.get("observed_fingerprint") or value.get("fingerprint") or "")[:256],
            "metadata": metadata,
            "available_actions": sorted(set(str(action)[:80] for action in actions)),
            "resource_type": resource_type,
        }

    @staticmethod
    def _validate_scope(integration_id: str, connection_id: str | None, resource_type: str | None) -> None:
        if not integration_id or len(integration_id) > _MAX_ID:
            raise PlatformValidationError("External resource integration scope is invalid.")
        if connection_id is not None and (not connection_id or len(connection_id) > _MAX_ID):
            raise PlatformValidationError("External resource connection scope is invalid.")
        if resource_type is not None and (not resource_type or len(resource_type) > 120):
            raise PlatformValidationError("External resource type is invalid.")

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        result["available_actions"] = json.loads(result.pop("available_actions_json") or "[]")
        result["binding_status"] = "UNBOUND"
        return result


__all__ = ["ExternalResourceInventoryService"]
