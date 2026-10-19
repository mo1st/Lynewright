"""Namespaced non-secret state primitives for generic integrations."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from app.services.platform_services import PlatformNotFoundError, PlatformStore, PlatformValidationError
from app.services.tenant_schema import DEFAULT_TENANT_ID


_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_MAX_JSON_BYTES = 64 * 1024
_SENSITIVE_KEY = re.compile(r"(secret|token|password|api[_-]?key)", re.IGNORECASE)


class IntegrationStateService:
    """Adapter-owned KV, mapping, and sync state without Core DB exposure."""

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
            CREATE TABLE IF NOT EXISTS platform_integration_kv (
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                connection_id TEXT NOT NULL DEFAULT '',
                project_id TEXT NOT NULL DEFAULT '',
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, integration_id, connection_id, project_id, key)
            );
            CREATE TABLE IF NOT EXISTS platform_integration_mappings (
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                connection_id TEXT NOT NULL DEFAULT '',
                project_id TEXT NOT NULL DEFAULT '',
                external_type TEXT NOT NULL,
                external_id TEXT NOT NULL,
                lynewright_type TEXT NOT NULL,
                lynewright_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, integration_id, connection_id, project_id, external_type, external_id)
            );
            CREATE TABLE IF NOT EXISTS platform_integration_sync_state (
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                connection_id TEXT NOT NULL DEFAULT '',
                project_id TEXT NOT NULL DEFAULT '',
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, integration_id, connection_id, project_id)
            );
            """
        )
        mapping_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(platform_integration_mappings)").fetchall()}
        if "project_id" not in mapping_columns:
            # SQLite cannot add a column to an existing primary key. Preserve
            # legacy global mappings by rebuilding this host-owned table.
            db.executescript(
                """
                CREATE TABLE platform_integration_mappings_v2 (
                    tenant_id TEXT NOT NULL,
                    integration_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    external_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    lynewright_type TEXT NOT NULL,
                    lynewright_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, integration_id, connection_id, project_id, external_type, external_id)
                );
                INSERT INTO platform_integration_mappings_v2
                (tenant_id,integration_id,connection_id,project_id,external_type,external_id,lynewright_type,lynewright_id,metadata_json,updated_at)
                SELECT tenant_id,integration_id,connection_id,'',external_type,external_id,lynewright_type,lynewright_id,metadata_json,updated_at
                FROM platform_integration_mappings;
                DROP TABLE platform_integration_mappings;
                ALTER TABLE platform_integration_mappings_v2 RENAME TO platform_integration_mappings;
                """
            )

    def get_value(self, scope: dict[str, str], key: str) -> Any:
        self._validate_key(key)
        with self._connection() as db:
            row = db.execute(
                "SELECT value_json FROM platform_integration_kv WHERE tenant_id=? AND integration_id=? AND connection_id=? AND project_id=? AND key=?",
                (*self._scope(scope), key),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Integration storage value not found.")
        return json.loads(row["value_json"])

    def set_value(self, scope: dict[str, str], key: str, value: Any) -> Any:
        self._validate_key(key)
        encoded = self._safe_json(value, label="Integration storage value")
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_integration_kv (tenant_id,integration_id,connection_id,project_id,key,value_json)
                VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id,integration_id,connection_id,project_id,key)
                DO UPDATE SET value_json=excluded.value_json,updated_at=CURRENT_TIMESTAMP""",
                (*self._scope(scope), key, encoded),
            )
        return value

    def delete_value(self, scope: dict[str, str], key: str) -> None:
        self._validate_key(key)
        with self._connection() as db, db:
            if db.execute(
                "DELETE FROM platform_integration_kv WHERE tenant_id=? AND integration_id=? AND connection_id=? AND project_id=? AND key=?",
                (*self._scope(scope), key),
            ).rowcount != 1:
                raise PlatformNotFoundError("Integration storage value not found.")

    def list_values(self, scope: dict[str, str]) -> list[str]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT key FROM platform_integration_kv WHERE tenant_id=? AND integration_id=? AND connection_id=? AND project_id=? ORDER BY key",
                self._scope(scope),
            ).fetchall()
        return [str(row["key"]) for row in rows]

    def get_mapping(self, scope: dict[str, str], external_type: str, external_id: str) -> dict[str, Any]:
        self._validate_key(external_type); self._validate_key(external_id)
        with self._connection() as db:
            row = db.execute(
                """SELECT * FROM platform_integration_mappings WHERE tenant_id=? AND integration_id=? AND connection_id=? AND project_id=?
                AND external_type=? AND external_id=?""",
                (*self._scope(scope), external_type, external_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Integration mapping not found.")
        return self._mapping(row)

    def put_mapping(self, scope: dict[str, str], *, external_type: str, external_id: str, lynewright_type: str, lynewright_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        for value in (external_type, external_id, lynewright_type, lynewright_id): self._validate_key(value)
        encoded = self._safe_json(metadata or {}, label="Integration mapping metadata")
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_integration_mappings (tenant_id,integration_id,connection_id,project_id,external_type,external_id,lynewright_type,lynewright_id,metadata_json)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,integration_id,connection_id,project_id,external_type,external_id)
                DO UPDATE SET lynewright_type=excluded.lynewright_type,lynewright_id=excluded.lynewright_id,metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP""",
                (*self._scope(scope), external_type, external_id, lynewright_type, lynewright_id, encoded),
            )
        return self.get_mapping(scope, external_type, external_id)

    def find_mapping_by_lynewright(self, scope: dict[str, str], lynewright_type: str, lynewright_id: str) -> dict[str, Any]:
        self._validate_key(lynewright_type); self._validate_key(lynewright_id)
        with self._connection() as db:
            row = db.execute(
                """SELECT * FROM platform_integration_mappings WHERE tenant_id=? AND integration_id=? AND connection_id=? AND project_id=?
                AND lynewright_type=? AND lynewright_id=? ORDER BY updated_at DESC LIMIT 1""",
                (*self._scope(scope), lynewright_type, lynewright_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Integration mapping not found.")
        return self._mapping(row)

    def get_sync_state(self, scope: dict[str, str]) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT state_json,updated_at FROM platform_integration_sync_state WHERE tenant_id=? AND integration_id=? AND connection_id=? AND project_id=?",
                self._scope(scope),
            ).fetchone()
        return {"state": json.loads(row["state_json"]) if row else {}, "updated_at": row["updated_at"] if row else None}

    def set_sync_state(self, scope: dict[str, str], state: dict[str, Any]) -> dict[str, Any]:
        encoded = self._safe_json(state, label="Integration sync state")
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_integration_sync_state (tenant_id,integration_id,connection_id,project_id,state_json)
                VALUES (?,?,?,?,?) ON CONFLICT(tenant_id,integration_id,connection_id,project_id)
                DO UPDATE SET state_json=excluded.state_json,updated_at=CURRENT_TIMESTAMP""",
                (*self._scope(scope), encoded),
            )
        return self.get_sync_state(scope)

    def set_sync_checkpoint(self, scope: dict[str, str], *, cursor: str = "", etag: str = "", revision: str = "", incremental: bool = True) -> dict[str, Any]:
        for label, value in {"cursor": cursor, "etag": etag, "revision": revision}.items():
            if not isinstance(value, str) or len(value) > 4_000:
                raise PlatformValidationError(f"Integration sync {label} must be a string up to 4,000 characters.")
        return self.set_sync_state(scope, {"cursor": cursor, "etag": etag, "revision": revision, "incremental": bool(incremental)})

    def _scope(self, scope: dict[str, str]) -> tuple[str, str, str, str]:
        integration_id = str(scope.get("integration_id") or "")
        connection_id = str(scope.get("connection_id") or "")
        project_id = str(scope.get("project_id") or "")
        if not integration_id:
            raise PlatformValidationError("Integration state requires an integration identity.")
        return self.tenant_id, integration_id, connection_id, project_id

    @staticmethod
    def _validate_key(value: str) -> None:
        if not _KEY_RE.fullmatch(str(value)):
            raise PlatformValidationError("Integration state keys must use a bounded identifier.")

    @staticmethod
    def _safe_json(value: Any, *, label: str) -> str:
        IntegrationStateService._reject_secrets(value)
        try:
            encoded = json.dumps(value, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError(f"{label} must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
            raise PlatformValidationError(f"{label} exceeds the 64 KiB limit.")
        return encoded

    @staticmethod
    def _reject_secrets(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if _SENSITIVE_KEY.search(str(key)):
                    raise PlatformValidationError("Integration state must not store secret values.")
                IntegrationStateService._reject_secrets(nested)
        elif isinstance(value, list):
            for nested in value: IntegrationStateService._reject_secrets(nested)

    @staticmethod
    def _mapping(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
        return value


class IntegrationScheduleStateService:
    """Host-owned durable checkpoints for scheduled integration contributions."""

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
            CREATE TABLE IF NOT EXISTS platform_integration_schedule_state (
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                connection_id TEXT NOT NULL DEFAULT '',
                contribution_id TEXT NOT NULL,
                next_due_at_epoch REAL NOT NULL DEFAULT 0,
                last_run_at_epoch REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, integration_id, connection_id, contribution_id)
            );
            """
        )

    def get(self, *, integration_id: str, connection_id: str | None, contribution_id: str) -> dict[str, float] | None:
        with self._connection() as db:
            row = db.execute(
                """SELECT next_due_at_epoch,last_run_at_epoch
                FROM platform_integration_schedule_state
                WHERE tenant_id=? AND integration_id=? AND connection_id=? AND contribution_id=?""",
                (self.tenant_id, integration_id, connection_id or "", contribution_id),
            ).fetchone()
        if row is None:
            return None
        return {"next_due_at_epoch": float(row["next_due_at_epoch"]), "last_run_at_epoch": float(row["last_run_at_epoch"])}

    def checkpoint(
        self,
        *,
        integration_id: str,
        connection_id: str | None,
        contribution_id: str,
        next_due_at_epoch: float,
        last_run_at_epoch: float,
    ) -> None:
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_integration_schedule_state
                (tenant_id,integration_id,connection_id,contribution_id,next_due_at_epoch,last_run_at_epoch)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(tenant_id,integration_id,connection_id,contribution_id)
                DO UPDATE SET next_due_at_epoch=excluded.next_due_at_epoch,
                    last_run_at_epoch=excluded.last_run_at_epoch,updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, integration_id, connection_id or "", contribution_id, next_due_at_epoch, last_run_at_epoch),
            )
