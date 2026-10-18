"""Explicit, audited provider selection for a provider-neutral contract."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import EventService, PlatformNotFoundError, PlatformValidationError


class IntegrationProviderSelectionService:
    """Resolve an explicit project override or the tenant's global default.

    The tenant default is configuration inheritance, not runtime failover. Once
    a project row exists, its provider remains authoritative even when that
    provider is unavailable.
    """

    def __init__(self, registry: IntegrationRegistry, *, events: EventService | None = None, tenant_id: str | None = None) -> None:
        self.registry = registry
        self.store = registry.store
        self.tenant_id = tenant_id or registry.tenant_id
        self.events = events or EventService(self.store, tenant_id=self.tenant_id)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_integration_provider_selection (
                tenant_id TEXT NOT NULL,
                contract TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT '',
                integration_id TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                actor_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id,contract,project_id)
            );
            """
        )

    def select(self, *, contract: str, integration_id: str, reason: str, actor_id: str = "", project_id: str | None = None) -> dict[str, Any]:
        if not contract.startswith("lynewright.") or not reason.strip():
            raise PlatformValidationError("Provider selection requires a Lynewright contract and explicit reason.")
        self.registry.get(integration_id)
        project = project_id or ""
        with self._connection() as db, db:
            prior = db.execute(
                """SELECT integration_id FROM platform_integration_provider_selection
                WHERE tenant_id=? AND contract=? AND project_id=?""",
                (self.tenant_id, contract, project),
            ).fetchone()
            previous = str(prior["integration_id"]) if prior else ""
            db.execute(
                """INSERT INTO platform_integration_provider_selection
                (tenant_id,contract,project_id,integration_id,reason,actor_id)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(tenant_id,contract,project_id) DO UPDATE SET
                    integration_id=excluded.integration_id,reason=excluded.reason,
                    actor_id=excluded.actor_id,updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, contract, project, integration_id, reason[:500], actor_id[:160]),
            )
        event = self.events.publish({
            "type": "integration.provider.changed",
            "project_id": project_id,
            "payload": {"contract": contract, "previous_integration_id": previous, "integration_id": integration_id, "reason": reason[:500], "actor_id": actor_id[:160]},
        })
        return {**self.get(contract=contract, project_id=project_id), "event_id": event["id"]}

    def get(self, *, contract: str, project_id: str | None = None) -> dict[str, Any]:
        requested_project_id = project_id or None
        with self._connection() as db:
            row = None
            selection_scope = "tenant_default"
            if requested_project_id is not None:
                row = db.execute(
                    """SELECT * FROM platform_integration_provider_selection
                    WHERE tenant_id=? AND contract=? AND project_id=?""",
                    (self.tenant_id, contract, requested_project_id),
                ).fetchone()
                if row is not None:
                    selection_scope = "project"
            if row is None:
                row = db.execute(
                    """SELECT * FROM platform_integration_provider_selection
                    WHERE tenant_id=? AND contract=? AND project_id=''""",
                    (self.tenant_id, contract),
                ).fetchone()
        if row is None:
            raise PlatformNotFoundError("No provider has been explicitly selected for this contract.")
        value = dict(row)
        source_project_id = str(row["project_id"]) if selection_scope == "project" else None
        value["project_id"] = source_project_id
        value["selection_scope"] = selection_scope
        value["source_project_id"] = source_project_id
        value["requested_project_id"] = requested_project_id
        value["resolution_trace"] = {
            "contract": contract,
            "requested_project_id": requested_project_id,
            "integration_id": str(row["integration_id"]),
            "selection_scope": selection_scope,
            "source_project_id": source_project_id,
        }
        return value
