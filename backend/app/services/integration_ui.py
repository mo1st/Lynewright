"""Read-only, provider-neutral view models for integration management UI."""
from __future__ import annotations

from typing import Any

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_lifecycle import IntegrationLifecycleRuntime
from app.services.integration_registry import IntegrationRegistry


class IntegrationUIService:
    def __init__(self, integrations: IntegrationRegistry, connections: ConnectionProfileService, contributions: ContributionRegistry, *, lifecycle: IntegrationLifecycleRuntime | None = None) -> None:
        self.integrations, self.connections, self.contributions, self.lifecycle = integrations, connections, contributions, lifecycle

    def list_integrations(self) -> list[dict[str, Any]]:
        return [self.summary(value["id"]) for value in self.integrations.list()]

    def summary(self, integration_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        connection_values = self.connections.list(integration_id)
        return {
            "integration": integration,
            "connections": [{key: value for key, value in item.items() if key not in {"configuration", "metadata"}} for item in connection_values],
            "contributions": [{"id": item.id, "kind": item.kind, "connection_id": item.connection_id, "metadata": self._safe_metadata(item.metadata)} for item in self.contributions.list(integration_id=integration_id)],
            "activity": {"connection_count": len(connection_values), "contribution_count": len(self.contributions.list(integration_id=integration_id))},
        }

    def settings(self, integration_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        return {"integration_id": integration_id, "configuration": integration["configuration"], "connections": self.connections.list(integration_id)}

    def health(self, integration_id: str) -> dict[str, Any]:
        if self.lifecycle is not None:
            return self.lifecycle.health(integration_id)
        integration = self.integrations.get(integration_id)
        return {"integration_id": integration_id, "status": integration["status"], "connections": [item["health"] for item in self.connections.list(integration_id)]}

    def diagnostics(self, integration_id: str) -> dict[str, Any]:
        if self.lifecycle is not None:
            return self.lifecycle.diagnose(integration_id)
        integration = self.integrations.get(integration_id)
        return {"integration_id": integration_id, "status": integration["status"], "result": {"runtime": "not_registered"}}

    @staticmethod
    def _safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
        return {key: nested for key, nested in value.items() if "secret" not in key.lower() and "token" not in key.lower()}
