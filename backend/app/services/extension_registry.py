"""Persistent metadata registry for Lynewright Extension Manifest v1.

This registry persists installation metadata and owner-controlled lifecycle
intent. It intentionally does not load extension code, grant requested
capabilities, or activate contributions; those are later milestones.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from collections.abc import Callable
from typing import Any, Iterator

from app.services.extension_manifest import ExtensionManifest, validate_extension_manifest
from app.services.platform_capabilities import PUBLIC_CAPABILITIES
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID


EXTENSION_STATUS_INSTALLED = "installed"
EXTENSION_STATUS_ENABLED = "enabled"
EXTENSION_STATUS_DISABLED = "disabled"
# A refreshed manifest no longer covers everything the operator granted or
# configured. Nothing is rewritten and nothing is revoked; the record says so
# and waits for a person.
EXTENSION_STATUS_NEEDS_ATTENTION = "needs_attention"
# "not_executable" is a runtime state, not a failure: the record declares no
# runtime under the current architecture, so there was never anything to
# activate. It is spelled the same as the derived `runtime` field so the stored
# status and the computed one share one vocabulary instead of approximating
# each other. "degraded" would have been the closest existing value and it is
# wrong -- that means running but impaired.
EXTENSION_STATUS_NOT_EXECUTABLE = "not_executable"
_RUNTIME_STATUSES = frozenset({
    "configured", "activating", "active", "degraded", "error",
    EXTENSION_STATUS_NOT_EXECUTABLE,
})
_CONFIGURATION_MAX_BYTES = 64 * 1024
_SENSITIVE_KEY = re.compile(r"(secret|token|password|api[_-]?key)", re.IGNORECASE)


class ExtensionRegistry:
    """Owns durable extension installation metadata for one tenant scope."""

    def __init__(
        self,
        store: PlatformStore | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        lynewright_version: str = "0.1.0",
        sdk_version: str = "1.0.0",
    ) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id
        self.lynewright_version = lynewright_version
        self.sdk_version = sdk_version
        self._disable_listeners: list[Callable[[str], None]] = []

    def on_disabled(self, callback: Callable[[str], None]) -> Callable[[], None]:
        """Subscribe a runtime cleanup callback without persisting runtime handles."""
        self._disable_listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._disable_listeners:
                self._disable_listeners.remove(callback)

        return unsubscribe

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_extensions (
                tenant_id TEXT NOT NULL,
                id TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                permission_mode TEXT NOT NULL DEFAULT 'restricted',
                requested_capabilities_json TEXT NOT NULL DEFAULT '[]',
                granted_capabilities_json TEXT NOT NULL DEFAULT '[]',
                configuration_json TEXT NOT NULL DEFAULT '{}',
                manifest_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                source_distribution TEXT NOT NULL DEFAULT '',
                source_version TEXT NOT NULL DEFAULT '',
                source_entrypoint TEXT NOT NULL DEFAULT '',
                installed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_activated_at TEXT,
                last_health_check_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tenant_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_platform_extensions_tenant_status
                ON platform_extensions(tenant_id, status, updated_at DESC);
            """
        )
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(platform_extensions)").fetchall()}
        if "last_health_check_at" not in columns:
            db.execute("ALTER TABLE platform_extensions ADD COLUMN last_health_check_at TEXT")
        for name in ("source", "source_distribution", "source_version", "source_entrypoint"):
            if name not in columns:
                default = "'manual'" if name == "source" else "''"
                db.execute(f"ALTER TABLE platform_extensions ADD COLUMN {name} TEXT NOT NULL DEFAULT {default}")

    def set_source_metadata(
        self, extension_id: str, *, source: str, distribution: str = "",
        version: str = "", entrypoint_module: str = "",
    ) -> dict[str, Any]:
        """Persist host-derived provenance for bundled or package integrations."""
        if source not in {"bundled", "package"}:
            raise PlatformValidationError("Integration source metadata is invalid.")
        with self._connection() as db, db:
            row = self._row_from_connection(db, extension_id)
            if row is None:
                raise PlatformNotFoundError("Extension not found.")
            db.execute(
                """UPDATE platform_extensions SET source=?,source_distribution=?,source_version=?,
                   source_entrypoint=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?""",
                (source, distribution, version, entrypoint_module, self.tenant_id, extension_id),
            )
        return self.get(extension_id)

    def list(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM platform_extensions WHERE tenant_id=? ORDER BY installed_at,id",
                (self.tenant_id,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def get(self, extension_id: str) -> dict[str, Any]:
        return self._public(self._row(extension_id))

    def inspect(self, extension_id: str) -> dict[str, Any]:
        """Return persisted metadata and the validated manifest for operators."""
        return self.get(extension_id)

    def register(
        self,
        value: ExtensionManifest | dict[str, object],
        *,
        configuration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        manifest = validate_extension_manifest(
            value,
            lynewright_version=self.lynewright_version,
            sdk_version=self.sdk_version,
        )
        config = self._configuration(configuration)
        manifest_value = manifest.model_dump(mode="json")
        try:
            with self._connection() as db, db:
                db.execute(
                    """
                    INSERT INTO platform_extensions (
                        tenant_id,id,name,version,type,status,enabled,permission_mode,
                        requested_capabilities_json,granted_capabilities_json,
                        configuration_json,manifest_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self.tenant_id,
                        manifest.id,
                        manifest.name,
                        manifest.version,
                        manifest.type,
                        EXTENSION_STATUS_INSTALLED,
                        0,
                        "restricted",
                        json.dumps(sorted(manifest.requested_capabilities)),
                        "[]",
                        json.dumps(config, sort_keys=True),
                        json.dumps(manifest_value, sort_keys=True),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PlatformConflictError("An extension with that id is already registered for this tenant.") from exc
        return self.get(manifest.id)

    def refresh_manifest(
        self,
        value: ExtensionManifest | dict[str, object],
    ) -> dict[str, Any]:
        """Update a first-party integration's shipped declaration in place.

        Installation is idempotent on *existence*, which meant a manifest that
        changed in a later release never reached the registry: the record kept
        whatever was stored when it was first installed, and anything reading
        the manifest -- notably the runtime compiler -- kept acting on a stale
        declaration forever.

        Only the declaration is replaced. `enabled`, `status`, `configuration`,
        and `granted_capabilities` are operator decisions and are deliberately
        untouched, so a deploy cannot enable an integration, re-grant a
        capability an operator revoked, or widen authority by shipping a
        manifest that requests more. Requested capabilities may change here;
        granted ones may not.
        """
        manifest = validate_extension_manifest(
            value,
            lynewright_version=self.lynewright_version,
            sdk_version=self.sdk_version,
        )
        with self._connection() as db, db:
            row = self._row_from_connection(db, manifest.id)
            if row is None:
                raise PlatformNotFoundError("Extension not found.")
            # Runtime status is recomputed rather than carried over: the old
            # value described the old declaration, so "active" or "error" from
            # a previous manifest would be a claim about code that is no longer
            # what this integration says it is. Activation sets the real value.
            granted = frozenset(json.loads(row["granted_capabilities_json"] or "[]"))
            stranded = granted - frozenset(manifest.requested_capabilities)
            status = (
                EXTENSION_STATUS_NEEDS_ATTENTION if stranded
                else (EXTENSION_STATUS_ENABLED if bool(row["enabled"]) else EXTENSION_STATUS_INSTALLED)
            )
            db.execute(
                """UPDATE platform_extensions
                   SET name=?,version=?,type=?,requested_capabilities_json=?,manifest_json=?,
                       status=?,last_error=?,updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND id=?""",
                (
                    manifest.name, manifest.version, manifest.type,
                    json.dumps(sorted(manifest.requested_capabilities)),
                    json.dumps(manifest.model_dump(mode="json"), sort_keys=True),
                    status,
                    # The stranded grants are the attention, stated plainly.
                    ("Capabilities granted but no longer requested: "
                     + ", ".join(sorted(stranded))) if stranded else "",
                    self.tenant_id, manifest.id,
                ),
            )
        return self.get(manifest.id)

    def configure(self, extension_id: str, configuration: dict[str, Any]) -> dict[str, Any]:
        """Persist non-secret configuration metadata; runtime validation is deferred."""
        config = self._configuration(configuration)
        with self._connection() as db, db:
            row = self._row_from_connection(db, extension_id)
            if bool(row["enabled"]):
                raise PlatformOperationError("Disable an extension before changing its configuration.")
            db.execute(
                "UPDATE platform_extensions SET configuration_json=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?",
                (json.dumps(config, sort_keys=True), self.tenant_id, extension_id),
            )
        return self.get(extension_id)

    def update_operator_settings(self, extension_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """Persist only validated declarative settings while an integration runs.

        General configuration can contain connection and deployment metadata,
        so it remains disabled-only.  Operator settings have already passed
        the packaged schema validator before reaching this method; allowing
        this narrow subtree lets credential rotation and safe defaults take
        effect for the next invocation without giving the UI a way to alter a
        runtime, image, command, endpoint, or volume.
        """
        if not isinstance(settings, dict):
            raise PlatformValidationError("Operator settings must be an object.")
        with self._connection() as db, db:
            row = self._row_from_connection(db, extension_id)
            configuration = self._configuration(json.loads(row["configuration_json"] or "{}"))
            configuration["settings"] = settings
            db.execute(
                "UPDATE platform_extensions SET configuration_json=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?",
                (json.dumps(configuration, sort_keys=True), self.tenant_id, extension_id),
            )
        return self.get(extension_id)

    def set_authorization(
        self,
        extension_id: str,
        *,
        permission_mode: str,
        granted_capabilities: list[str],
    ) -> dict[str, Any]:
        """Persist owner-approved grants; caller authorization belongs to the service layer."""
        if permission_mode not in {"restricted", "custom", "full"}:
            raise PlatformValidationError("Unsupported extension permission mode.")
        requested = self.get(extension_id)["requested_capabilities"]
        candidate = frozenset(str(capability) for capability in granted_capabilities)
        unknown = candidate - PUBLIC_CAPABILITIES
        if unknown:
            raise PlatformValidationError("Only known public capabilities may be granted to an extension.")
        if permission_mode == "full":
            if candidate:
                raise PlatformValidationError("Full Access extensions do not accept custom capability grants.")
            stored_grants: frozenset[str] = frozenset()
        else:
            not_requested = candidate - frozenset(requested)
            if not_requested:
                raise PlatformValidationError("Extensions may only be granted capabilities they requested in their manifest.")
            stored_grants = candidate
        with self._connection() as db, db:
            self._row_from_connection(db, extension_id)
            db.execute(
                "UPDATE platform_extensions SET permission_mode=?,granted_capabilities_json=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?",
                (permission_mode, json.dumps(sorted(stored_grants)), self.tenant_id, extension_id),
            )
        return self.get(extension_id)

    def set_runtime_status(self, extension_id: str, *, status: str, error: str = "", activated: bool = False) -> dict[str, Any]:
        """Persist lifecycle state from the controlled runtime, never from an adapter."""
        if status not in _RUNTIME_STATUSES:
            raise PlatformValidationError("Unsupported integration runtime status.")
        with self._connection() as db, db:
            self._row_from_connection(db, extension_id)
            activated_sql = ",last_activated_at=CURRENT_TIMESTAMP" if activated else ""
            db.execute(
                f"UPDATE platform_extensions SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP{activated_sql} WHERE tenant_id=? AND id=?",
                (status, error[:1_000], self.tenant_id, extension_id),
            )
        return self.get(extension_id)

    def record_health_check(self, extension_id: str, *, error: str = "") -> dict[str, Any]:
        with self._connection() as db, db:
            self._row_from_connection(db, extension_id)
            db.execute(
                "UPDATE platform_extensions SET last_health_check_at=CURRENT_TIMESTAMP,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?",
                (error[:1_000], self.tenant_id, extension_id),
            )
        return self.get(extension_id)

    def enable(self, extension_id: str) -> dict[str, Any]:
        with self._connection() as db, db:
            row = self._row_from_connection(db, extension_id)
            if bool(row["enabled"]):
                raise PlatformOperationError("Extension is already enabled.")
            db.execute(
                "UPDATE platform_extensions SET enabled=1,status=?,updated_at=CURRENT_TIMESTAMP,last_error='' WHERE tenant_id=? AND id=?",
                (EXTENSION_STATUS_ENABLED, self.tenant_id, extension_id),
            )
        return self.get(extension_id)

    def disable(self, extension_id: str) -> dict[str, Any]:
        with self._connection() as db, db:
            row = self._row_from_connection(db, extension_id)
            if not bool(row["enabled"]):
                raise PlatformOperationError("Extension is already disabled.")
            db.execute(
                "UPDATE platform_extensions SET enabled=0,status=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?",
                (EXTENSION_STATUS_DISABLED, self.tenant_id, extension_id),
            )
        result = self.get(extension_id)
        for callback in tuple(self._disable_listeners):
            try:
                callback(extension_id)
            except Exception:
                # A cleanup observer must not make durable disabling fail.
                pass
        return result

    def uninstall(self, extension_id: str) -> None:
        """Remove installation metadata only after the extension is disabled."""
        with self._connection() as db, db:
            row = self._row_from_connection(db, extension_id)
            if bool(row["enabled"]):
                raise PlatformOperationError("Disable an extension before uninstalling it.")
            db.execute(
                "DELETE FROM platform_extensions WHERE tenant_id=? AND id=?",
                (self.tenant_id, extension_id),
            )

    def _row(self, extension_id: str) -> sqlite3.Row:
        with self._connection() as db:
            return self._row_from_connection(db, extension_id)

    def _row_from_connection(self, db: sqlite3.Connection, extension_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM platform_extensions WHERE tenant_id=? AND id=?",
            (self.tenant_id, extension_id),
        ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Extension not found.")
        return row

    @staticmethod
    def _configuration(configuration: dict[str, Any] | None) -> dict[str, Any]:
        if configuration is None:
            return {}
        if not isinstance(configuration, dict):
            raise PlatformValidationError("Extension configuration must be an object.")
        ExtensionRegistry._reject_secrets(configuration)
        try:
            encoded = json.dumps(configuration, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("Extension configuration must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > _CONFIGURATION_MAX_BYTES:
            raise PlatformValidationError("Extension configuration exceeds the 64 KiB limit.")
        return configuration

    @staticmethod
    def _reject_secrets(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if _SENSITIVE_KEY.search(str(key)):
                    raise PlatformValidationError("Extension configuration must reference credentials instead of storing secrets.")
                ExtensionRegistry._reject_secrets(nested)
        elif isinstance(value, list):
            for nested in value:
                ExtensionRegistry._reject_secrets(nested)

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        value["requested_capabilities"] = json.loads(value.pop("requested_capabilities_json") or "[]")
        value["granted_capabilities"] = json.loads(value.pop("granted_capabilities_json") or "[]")
        value["configuration"] = json.loads(value.pop("configuration_json") or "{}")
        value["manifest"] = json.loads(value.pop("manifest_json") or "{}")
        # A grant only takes effect while the current manifest still requests
        # it. Storing the grant and deriving its effect keeps a capability
        # dropped by one release from being destroyed: roll the manifest back
        # and the original authorization applies again, untouched.
        requested = frozenset(value["requested_capabilities"])
        value["effective_capabilities"] = sorted(
            frozenset(value["granted_capabilities"]) & requested
        )
        value["inactive_capabilities"] = sorted(
            frozenset(value["granted_capabilities"]) - requested
        )
        return value
