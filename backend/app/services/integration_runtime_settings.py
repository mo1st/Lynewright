"""Schema-driven runtime settings for managed integrations.

The dashboard persists only declarative values and credential *references*.
At execution time this service loads the activated integration's own schema,
resolves a referenced secret inside ``CredentialService.use``, and returns a
small runtime configuration.  It is intentionally provider-neutral: adding a
new integration does not require a new settings resolver or a new secret path.
"""
from __future__ import annotations

from typing import Any

from app.services.capability_context import current_capability_context
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_settings import IntegrationSettingsService
from app.services.platform_services import CredentialService
from app.services.tenant_schema import DEFAULT_TENANT_ID


class IntegrationRuntimeSettingsResolver:
    """Resolve the current, schema-declared runtime configuration safely."""

    def __init__(self, *, integrations: IntegrationRegistry | None = None, credentials: CredentialService | None = None) -> None:
        context = current_capability_context()
        tenant_id = context.tenant_id if context is not None else DEFAULT_TENANT_ID
        self.integrations = integrations or IntegrationRegistry(tenant_id=tenant_id)
        self.credentials = credentials or CredentialService(self.integrations.store)

    def public_values(self, integration_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        model = IntegrationSettingsService.model(integration)
        return dict(model.get("values") or {}) if model is not None else {}

    def runtime_config(self, integration_id: str) -> dict[str, Any]:
        """Build the transient config declared by an integration manifest.

        The returned mapping may contain a secret and is therefore for an
        internal runtime transport only.  Callers must neither persist, log,
        nor return it.  Credential resolution happens immediately before use,
        so credential rotation takes effect on the next operation.
        """
        integration = self.integrations.get(integration_id)
        schema = IntegrationSettingsService.schema(integration)
        if schema is None:
            return {}
        values = self.public_values(integration_id)
        resolved: dict[str, Any] = {}
        for field in schema["fields"]:
            binding = field.get("runtime_binding")
            if not isinstance(binding, dict):
                continue
            value = values.get(field["key"], field.get("default"))
            if field["type"] == "credential_ref":
                reference = str(value or "").strip()
                if not reference:
                    # Older installed configurations can be upgraded without
                    # interrupting a render already backed by the legacy
                    # isolated runtime file.  The management API enforces
                    # required_when on every new save; once a reference is
                    # selected it becomes the authoritative value here.
                    continue
                value = self.credentials.use(reference, lambda secret: secret)
            if value in (None, ""):
                continue
            name = str(binding["name"])
            resolved[name] = [value] if binding.get("collection") else value
        return resolved


__all__ = ["IntegrationRuntimeSettingsResolver"]
