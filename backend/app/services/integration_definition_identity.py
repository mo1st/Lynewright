"""Durable registry and activation lock for trusted runtime definitions."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Iterator

from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformNotFoundError, PlatformStore, PlatformValidationError


@dataclass(frozen=True, slots=True)
class IntegrationDefinitionIdentity:
    integration_id: str
    definition_version: str
    manifest_fingerprint: str
    contract_versions: dict[str, str]

    def key(self) -> tuple[str, str, str, str]:
        return (self.integration_id, self.definition_version, self.manifest_fingerprint, json.dumps(self.contract_versions, sort_keys=True))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _canonical_manifest_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    """Keep additive empty declarations from invalidating an old activation lock."""
    value = json.loads(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    if not isinstance(value, dict):
        return manifest
    provides = value.get("provides")
    if isinstance(provides, list):
        for item in provides:
            if isinstance(item, dict) and item.get("async_operations") == {}:
                item.pop("async_operations", None)
    return value


class IntegrationDefinitionIdentityService:
    """Records bootstrapped definitions and locks enabled instances to one."""

    def __init__(self, registry: IntegrationRegistry) -> None:
        self.registry = registry
        self.store: PlatformStore = registry.store
        self.tenant_id = registry.tenant_id

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_integration_definition_registry (
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                definition_version TEXT NOT NULL,
                manifest_fingerprint TEXT NOT NULL,
                contract_versions_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id,integration_id,definition_version,manifest_fingerprint,contract_versions_json)
            );
            CREATE TABLE IF NOT EXISTS platform_integration_activated_definition (
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                definition_version TEXT NOT NULL,
                manifest_fingerprint TEXT NOT NULL,
                contract_versions_json TEXT NOT NULL DEFAULT '{}',
                activated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id,integration_id)
            );
            """
        )

    def identity_for(
        self,
        integration_id: str,
        *,
        definition_version: str | None = None,
        manifest_fingerprint: str | None = None,
        contract_versions: dict[str, str] | None = None,
    ) -> IntegrationDefinitionIdentity:
        integration = self.registry.get(integration_id)
        version = str(definition_version or integration["version"])
        if not version:
            raise PlatformValidationError("Integration definitions require a version.")
        manifest = integration["manifest"]
        fingerprint = manifest_fingerprint or hashlib.sha256(
            json.dumps(_canonical_manifest_identity(manifest), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        contracts = {str(key): str(value) for key, value in sorted((contract_versions or {}).items())}
        if any(not key.startswith("lynewright.") or not value for key, value in contracts.items()):
            raise PlatformValidationError("Definition contract versions must use Lynewright contract names.")
        return IntegrationDefinitionIdentity(integration_id, version, fingerprint, contracts)
    def record_identity(self, identity: IntegrationDefinitionIdentity) -> None:
        with self._connection() as db, db:
            db.execute(
                """INSERT OR IGNORE INTO platform_integration_definition_registry
                (tenant_id,integration_id,definition_version,manifest_fingerprint,contract_versions_json)
                VALUES (?,?,?,?,?)""",
                (self.tenant_id, identity.integration_id, identity.definition_version, identity.manifest_fingerprint, json.dumps(identity.contract_versions, sort_keys=True)),
            )

    def active(self, integration_id: str) -> IntegrationDefinitionIdentity | None:
        with self._connection() as db:
            row = db.execute(
                """SELECT integration_id,definition_version,manifest_fingerprint,contract_versions_json
                FROM platform_integration_activated_definition WHERE tenant_id=? AND integration_id=?""",
                (self.tenant_id, integration_id),
            ).fetchone()
        return self._row(row) if row is not None else None

    def lock_or_verify(self, identity: IntegrationDefinitionIdentity) -> IntegrationDefinitionIdentity:
        """First activation locks the identity; later activation requires exact equality."""
        self.record_identity(identity)
        active = self.active(identity.integration_id)
        if active is None:
            self.replace_active(identity)
            return identity
        if active != identity:
            raise PlatformValidationError("Activated integration definition does not match the locked identity.")
        return active

    def replace_active(self, identity: IntegrationDefinitionIdentity) -> None:
        """Only an explicit upgrade path may replace this durable lock."""
        self.record_identity(identity)
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_integration_activated_definition
                (tenant_id,integration_id,definition_version,manifest_fingerprint,contract_versions_json)
                VALUES (?,?,?,?,?)
                ON CONFLICT(tenant_id,integration_id) DO UPDATE SET
                    definition_version=excluded.definition_version,
                    manifest_fingerprint=excluded.manifest_fingerprint,
                    contract_versions_json=excluded.contract_versions_json,
                    updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, identity.integration_id, identity.definition_version, identity.manifest_fingerprint, json.dumps(identity.contract_versions, sort_keys=True)),
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> IntegrationDefinitionIdentity:
        return IntegrationDefinitionIdentity(
            str(row["integration_id"]), str(row["definition_version"]), str(row["manifest_fingerprint"]),
            json.loads(row["contract_versions_json"] or "{}"),
        )
