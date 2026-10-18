"""Tenant-scoped provider-neutral bindings for canonical Lynewright resources."""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any, Literal

from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformStore,
    PlatformValidationError,
    _redact,
)


BindingState = Literal[
    "PENDING", "DISCOVERING", "PROVISIONING", "SETUP_REQUIRED", "VERIFYING",
    "READY", "DEGRADED", "RETIRING", "DETACHED", "FAILED",
]
OwnershipMode = Literal["managed", "linked", "observed"]
_STATES = frozenset({
    "PENDING", "DISCOVERING", "PROVISIONING", "SETUP_REQUIRED", "VERIFYING",
    "READY", "DEGRADED", "RETIRING", "DETACHED", "FAILED",
})
_OWNERSHIP = frozenset({"managed", "linked", "observed"})
_MAX_ID = 256
_MAX_URL = 2_000
_MAX_FINGERPRINT = 256


class ResourceBindingService:
    """Owns the lifecycle metadata joining one canonical resource to a provider."""

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
            CREATE TABLE IF NOT EXISTS platform_resource_bindings (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT '',
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                role TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                connection_id TEXT NOT NULL DEFAULT '',
                external_resource_id TEXT NOT NULL DEFAULT '',
                external_resource_url TEXT NOT NULL DEFAULT '',
                ownership_mode TEXT NOT NULL CHECK (ownership_mode IN ('managed','linked','observed')),
                state TEXT NOT NULL CHECK (state IN ('PENDING','DISCOVERING','PROVISIONING','SETUP_REQUIRED','VERIFYING','READY','DEGRADED','RETIRING','DETACHED','FAILED')),
                desired_fingerprint TEXT NOT NULL DEFAULT '',
                observed_fingerprint TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 1,
                last_synced_at TEXT,
                last_verified_at TEXT,
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                retired_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_bindings_active_logical
              ON platform_resource_bindings(tenant_id,resource_type,resource_id,role,integration_id,connection_id)
              WHERE state NOT IN ('DETACHED','RETIRING');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_bindings_external
              ON platform_resource_bindings(tenant_id,integration_id,connection_id,resource_type,external_resource_id)
              WHERE external_resource_id <> '' AND state NOT IN ('DETACHED','RETIRING');
            CREATE INDEX IF NOT EXISTS idx_resource_bindings_scope
              ON platform_resource_bindings(tenant_id,project_id,resource_type,resource_id,role,state);
            CREATE INDEX IF NOT EXISTS idx_resource_bindings_provider
              ON platform_resource_bindings(tenant_id,integration_id,connection_id,state,updated_at DESC);
            """
        )

    def create(
        self,
        *,
        project_id: str | None,
        resource_type: str,
        resource_id: str,
        role: str,
        integration_id: str,
        connection_id: str | None = None,
        ownership_mode: OwnershipMode = "managed",
        state: BindingState = "PENDING",
        external_resource_id: str = "",
        external_resource_url: str = "",
        desired_fingerprint: str = "",
        metadata: dict[str, Any] | None = None,
        db: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        self._validate(resource_type, resource_id, role, integration_id)
        if ownership_mode not in _OWNERSHIP or state not in _STATES:
            raise PlatformValidationError("Resource binding ownership mode or state is invalid.")
        if (
            len(str(project_id or "")) > _MAX_ID
            or len(str(connection_id or "")) > _MAX_ID
            or len(str(external_resource_id or "")) > _MAX_ID
            or len(str(external_resource_url or "")) > _MAX_URL
            or len(str(desired_fingerprint or "")) > _MAX_FINGERPRINT
        ):
            raise PlatformValidationError("Resource binding optional fields exceed their bounds.")
        safe_metadata = self._metadata(metadata)
        values = (
            f"bnd_{secrets.token_hex(12)}", self.tenant_id, project_id or "", resource_type,
            resource_id, role, integration_id, connection_id or "", external_resource_id,
            external_resource_url, ownership_mode, state, desired_fingerprint,
            json.dumps(safe_metadata, ensure_ascii=False, separators=(",", ":")),
        )
        if db is not None:
            self.ensure_schema(db)
            return self._insert(db, values)
        with self._connection() as connection, connection:
            return self._insert(connection, values)

    def find(
        self,
        *,
        resource_type: str,
        resource_id: str,
        role: str,
        integration_id: str,
        connection_id: str | None = None,
    ) -> dict[str, Any] | None:
        self._validate(resource_type, resource_id, role, integration_id)
        if connection_id is not None and len(str(connection_id)) > _MAX_ID:
            raise PlatformValidationError("Resource binding connection filter is invalid.")
        with self._connection() as db:
            row = db.execute(
                """SELECT * FROM platform_resource_bindings
                WHERE tenant_id=? AND resource_type=? AND resource_id=? AND role=?
                  AND integration_id=? AND connection_id=?
                  AND state NOT IN ('DETACHED','RETIRING')""",
                (self.tenant_id, resource_type, resource_id, role, integration_id, connection_id or ""),
            ).fetchone()
        return self._public(row) if row else None

    def get(self, binding_id: str) -> dict[str, Any]:
        if not binding_id or len(binding_id) > _MAX_ID:
            raise PlatformValidationError("Resource binding id is invalid.")
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM platform_resource_bindings WHERE tenant_id=? AND id=?",
                (self.tenant_id, binding_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Resource binding was not found.")
        return self._public(row)

    def list(self, *, project_id: str | None = None, resource_type: str | None = None, resource_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["tenant_id=?"]
        params: list[Any] = [self.tenant_id]
        for field, value in (("project_id", project_id), ("resource_type", resource_type), ("resource_id", resource_id)):
            if value is not None:
                if len(str(value)) > _MAX_ID:
                    raise PlatformValidationError(f"Resource binding {field} filter is invalid.")
                clauses.append(f"{field}=?")
                params.append(value)
        with self._connection() as db:
            rows = db.execute(
                f"SELECT * FROM platform_resource_bindings WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC,id",
                params,
            ).fetchall()
        return [self._public(row) for row in rows]

    def transition(
        self,
        binding_id: str,
        *,
        state: BindingState,
        expected_revision: int,
        external_resource_id: str | None = None,
        external_resource_url: str | None = None,
        observed_fingerprint: str | None = None,
        error_code: str = "",
        error_message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state not in _STATES or expected_revision < 1:
            raise PlatformValidationError("Resource binding transition is invalid.")
        if (
            external_resource_id is not None and len(str(external_resource_id)) > _MAX_ID
            or external_resource_url is not None and len(str(external_resource_url)) > _MAX_URL
            or observed_fingerprint is not None and len(str(observed_fingerprint)) > _MAX_FINGERPRINT
            or len(str(error_code)) > 120
            or len(str(error_message)) > 2_000
        ):
            raise PlatformValidationError("Resource binding transition fields exceed their bounds.")
        assignments = ["state=?", "revision=revision+1", "updated_at=CURRENT_TIMESTAMP", "last_error_code=?", "last_error_message=?"]
        params: list[Any] = [state, error_code[:120], error_message[:2_000]]
        for column, value in (
            ("external_resource_id", external_resource_id),
            ("external_resource_url", external_resource_url),
            ("observed_fingerprint", observed_fingerprint),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                params.append(str(value))
        if state == "READY":
            assignments += ["last_verified_at=CURRENT_TIMESTAMP", "last_synced_at=CURRENT_TIMESTAMP"]
        if metadata is not None:
            assignments.append("metadata_json=?")
            params.append(json.dumps(self._metadata(metadata), ensure_ascii=False, separators=(",", ":")))
        params += [self.tenant_id, binding_id, expected_revision]
        with self._connection() as db, db:
            changed = db.execute(
                f"UPDATE platform_resource_bindings SET {', '.join(assignments)} WHERE tenant_id=? AND id=? AND revision=?",
                params,
            ).rowcount
            if not changed:
                raise PlatformConflictError("Resource binding revision is stale or binding is missing.")
        return self.get(binding_id)

    def _insert(self, db: sqlite3.Connection, values: tuple[Any, ...]) -> dict[str, Any]:
        try:
            db.execute(
                """INSERT INTO platform_resource_bindings
                (id,tenant_id,project_id,resource_type,resource_id,role,integration_id,connection_id,
                 external_resource_id,external_resource_url,ownership_mode,state,desired_fingerprint,metadata_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise PlatformConflictError("An active resource binding already exists.") from exc
        row = db.execute(
            "SELECT * FROM platform_resource_bindings WHERE tenant_id=? AND id=?",
            (self.tenant_id, values[0]),
        ).fetchone()
        if row is None:
            raise PlatformConflictError("Resource binding could not be read after insert.")
        return self._public(row)

    @staticmethod
    def _validate(*values: str) -> None:
        if any(not value or len(value) > _MAX_ID for value in values):
            raise PlatformValidationError("Resource binding identifiers are invalid.")

    @staticmethod
    def _metadata(value: dict[str, Any] | None) -> dict[str, Any]:
        try:
            encoded = json.dumps(value or {}, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise PlatformValidationError("Resource binding metadata must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise PlatformValidationError("Resource binding metadata exceeds the 64 KiB limit.")
        return _redact(value or {})

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            result["metadata"] = {}
        return result


__all__ = ["BindingState", "OwnershipMode", "ResourceBindingService"]
