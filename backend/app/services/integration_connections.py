"""Tenant-scoped connection profiles for generic integrations."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, Iterator

from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import (
    CredentialService,
    HTTPService,
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID


_CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_SENSITIVE_KEY = re.compile(r"(secret|token|password|api[_-]?key)", re.IGNORECASE)
_MAX_JSON_BYTES = 64 * 1024


class ConnectionProfileService:
    """Keeps reusable adapter code separate from its configured connections."""

    def __init__(
        self,
        store: PlatformStore | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        registry: IntegrationRegistry | None = None,
        credentials: CredentialService | None = None,
        endpoint_validator: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id
        self.registry = registry or IntegrationRegistry(self.store, tenant_id=tenant_id)
        self.credentials = credentials or CredentialService(self.store)
        self.endpoint_validator = endpoint_validator or HTTPService().validate_public_url

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            self._migrate(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_integration_connections (
                tenant_id TEXT NOT NULL,
                id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                name TEXT NOT NULL,
                project_id TEXT,
                endpoint_url TEXT NOT NULL DEFAULT '',
                origin_kind TEXT NOT NULL DEFAULT 'public',
                internal_endpoint_ref TEXT NOT NULL DEFAULT '',
                credential_refs_json TEXT NOT NULL DEFAULT '[]',
                configuration_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                health_json TEXT NOT NULL DEFAULT '{"state":"UNKNOWN"}',
                last_health_check_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, id),
                UNIQUE (tenant_id, integration_id, name)
            );
            CREATE INDEX IF NOT EXISTS idx_integration_connections_scope
                ON platform_integration_connections(tenant_id, integration_id, project_id, enabled, updated_at DESC);
            """
        )

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Additive columns for deployments created before origin_kind existed.

        Existing rows take `public`, which is the origin kind they already had:
        every endpoint they hold went through public validation to get there.
        """
        columns = {row[1] for row in db.execute(
            "PRAGMA table_info(platform_integration_connections)")}
        for column, definition in (
            ("origin_kind", "TEXT NOT NULL DEFAULT 'public'"),
            ("internal_endpoint_ref", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                db.execute(
                    f"ALTER TABLE platform_integration_connections ADD COLUMN {column} {definition}")

    def list(self, integration_id: str, *, project_id: str | None = None) -> list[dict[str, Any]]:
        self.registry.get(integration_id)
        with self._connection() as db:
            rows = db.execute(
                """SELECT * FROM platform_integration_connections
                WHERE tenant_id=? AND integration_id=? AND (? IS NULL OR project_id=?)
                ORDER BY created_at,id""",
                (self.tenant_id, integration_id, project_id, project_id),
            ).fetchall()
        return [self._public(row) for row in rows]

    def get(self, integration_id: str, connection_id: str) -> dict[str, Any]:
        return self._public(self._row(integration_id, connection_id))

    def create(
        self,
        integration_id: str,
        *,
        name: str,
        endpoint_url: str = "",
        credential_refs: list[str] | None = None,
        project_id: str | None = None,
        configuration: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        connection_id: str | None = None,
        enabled: bool = True,
        origin_kind: str = "public",
        internal_endpoint_ref: str = "",
    ) -> dict[str, Any]:
        self.registry.get(integration_id)
        if not name.strip() or len(name.strip()) > 120:
            raise PlatformValidationError("Connection profile name is required and must be at most 120 characters.")
        normalized_id = connection_id or f"conn_{secrets.token_hex(12)}"
        self._validate_id(normalized_id)
        normalized_project = self._project_id(project_id)
        kind, ref, normalized_endpoint = self._origin(
            origin_kind, internal_endpoint_ref, endpoint_url)
        refs = self._credential_refs(credential_refs or [])
        config = self._safe_json(configuration, label="Connection configuration")
        safe_metadata = self._safe_json(metadata, label="Connection metadata")
        try:
            with self._connection() as db, db:
                db.execute(
                    """INSERT INTO platform_integration_connections (
                        tenant_id,id,integration_id,name,project_id,endpoint_url,
                        origin_kind,internal_endpoint_ref,
                        credential_refs_json,configuration_json,metadata_json,enabled
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self.tenant_id,
                        normalized_id,
                        integration_id,
                        name.strip(),
                        normalized_project,
                        normalized_endpoint,
                        kind,
                        ref,
                        json.dumps(refs),
                        json.dumps(config, sort_keys=True),
                        json.dumps(safe_metadata, sort_keys=True),
                        int(enabled),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PlatformConflictError("A connection with that id or name already exists for this integration.") from exc
        return self.get(integration_id, normalized_id)

    def configure(
        self,
        integration_id: str,
        connection_id: str,
        *,
        endpoint_url: str | None = None,
        credential_refs: list[str] | None = None,
        project_id: str | None = None,
        configuration: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._connection() as db, db:
            row = self._row_from_connection(db, integration_id, connection_id)
            fields: list[str] = []
            params: list[Any] = []
            if endpoint_url is not None:
                fields.append("endpoint_url=?")
                params.append(self._endpoint(endpoint_url))
            if credential_refs is not None:
                fields.append("credential_refs_json=?")
                params.append(json.dumps(self._credential_refs(credential_refs)))
            if project_id is not None:
                fields.append("project_id=?")
                params.append(self._project_id(project_id))
            if configuration is not None:
                fields.append("configuration_json=?")
                params.append(json.dumps(self._safe_json(configuration, label="Connection configuration"), sort_keys=True))
            if metadata is not None:
                fields.append("metadata_json=?")
                params.append(json.dumps(self._safe_json(metadata, label="Connection metadata"), sort_keys=True))
            if not fields:
                raise PlatformValidationError("At least one connection field must be supplied.")
            db.execute(
                f"UPDATE platform_integration_connections SET {', '.join(fields)},updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND integration_id=? AND id=?",
                (*params, self.tenant_id, integration_id, connection_id),
            )
        return self.get(integration_id, connection_id)

    def set_enabled(self, integration_id: str, connection_id: str, *, enabled: bool) -> dict[str, Any]:
        with self._connection() as db, db:
            row = self._row_from_connection(db, integration_id, connection_id)
            if bool(row["enabled"]) == enabled:
                raise PlatformOperationError("Connection is already in the requested enabled state.")
            db.execute(
                "UPDATE platform_integration_connections SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND integration_id=? AND id=?",
                (int(enabled), self.tenant_id, integration_id, connection_id),
            )
        return self.get(integration_id, connection_id)

    def record_health(
        self,
        integration_id: str,
        connection_id: str,
        *,
        state: str,
        latency_ms: int | None = None,
        remote_version: str = "",
        error_code: str = "",
    ) -> dict[str, Any]:
        valid_states = {"UNKNOWN", "UNCONFIGURED", "CONFIGURED", "HEALTHY", "DEGRADED", "UNAVAILABLE", "ERROR", "DISABLED"}
        if state not in valid_states:
            raise PlatformValidationError("Unsupported integration health state.")
        if latency_ms is not None and latency_ms < 0:
            raise PlatformValidationError("Health latency cannot be negative.")
        health = {"state": state, "latency_ms": latency_ms, "remote_version": remote_version[:120], "error_code": error_code[:100]}
        with self._connection() as db, db:
            self._row_from_connection(db, integration_id, connection_id)
            db.execute(
                """UPDATE platform_integration_connections
                SET health_json=?,last_health_check_at=CURRENT_TIMESTAMP,last_error=?,updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=? AND integration_id=? AND id=?""",
                (json.dumps(health), error_code[:500], self.tenant_id, integration_id, connection_id),
            )
        return self.get(integration_id, connection_id)

    def remove(self, integration_id: str, connection_id: str) -> None:
        with self._connection() as db, db:
            self._row_from_connection(db, integration_id, connection_id)
            db.execute(
                "DELETE FROM platform_integration_connections WHERE tenant_id=? AND integration_id=? AND id=?",
                (self.tenant_id, integration_id, connection_id),
            )

    def _row(self, integration_id: str, connection_id: str) -> sqlite3.Row:
        with self._connection() as db:
            return self._row_from_connection(db, integration_id, connection_id)

    def _row_from_connection(self, db: sqlite3.Connection, integration_id: str, connection_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM platform_integration_connections WHERE tenant_id=? AND integration_id=? AND id=?",
            (self.tenant_id, integration_id, connection_id),
        ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Integration connection not found.")
        return row

    def _origin(
        self, origin_kind: str, internal_endpoint_ref: str, endpoint_url: str,
    ) -> tuple[str, str, str]:
        """Establish where this connection may reach, and by which rule.

        A `public` connection supplies its own URL and goes through the
        outbound boundary unchanged. An `internal_endpoint` connection supplies
        only a *name*, resolved against the deployment's allowlist -- so the
        exemption is bounded by what an operator configured rather than by what
        a caller asked for.

        A supplied `endpoint_url` on an internal connection is **refused**, not
        ignored. A security-relevant field that is silently dropped reads as
        accepted, and the person who set it would have no reason to look again.
        """
        from app.services.internal_endpoints import (
            normalize_ref, resolve_internal_endpoint,
        )

        kind = str(origin_kind or "public").strip().lower()
        if kind not in {"public", "internal_endpoint"}:
            raise PlatformValidationError(
                "Connection origin kind must be 'public' or 'internal_endpoint'."
            )
        if kind == "public":
            if internal_endpoint_ref:
                raise PlatformValidationError(
                    "A public connection cannot name an internal endpoint reference."
                )
            return kind, "", self._endpoint(endpoint_url)

        ref = normalize_ref(internal_endpoint_ref)
        # Resolve now, and pin every later request to this origin. A URL may
        # select a path on the pinned service, but it cannot choose a host,
        # port, credentials, query, or fragment.
        origin = resolve_internal_endpoint(ref)
        supplied = str(endpoint_url or "").strip()
        if not supplied:
            return kind, ref, origin
        from urllib.parse import urlsplit
        parts = urlsplit(supplied)
        requested = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        if requested != origin or parts.username or parts.password or parts.query or parts.fragment:
            raise PlatformValidationError("An internal endpoint URL must stay on its configured origin and cannot include credentials, a query, or a fragment.")
        path = parts.path or "/"
        if not path.startswith("/") or "\\" in path or any(segment in {".", ".."} for segment in path.split("/")):
            raise PlatformValidationError("An internal endpoint path is invalid.")
        return kind, ref, origin + path.rstrip("/")

    def _endpoint(self, endpoint_url: str) -> str:
        normalized = endpoint_url.strip().rstrip("/")
        if normalized:
            self.endpoint_validator(normalized)
        return normalized

    def _credential_refs(self, values: list[str]) -> list[str]:
        refs = [str(value).strip() for value in values]
        if len(set(refs)) != len(refs) or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", ref) for ref in refs):
            raise PlatformValidationError("Credential references must be unique valid credential names.")
        missing = [ref for ref in refs if not self.credentials.get_status(ref)["configured"]]
        if missing:
            raise PlatformValidationError("Configured credential references were not found.")
        return refs

    @staticmethod
    def _project_id(value: str | None) -> str | None:
        if value is None:
            return None
        if not _CONNECTION_ID_RE.fullmatch(value):
            raise PlatformValidationError("Invalid connection project scope.")
        return value

    @staticmethod
    def _validate_id(value: str) -> None:
        if not _CONNECTION_ID_RE.fullmatch(value):
            raise PlatformValidationError("Invalid integration connection id.")

    @staticmethod
    def _safe_json(value: dict[str, Any] | None, *, label: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise PlatformValidationError(f"{label} must be an object.")
        ConnectionProfileService._reject_secrets(value)
        try:
            encoded = json.dumps(value, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError(f"{label} must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
            raise PlatformValidationError(f"{label} exceeds the 64 KiB limit.")
        return value

    @staticmethod
    def _reject_secrets(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if _SENSITIVE_KEY.search(str(key)):
                    raise PlatformValidationError("Connection configuration must reference credentials instead of storing secrets.")
                ConnectionProfileService._reject_secrets(nested)
        elif isinstance(value, list):
            for nested in value:
                ConnectionProfileService._reject_secrets(nested)

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        value["credential_refs"] = json.loads(value.pop("credential_refs_json") or "[]")
        value["configuration"] = json.loads(value.pop("configuration_json") or "{}")
        value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
        value["health"] = json.loads(value.pop("health_json") or "{}")
        return value
