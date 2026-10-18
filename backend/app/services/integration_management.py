"""Provider-neutral operator management for integrations and remote resources."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
from datetime import UTC, datetime
from typing import Any

from app.services.capability_context import CapabilityInvocationContext
from app.services.control_plane_host import get_control_plane_registry
from app.services.extension_manifest import (
    ExtensionManifest,
    manifest_declares_runtime,
    validate_extension_manifest,
)
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_external_resources import ExternalResourceInventoryService
from app.services.integration_mcp import IntegrationMCPAdapter
from app.services.integration_openapi import IntegrationOpenAPIClient
from app.services.integration_oauth import OAuthConfiguration, OAuthService
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.integration_service_contracts import IntegrationServiceContractRegistry
from app.services.integration_settings import IntegrationSettingsService
from app.services.integration_lifecycle import IntegrationLifecycleRuntime
from app.services.integration_mapping_bootstrap import (
    MAX_MAPPING_SAMPLE_BYTES,
    MAX_PROBE_RESULT_BYTES,
    bounded_json,
    suggest_mapping_paths,
)
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_provenance import get_provenance, list_provenance, record_provenance
from app.services.integration_install_policy import (
    InstallPolicyForbidden,
    build_install_summary,
    catalogue_status,
    current_install_policy,
    decide_install,
    log_install_confirmation,
)
from app.services.platform_capabilities import PUBLIC_CAPABILITIES, CapabilityActor, require_capability
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformServiceError,
    PlatformStore,
    PlatformValidationError,
    _redact,
)
from app.services.production_integrations import (
    PRODUCT_ROLE_FIRST_PARTY_FEATURE,
    ensure_discovered_integrations_installed,
    integration_package_statuses,
    is_reserved_first_party_feature_id,
    package_manifest_source_available,
    product_metadata_for,
)
from app.services.resource_bindings import ResourceBindingService
from app.services.resource_lifecycle import ResourceLifecycleCoordinator
from app.services.resource_reconciliation import ResourceReconciliationService
from app.services.resource_setup_requirements import ResourceSetupRequirementService
from app.services.rate_limiter import RateLimiterUnavailable, get_rate_limiter
from app.services.tenant_schema import DEFAULT_TENANT_ID


_MANUAL_KINDS = frozenset({"mcp", "openapi", "webhook"})
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
_WEBHOOK_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,119}$")
_WEBHOOK_HEADER = re.compile(r"^[A-Za-z0-9-]{1,120}$")
_ACTION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_DESTRUCTIVE_ACTIONS = frozenset({"delete", "archive", "retire"})
_MUTATING_ACTIONS = _DESTRUCTIVE_ACTIONS | {"create", "update"}
_OPENAPI_MAPPING_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_OPENAPI_RESOURCE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_OPENAPI_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,79}(?:\.[A-Za-z_][A-Za-z0-9_-]{0,79}){0,9}$")
_OPENAPI_QUERY_PARAMETER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_OPENAPI_SENSITIVE_PATH = re.compile(r"(?:secret|token|password|api[_-]?key)", re.IGNORECASE)
_OPENAPI_INPUT_TARGET = re.compile(r"^(?:path|query)\.[A-Za-z_][A-Za-z0-9_-]{0,79}$|^body\.[A-Za-z_][A-Za-z0-9_-]{0,79}(?:\.[A-Za-z_][A-Za-z0-9_-]{0,79}){0,5}$")
_MAX_RESOURCE_MAPPINGS = 100
_OPENAPI_MAX_PAGES = 20
_MAX_RESOURCE_RESOURCES = 10_000
_MCP_MAPPING_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_MCP_MAPPING_PATH = re.compile(r"^\$|^[A-Za-z_][A-Za-z0-9_-]{0,79}(?:\.[A-Za-z_][A-Za-z0-9_-]{0,79}){0,5}$")
_MCP_MAPPING_TARGET = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,79}(?:\.[A-Za-z_][A-Za-z0-9_-]{0,79}){0,5}$")
_MCP_MAX_CONTRACT_MAPPINGS = 100
_MAX_MCP_TOOL_SCHEMA_BYTES = 64 * 1024
_MAPPING_READ_RATE_LIMIT = 10
_MAPPING_READ_RATE_WINDOW_SECONDS = 60


class IntegrationManagementService:
    """Compose the existing registry, connection, runtime, MCP, OpenAPI,
    inventory, and binding services into one generic management surface.

    The service knows runtime families (MCP/OpenAPI), not provider names. A
    package resource is discovered through the contract declared by its
    manifest; no provider-specific branch is required here.
    """

    def __init__(
        self,
        integrations: IntegrationRegistry,
        connections: ConnectionProfileService,
        contributions: ContributionRegistry,
        *,
        lifecycle: IntegrationLifecycleRuntime | None = None,
        inventory: ExternalResourceInventoryService | None = None,
        resource_lifecycle: ResourceLifecycleCoordinator | None = None,
        control_plane: Any | None = None,
        actor_id: str = "system",
    ) -> None:
        self.integrations = integrations
        self.connections = connections
        self.contributions = contributions
        self.lifecycle = lifecycle
        self.store = integrations.store
        self.tenant_id = integrations.tenant_id
        self.actor_id = str(actor_id or "system")[:200]
        self.inventory = inventory or ExternalResourceInventoryService(self.store, tenant_id=self.tenant_id)
        self.bindings = ResourceBindingService(self.store, tenant_id=self.tenant_id)
        self.resource_lifecycle = resource_lifecycle
        self.reconciliation = ResourceReconciliationService(self.store, tenant_id=self.tenant_id)
        self.setup = ResourceSetupRequirementService(self.store, tenant_id=self.tenant_id)
        self.mcp = IntegrationMCPAdapter(connections)
        self.control_plane = control_plane or get_control_plane_registry(self.tenant_id)
        self._shipped_packages_reconciled = False
        self._ensure_action_schema()

    # ------------------------------------------------------------------ reads

    def list_integrations(self) -> list[dict[str, Any]]:
        self._reconcile_shipped_packages()
        details: list[dict[str, Any]] = []
        for item in self.integrations.list():
            try:
                details.append(self.detail(str(item["id"])))
            except PlatformNotFoundError:
                # A concurrent removal may win between the registry snapshot
                # and detail lookup. The list should reflect the now-current
                # registry instead of surfacing a transient 404 to the page.
                continue
        return details

    def package_statuses(self) -> list[dict[str, str]]:
        self._reconcile_shipped_packages()
        statuses = integration_package_statuses(
            self.integrations,
            lynewright_version=str(getattr(self.integrations, "lynewright_version", "0.1.0")),
        )
        policy = current_install_policy()
        from app.services.production_integrations import discover_shipped_manifests
        discovered = {
            str(item.manifest.get("id") or ""): item
            for item in discover_shipped_manifests(
                str(getattr(self.integrations, "lynewright_version", "0.1.0"))
            )
            if item.source == "package"
        }
        provenance_by_id = {
            str(row["integration_id"]): row for row in list_provenance(self.store, tenant_id=self.tenant_id)
        }
        for status in statuses:
            integration_id = str(status.get("integration_id") or "")
            item = discovered.get(integration_id)
            if item and policy == "catalogue-only":
                state = catalogue_status(
                    self.store,
                    integration_id=integration_id,
                    version=str(item.manifest.get("version") or ""),
                    artifact_kind="package",
                    sha256=item.artifact_sha256,
                )
                if state != "matched" and status.get("status") == "available":
                    status["status"] = "not_allowed_by_policy"
                    status["message"] = "Outside package is blocked by LYNEWRIGHT_INSTALL_POLICY=catalogue-only."
            provenance = provenance_by_id.get(integration_id)
            if provenance and provenance.get("status") == "awaiting_confirmation":
                status["status"] = "awaiting_confirmation"
                status["message"] = "This outside package is installed but awaits admin confirmation."
            elif provenance and provenance.get("kind") == "outside" and policy == "catalogue-only":
                status["status"] = "not_allowed_by_policy"
                status["message"] = "This outside integration is not allowed by LYNEWRIGHT_INSTALL_POLICY=catalogue-only."
        return statuses

    def confirm_package_install(
        self, integration_id: str, *, confirm: dict[str, Any] | None,
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        if str(integration.get("source") or "") != "package":
            raise PlatformNotFoundError("Installed native package was not found.")
        provenance = get_provenance(self.store, self.tenant_id, integration_id)
        if provenance and provenance.get("kind") == "catalogue":
            return self.detail(integration_id)
        from app.services.production_integrations import discover_shipped_manifests

        item = next((
            candidate for candidate in discover_shipped_manifests(
                str(getattr(self.integrations, "lynewright_version", "0.1.0"))
            )
            if candidate.source == "package" and str(candidate.manifest.get("id") or "") == integration_id
        ), None)
        if item is None:
            raise PlatformOperationError("The package is not currently selected and discoverable; confirmation is unavailable.")
        digest = str(item.artifact_sha256 or "")
        summary_digest = digest or str(item.installed_sha256 or "")
        version = str(item.manifest.get("version") or item.distribution_version or "")
        state = catalogue_status(
            self.store,
            integration_id=integration_id,
            version=version,
            artifact_kind="package",
            sha256=digest,
        )
        source_description = (
            f"package:{item.distribution}=={item.distribution_version}; wheel-sha256:{digest}"
            if digest else
            f"package:{item.distribution}=={item.distribution_version}; installed-files-sha256:{summary_digest}"
            if summary_digest else
            f"package:{item.distribution}=={item.distribution_version}; artifact SHA-256 unavailable"
        )
        summary = build_install_summary(
            item.manifest,
            kind="native package",
            source=source_description,
            sha256=summary_digest,
            catalogue_status_value=state,
        )
        decision = decide_install(
            summary, confirm=confirm, actor=self.actor_id,
            catalogue_matched=state == "matched",
        )
        if not decision.catalogue_matched:
            record_provenance(
                self.store, self.tenant_id, integration_id, kind="outside",
                source=source_description,
                confirmed_by=decision.confirmed_by,
                confirmed_at=decision.confirmed_at,
                entry_version=version,
                sha256=digest or None,
                status="current",
            )
            log_install_confirmation(integration_id=integration_id, actor=self.actor_id, decision=decision)
            self.ensure_runtime_definition_registered(integration_id)
        return self.detail(integration_id)

    def ensure_runtime_definition_registered(self, integration_id: str) -> dict[str, Any]:
        """Register a newly installed manifest through the startup compiler path."""
        if self.lifecycle is None:
            return {"registered": False, "reason": "runtime_lifecycle_unavailable"}
        from app.services.integration_activation import bootstrap_installed_runtime_definitions

        result = bootstrap_installed_runtime_definitions(
            self.integrations, runtime=self.lifecycle, integration_ids={str(integration_id)},
        )
        if (
            str(integration_id) not in result["registered"]
            and str(integration_id) not in result["already_registered"]
        ):
            reason = next(
                (item.split(":", 1)[1] for item in result["failed"]
                 if item.startswith(f"{integration_id}:")),
                str(result["diagnostics"].get(str(integration_id)) or "runtime definition is unavailable"),
            )
            raise PlatformOperationError(
                f"Integration runtime definition could not be registered: {reason}."
            )
        return result

    def detail(self, integration_id: str) -> dict[str, Any]:
        self._reconcile_shipped_packages()
        integration = self.integrations.get(integration_id)
        lynewright_version = str(getattr(self.integrations, "lynewright_version", "0.1.0"))
        product_metadata = product_metadata_for(integration_id, lynewright_version)
        source = str(integration.get("source") or product_metadata.get("source") or "manual")
        package_in_process = source == "package" and os.getenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", "").strip() == "1"
        execution_plane = "direct" if package_in_process else "container" if source == "package" else "direct"
        from app.services.integration_runner import IntegrationRunnerService
        runner_status = IntegrationRunnerService(self.store, tenant_id=self.tenant_id).status(integration_id)
        package_status = next((
            item for item in self.package_statuses()
            if item.get("integration_id") == integration_id
        ), None)
        product_metadata["source"] = source
        product_metadata["source_distribution"] = str(integration.get("source_distribution") or "")
        product_metadata["source_version"] = str(integration.get("source_version") or "")
        product_metadata["source_entrypoint"] = str(integration.get("source_entrypoint") or "")
        if source == "package":
            stored_package = (integration.get("configuration") or {}).get("catalogue_package")
            if isinstance(stored_package, dict):
                wheel_directory = Path(str(stored_package.get("wheel_directory") or ""))
                wheel_filename = str(stored_package.get("wheel_filename") or "")
                product_metadata["available"] = (
                    bool(wheel_filename)
                    and wheel_directory.is_dir()
                    and (wheel_directory / wheel_filename).is_file()
                )
            else:
                product_metadata["available"] = package_manifest_source_available(
                    integration_id,
                    str(integration.get("source_distribution") or ""),
                    str(integration.get("source_entrypoint") or ""),
                    lynewright_version=lynewright_version,
                )
        if isinstance((integration.get("configuration") or {}).get("catalogue_package"), dict):
            package_status = {"status": "catalogue_installed"}
        if package_status and package_status.get("status") in {"incompatible", "id_collision", "not_listed", "not_installed", "no_entry_point", "manifest_error", "invalid_entry_point", "unavailable", "awaiting_confirmation"}:
            product_metadata["available"] = False
        product_role = str(product_metadata["product_role"])
        manifest = self._manifest(integration)
        try:
            settings_schema = IntegrationSettingsService.schema(integration)
        except PlatformValidationError:
            # A delisted or incompatible native package stays visible as an
            # unavailable integration, but its Python settings code is not imported.
            settings_schema = None
        descriptor = self._descriptor(
            manifest,
            product_role=product_role,
            has_settings=settings_schema is not None,
        )
        try:
            settings_model = IntegrationSettingsService.model(integration) if descriptor.get("supports_settings") else None
        except PlatformValidationError:
            settings_model = None
        connections = [self._safe_connection(item) for item in self.connections.list(integration_id)]
        resources = self._resource_descriptors(descriptor, integration)
        binding_values = self.bindings.list()
        integration_bindings = [item for item in binding_values if item["integration_id"] == integration_id]
        setup_requirements = [
            requirement
            for binding in integration_bindings
            for requirement in self.setup.list(binding_id=binding["id"])
        ]
        inventory_items = self.inventory.list(integration_id=integration_id)["items"]
        provenance = get_provenance(self.store, self.tenant_id, integration_id)
        resource_count = sum(1 for item in inventory_items if item["state"] == "ACTIVE")
        attention_items = self._attention_for_bindings({item["id"] for item in integration_bindings})
        attention: list[str] = []
        if integration.get("last_error"):
            attention.append(str(integration["last_error"]))
        if any(item["state"] != "ACTIVE" for item in inventory_items):
            attention.append("Some discovered external resources are stale.")
        attention.extend(str(item["message"]) for item in attention_items)
        safe_integration = dict(integration)
        # These values are derived from the host-owned package location, never
        # from the operator-authored manifest or a manual integration request.
        safe_integration["product_role"] = product_role
        safe_integration["kind"] = str(product_metadata["kind"])
        safe_integration["source"] = source
        safe_integration["source_distribution"] = str(product_metadata["source_distribution"])
        safe_integration["source_version"] = str(product_metadata["source_version"])
        safe_integration["source_entrypoint"] = str(product_metadata["source_entrypoint"])
        safe_integration["execution_plane"] = execution_plane
        safe_integration["trusted_inprocess"] = package_in_process
        # The management view never needs a provider document or connection
        # configuration. Keep only the manual runtime discriminator and redact
        # package metadata defensively before it crosses the API boundary.
        raw_configuration = integration.get("configuration") or {}
        manual_configuration = raw_configuration.get("manual") if isinstance(raw_configuration, dict) else None
        safe_integration["configuration"] = _redact(
            {"manual": {"runtime": manual_configuration.get("runtime")}}
            if isinstance(manual_configuration, dict) and manual_configuration.get("runtime")
            else {}
        )
        return {
            "integration": safe_integration,
            "provenance": provenance,
            "availability": {
                "available": bool(product_metadata["available"]),
                "enabled": bool(integration.get("enabled")),
                "configured": bool(connections),
                "manual": self._manual_kind(integration) is not None,
                "declarative": bool(
                    isinstance(manual_configuration, dict)
                    and isinstance(manual_configuration.get("declaration"), dict)
                ),
                "product_role": product_role,
                "kind": str(product_metadata["kind"]),
                "package_status": str(package_status.get("status") if package_status else ("available" if source == "package" and product_metadata["available"] else "")),
                "execution_plane": execution_plane,
                "runner_status": runner_status["status"],
                "runner_last_seen_at": runner_status["last_seen_at"],
            },
            "capabilities": {
                "requested": list(integration.get("requested_capabilities") or []),
                "granted": list(integration.get("granted_capabilities") or []),
                "permission_mode": integration.get("permission_mode", "restricted"),
            },
            "connections": connections,
            "management": {
                "sections": descriptor["sections"],
                "resource_types": resources,
                "settings_schema": settings_model,
                "supported_actions": sorted({
                    action for resource in resources for action in resource["operations"]
                }),
            },
            "setup_requirements": setup_requirements,
            "oauth": {"supported": bool(descriptor.get("supports_oauth"))},
            "inbound": self.inbound_handlers(integration_id) if descriptor.get("supports_webhooks") else [],
            "activity": {
                "connection_count": len(connections),
                "resource_count": resource_count,
                "binding_count": len(integration_bindings),
            },
            "health": self._health(integration_id),
            "attention": attention,
            "attention_items": attention_items,
        }

    def _reconcile_shipped_packages(self) -> None:
        """Make shipped declarations visible in this existing owner scope once per access."""
        if self._shipped_packages_reconciled:
            return
        ensure_discovered_integrations_installed(self.integrations)
        self._shipped_packages_reconciled = True

    def settings(self, integration_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        model = IntegrationSettingsService.model(integration)
        if model is None:
            raise PlatformOperationError("This integration does not declare operator settings.")
        return model

    def update_settings(self, integration_id: str, values: dict[str, Any]) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        candidate = IntegrationSettingsService.update(integration, values)
        saved = self.integrations.update_operator_settings(integration_id, candidate)
        if self.lifecycle is not None:
            self.lifecycle.apply_operator_settings(
                integration_id, dict(saved.get("configuration") or {}),
            )
        return self.settings(integration_id)

    def resources(
        self,
        integration_id: str,
        *,
        connection_id: str | None = None,
        resource_type: str | None = None,
        search: str | None = None,
        cursor: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        self.integrations.get(integration_id)
        result = self.inventory.list(
            integration_id=integration_id,
            connection_id=connection_id,
            resource_type=resource_type,
            search=search,
            cursor=cursor,
            limit=limit,
        )
        all_bindings = self.bindings.list()
        attention_items = self._attention_for_bindings({item["id"] for item in all_bindings})
        attention_by_binding: dict[str, list[dict[str, Any]]] = {}
        for attention in attention_items:
            attention_by_binding.setdefault(str(attention.get("binding_id") or ""), []).append(attention)
        for item in result["items"]:
            matching = [
                binding for binding in all_bindings
                if binding["integration_id"] == integration_id
                and binding["connection_id"] == item["connection_id"]
                and binding["external_resource_id"] == item["external_id"]
                and binding["resource_type"] == item["resource_type"]
                and binding["state"] not in {"DETACHED", "RETIRING"}
            ]
            item["binding_status"] = matching[0]["state"] if matching else "UNBOUND"
            item["binding"] = matching[0] if matching else None
            item["setup_requirements"] = self.setup.list(binding_id=matching[0]["id"]) if matching else []
            item["attention"] = attention_by_binding.get(matching[0]["id"], []) if matching else []
        return result

    def connections_list(self, integration_id: str) -> list[dict[str, Any]]:
        self.integrations.get(integration_id)
        return [self._safe_connection(item) for item in self.connections.list(integration_id)]

    def bindings_list(self, integration_id: str) -> list[dict[str, Any]]:
        self.integrations.get(integration_id)
        return [item for item in self.bindings.list() if item["integration_id"] == integration_id]

    def health(self, integration_id: str) -> dict[str, Any]:
        self.integrations.get(integration_id)
        return self._health(integration_id)

    def diagnostics(self, integration_id: str) -> dict[str, Any]:
        self.integrations.get(integration_id)
        if self.lifecycle is not None:
            try:
                return self.lifecycle.diagnose(integration_id)
            except PlatformNotFoundError:
                pass
        return {"integration_id": integration_id, "status": "not_registered", "result": {}}

    def attention(self, integration_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return existing canonical lifecycle attention for one integration."""
        self.integrations.get(integration_id)
        binding_ids = {
            item["id"] for item in self.bindings.list()
            if item["integration_id"] == integration_id
        }
        return self._attention_for_bindings(binding_ids, limit=limit)

    def setup_requirements(self, integration_id: str) -> list[dict[str, Any]]:
        """Project setup requirements with their canonical binding context."""
        self.integrations.get(integration_id)
        result: list[dict[str, Any]] = []
        for binding in self.bindings.list():
            if binding["integration_id"] != integration_id:
                continue
            for requirement in self.setup.list(binding_id=binding["id"]):
                result.append({
                    **requirement,
                    "binding": {
                        "id": binding["id"],
                        "resource_type": binding["resource_type"],
                        "resource_id": binding["resource_id"],
                        "role": binding["role"],
                        "state": binding["state"],
                    },
                })
        return result[:500]

    def reconcile(self, integration_id: str, *, max_items: int = 5) -> dict[str, Any]:
        """Run the existing bounded lifecycle reconciliation for one integration."""
        self.integrations.get(integration_id)
        if self.resource_lifecycle is None:
            raise PlatformOperationError("Resource lifecycle worker is not available for this tenant.")
        if max_items < 1 or max_items > 100:
            raise PlatformValidationError("Integration reconciliation limit is invalid.")
        reconciled = self.resource_lifecycle.reconcile_bindings(
            max_items=max_items,
            integration_id=integration_id,
        )
        self.resource_lifecycle.reconcile_attention(limit=min(1_000, max_items * 10))
        return {
            "integration_id": integration_id,
            "reconciled": reconciled,
            "attention": self.attention(integration_id),
        }

    def repair_binding(
        self,
        integration_id: str,
        binding_id: str,
        *,
        operator_id: str,
        reason: str,
    ) -> dict[str, Any]:
        self.integrations.get(integration_id)
        binding = self.bindings.get(binding_id)
        if binding["integration_id"] != integration_id:
            raise PlatformNotFoundError("Resource binding was not found for this integration.")
        if self.resource_lifecycle is None:
            raise PlatformOperationError("Resource lifecycle worker is not available for this tenant.")
        return self.resource_lifecycle.repair_binding(
            binding_id=binding_id,
            operator_id=operator_id,
            reason=reason,
        )

    def verify_setup(
        self,
        integration_id: str,
        binding_id: str,
        requirement_id: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.integrations.get(integration_id)
        binding = self.bindings.get(binding_id)
        if binding["integration_id"] != integration_id:
            raise PlatformNotFoundError("Resource binding was not found for this integration.")
        if self.resource_lifecycle is None:
            raise PlatformOperationError("Resource lifecycle worker is not available for this tenant.")
        return self.resource_lifecycle.verify_setup(
            binding_id=binding_id,
            requirement_id=requirement_id,
            evidence=evidence,
        )

    def inbound_handlers(self, integration_id: str) -> list[dict[str, Any]]:
        """Expose declared inbound handlers without their verification secrets."""
        integration = self.integrations.get(integration_id)
        handlers: list[dict[str, Any]] = []
        if self._manual_kind(integration) == "webhook":
            for connection in self.connections.list(integration_id):
                webhook = (connection.get("configuration") or {}).get("webhook")
                if not isinstance(webhook, dict):
                    continue
                handlers.append(self._safe_inbound_handler(
                    integration_id=integration_id,
                    connection_id=connection["id"],
                    handler_id=str(webhook.get("handler_id") or "inbound"),
                    kind="webhook",
                    verification=webhook.get("verification"),
                ))
        for kind in ("webhook", "callback"):
            for contribution in self.contributions.list(integration_id=integration_id, kind=kind):
                handlers.append(self._safe_inbound_handler(
                    integration_id=integration_id,
                    connection_id=contribution.connection_id,
                    handler_id=contribution.local_id,
                    kind=kind,
                    verification=contribution.metadata.get("verification"),
                    contribution_id=contribution.id,
                ))
        return handlers

    def oauth_status(self, integration_id: str, connection_id: str) -> dict[str, Any]:
        """Return safe OAuth state for one connection; never return token values."""
        integration = self.integrations.get(integration_id)
        self._oauth_configuration(integration)
        connection = self.connections.get(integration_id, connection_id)
        if not connection["enabled"]:
            return {"integration_id": integration_id, "connection_id": connection_id, "state": "DISABLED"}
        oauth = OAuthService(self.store, self.connections)
        try:
            token = oauth.token_status(self._sdk(integration_id, connection_id), connection_id=connection_id)
        except PlatformNotFoundError:
            return {"integration_id": integration_id, "connection_id": connection_id, "state": "NOT_AUTHORIZED"}
        return {
            "integration_id": integration_id,
            "connection_id": connection_id,
            "state": "EXPIRED" if token.get("expired") else "AUTHORIZED",
            "token_type": token.get("token_type", ""),
            "scope": token.get("scope", ""),
            "expires_at_epoch": token.get("expires_at_epoch"),
            "expired": bool(token.get("expired")),
            "updated_at": token.get("updated_at"),
        }

    def oauth_begin(self, integration_id: str, connection_id: str, *, redirect_uri: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        configuration = self._oauth_configuration(integration)
        result = OAuthService(self.store, self.connections).begin_authorization(
            self._sdk(integration_id, connection_id), configuration,
            redirect_uri=redirect_uri, connection_id=connection_id,
        )
        return {"integration_id": integration_id, "connection_id": connection_id, **result}

    def oauth_callback(
        self,
        integration_id: str,
        connection_id: str,
        *,
        state: str,
        code: str,
        access_credential_ref: str,
        refresh_credential_ref: str = "",
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        configuration = self._oauth_configuration(integration)
        result = OAuthService(self.store, self.connections).complete_callback(
            self._sdk(integration_id, connection_id), configuration,
            state=state, code=code, access_credential_ref=access_credential_ref,
            refresh_credential_ref=refresh_credential_ref, connection_id=connection_id,
        )
        return {"integration_id": integration_id, "connection_id": connection_id, "state": "AUTHORIZED", **result}

    def oauth_refresh(self, integration_id: str, connection_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        configuration = self._oauth_configuration(integration)
        result = OAuthService(self.store, self.connections).refresh(
            self._sdk(integration_id, connection_id), configuration, connection_id=connection_id,
        )
        return {"integration_id": integration_id, "connection_id": connection_id, "state": "AUTHORIZED", **result}

    @staticmethod
    def _safe_inbound_handler(
        *,
        integration_id: str,
        connection_id: str | None,
        handler_id: str,
        kind: str,
        verification: Any,
        contribution_id: str | None = None,
    ) -> dict[str, Any]:
        settings = verification if isinstance(verification, dict) else {}
        plural = "webhooks" if kind == "webhook" else "callbacks"
        connection_segment = connection_id or "{connection_id}"
        return {
            "id": contribution_id or f"{integration_id}:{connection_segment}:{handler_id}",
            "handler_id": handler_id,
            "kind": kind,
            "connection_id": connection_id,
            "endpoint": f"/api/integrations/{integration_id}/{connection_segment}/{plural}/{handler_id}",
            "verification_mode": str(settings.get("mode") or "declared"),
            "signature_header": str(settings.get("signature_header") or "x-signature"),
            "timestamp_header": str(settings.get("timestamp_header") or ""),
            "delivery_header": str(settings.get("delivery_header") or ""),
        }

    @staticmethod
    def _oauth_configuration(integration: dict[str, Any]) -> OAuthConfiguration:
        manifest = IntegrationManagementService._manifest(integration)
        value = manifest.management.oauth
        if not manifest.management.supports_oauth or value is None:
            raise PlatformOperationError("This integration does not support OAuth.")
        return OAuthConfiguration(
            authorization_url=value.authorization_url,
            token_url=value.token_url,
            client_id_ref=value.client_id_ref,
            client_secret_ref=value.client_secret_ref,
            scopes=tuple(value.scopes),
            use_pkce=value.use_pkce,
        )

    # -------------------------------------------------------------- discovery

    def discover(self, integration_id: str, connection_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        if not integration["enabled"]:
            raise PlatformOperationError("Integration must be enabled before discovery.")
        connection = self.connections.get(integration_id, connection_id)
        if not connection["enabled"]:
            raise PlatformOperationError("Integration connection must be enabled before discovery.")
        manual_kind = self._manual_kind(integration)
        if manual_kind == "mcp":
            discovered = self._discover_mcp(integration_id, connection_id)
            self.connections.configure(
                integration_id,
                connection_id,
                metadata={
                    "mcp": {
                        "protocol_version": discovered.get("protocol_version", ""),
                        "tools": [
                            {
                                "name": item.get("name", ""),
                                "description": str(item.get("description", ""))[:500],
                                "input_schema": item.get("input_schema", {}) if isinstance(item.get("input_schema"), dict) else {},
                                "read_only": item.get("read_only") if isinstance(item.get("read_only"), bool) else None,
                                "destructive": item.get("destructive") if isinstance(item.get("destructive"), bool) else None,
                                "safety_known": item.get("safety_known") is True,
                            }
                            for item in discovered.get("tools", [])
                            if isinstance(item, dict)
                        ],
                    }
                },
            )
            native = self.inventory.refresh(
                integration_id=integration_id,
                connection_id=connection_id,
                resource_type="mcp_resource",
                resources=[self._mcp_resource(item) for item in discovered["resources"]],
            )
            results = list(native["items"])
            errors: list[dict[str, str]] = []
            for mapping in self._mcp_resource_mappings(integration, connection):
                if not mapping["enabled"]:
                    continue
                try:
                    result = self._discover_mcp_mapping(integration_id, connection_id, mapping)
                    results.extend(result["items"])
                except PlatformServiceError as exc:
                    errors.append({"mapping_id": mapping["mapping_id"], "message": str(exc)[:300]})
            if errors:
                self.connections.record_health(integration_id, connection_id, state="DEGRADED", error_code="mcp_discovery_failed")
            return {
                "integration_id": integration_id,
                "connection_id": connection_id,
                "resources": results,
                "tools": discovered["tools"],
                "errors": errors,
            }
        if manual_kind == "openapi":
            return self._discover_openapi_resources(integration, connection_id)

        descriptor = self._descriptor(self._manifest(integration))
        resource_results: list[dict[str, Any]] = []
        for resource in descriptor["resources"]:
            if not resource["discoverable"]:
                continue
            discovered = self._invoke_discovery(
                integration_id,
                connection_id,
                resource["discovery_contract"],
                resource["discovery_operation"],
            )
            values = self._extract_resources(discovered)
            result = self.inventory.refresh(
                integration_id=integration_id,
                connection_id=connection_id,
                resource_type=resource["type"],
                resources=values,
            )
            resource_results.extend(result["items"])
        return {"integration_id": integration_id, "connection_id": connection_id, "resources": resource_results}

    def _discover_openapi_resources(self, integration: dict[str, Any], connection_id: str) -> dict[str, Any]:
        integration_id = str(integration["id"])
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for mapping in self._resource_mappings(integration):
            if not mapping["enabled"]:
                continue
            try:
                result = self._discover_openapi_mapping(integration_id, connection_id, mapping)
                results.extend(result["items"])
            except PlatformServiceError as exc:
                # A failed refresh deliberately leaves this mapping's prior
                # inventory intact; only a successful refresh may mark rows stale.
                errors.append({"mapping_id": mapping["mapping_id"], "message": str(exc)[:300]})
        if errors:
            self.connections.record_health(integration_id, connection_id, state="DEGRADED", error_code="openapi_discovery_failed")
        elif results:
            self.connections.record_health(integration_id, connection_id, state="HEALTHY", remote_version="openapi")
        return {
            "integration_id": integration_id,
            "connection_id": connection_id,
            "resources": results,
            "operations": self.openapi_operations(integration_id, connection_id),
            "errors": errors,
        }

    def _discover_openapi_mapping(
        self,
        integration_id: str,
        connection_id: str,
        mapping: dict[str, Any],
        *,
        persist: bool = True,
        allow_disabled_integration_for_preview: bool = False,
    ) -> dict[str, Any]:
        operations = {item["operation_id"]: item for item in self.openapi_operations(integration_id, connection_id)}
        list_operation = operations.get(mapping["list_operation_id"])
        if list_operation is None or not list_operation["approved"]:
            raise PlatformOperationError(f"Approve {mapping['list_operation_id']} on this connection before discovering {mapping['plural_label']}.")
        values: list[dict[str, Any]] = []
        pagination = mapping["pagination"]
        cursor: Any = None
        max_pages = pagination["max_pages"] if pagination else 1
        max_resources = pagination["max_resources"] if pagination else _MAX_RESOURCE_RESOURCES
        first_response: Any = None
        path_errors: list[str] = []
        for _ in range(max_pages):
            arguments = {"query": {pagination["cursor_query_param"]: cursor}} if pagination and cursor not in (None, "") else {}
            if allow_disabled_integration_for_preview:
                response = self.execute_openapi(
                    integration_id,
                    connection_id,
                    mapping["list_operation_id"],
                    arguments,
                    allow_disabled_integration_for_preview=True,
                )
            else:
                response = self.execute_openapi(
                    integration_id,
                    connection_id,
                    mapping["list_operation_id"],
                    arguments,
                )
            if first_response is None:
                first_response = response
            items, item_errors = self._mapping_items(response, mapping, kind_label="OpenAPI")
            path_errors.extend(item_errors)
            for item in items:
                if isinstance(item, dict) and len(values) < max_resources:
                    normalized = self._normalize_openapi_resource(item, mapping, operations)
                    if normalized is not None:
                        values.append(normalized)
            if not pagination or len(values) >= max_resources:
                break
            cursor = self._dotted_value(response, pagination["next_cursor_path"])
            if cursor in (None, ""):
                break
        if not persist:
            return {"items": values, "sample": first_response, "path_errors": path_errors}
        refreshed = self.inventory.refresh(
            integration_id=integration_id,
            connection_id=connection_id,
            resource_type=mapping["resource_type"],
            resources=values,
        )
        return refreshed

    def _discover_mcp_mapping(
        self,
        integration_id: str,
        connection_id: str,
        mapping: dict[str, Any],
        *,
        persist: bool = True,
        record_health: bool = True,
        allow_disabled_integration_for_preview: bool = False,
    ) -> dict[str, Any]:
        connection = self.connections.get(integration_id, connection_id)
        approved = set((connection.get("configuration") or {}).get("mcp", {}).get("approved_tools", []))
        response = self.mcp.call_tool(
            self._sdk(
                integration_id,
                connection_id,
                allow_disabled_integration_for_preview,
            ),
            tool_name=str(mapping["list_tool_name"]),
            arguments=dict(mapping["list_arguments"]),
            connection_id=connection_id,
            require_read_only=allow_disabled_integration_for_preview,
            record_health=record_health,
        )
        items, path_errors = self._mapping_items(response, mapping, kind_label="MCP")
        values: list[dict[str, Any]] = []
        for item in items[:_MAX_RESOURCE_RESOURCES]:
            if isinstance(item, dict):
                normalized = self._normalize_mcp_resource(item, mapping, approved)
                if normalized is not None:
                    values.append(normalized)
        if not persist:
            return {"items": values, "sample": response, "path_errors": path_errors}
        return self.inventory.refresh(
            integration_id=integration_id,
            connection_id=connection_id,
            resource_type=mapping["resource_type"],
            resources=values,
        )

    def _mapping_items(
        self,
        response: Any,
        mapping: dict[str, Any],
        *,
        kind_label: str,
    ) -> tuple[list[Any], list[str]]:
        path = str(mapping["items_path"])
        resolved = self._dotted_value(response, path)
        if resolved is None:
            raise PlatformValidationError(
                f"{kind_label} mapping items_path '{path}' matched nothing in the response."
            )
        if not isinstance(resolved, list):
            raise PlatformValidationError(
                f"{kind_label} mapping items_path '{path}' resolved to {type(resolved).__name__}; expected a list."
            )
        object_items = [item for item in resolved if isinstance(item, dict)]
        invalid_count = 0
        for item in object_items:
            external_id = self._dotted_value(item, str(mapping["id_path"]))
            if external_id is None or not isinstance(external_id, (str, int)) or not str(external_id).strip():
                invalid_count += 1
        if object_items and invalid_count == len(object_items):
            raise PlatformValidationError(
                f"{kind_label} mapping id_path '{mapping['id_path']}' was missing or invalid for all {len(object_items)} items."
            )
        errors = []
        if invalid_count:
            errors.append(
                f"{kind_label} mapping id_path '{mapping['id_path']}' was missing or invalid for {invalid_count} of {len(object_items)} items."
            )
        return resolved, errors

    def _normalize_mcp_resource(self, item: dict[str, Any], mapping: dict[str, Any], approved: set[str]) -> dict[str, Any] | None:
        external_id = self._dotted_value(item, mapping["id_path"])
        if external_id is None or not isinstance(external_id, (str, int)) or not str(external_id).strip():
            return None
        name = self._dotted_value(item, mapping["name_path"])
        metadata = {
            key: value for key, path in mapping.get("metadata_paths", {}).items()
            if (value := self._dotted_value(item, path)) is not None
        }
        available_actions = ["discover", "read", "bind", "refresh"]
        if mapping.get("create_tool_name") in approved:
            available_actions.append("create")
        for action, tool_key in (
            ("update", "update_tool_name"),
            ("delete", "delete_tool_name"),
        ):
            if mapping.get(tool_key) and mapping.get(f"{action}_external_id_target") and mapping[tool_key] in approved:
                available_actions.append(action)
        return {
            "external_id": str(external_id)[:256],
            "resource_type": mapping["resource_type"],
            "name": str(name if name is not None else external_id)[:500],
            "parent_external_id": self._dotted_text(item, mapping.get("parent_id_path")),
            "url": self._dotted_text(item, mapping.get("url_path")),
            "remote_status": self._dotted_text(item, mapping.get("status_path")),
            "metadata": metadata,
            "available_actions": available_actions,
        }


    def _normalize_openapi_resource(self, item: dict[str, Any], mapping: dict[str, Any], operations: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        external_id = self._dotted_value(item, mapping["id_path"])
        if external_id is None or not isinstance(external_id, (str, int)) or not str(external_id).strip():
            return None
        name = self._dotted_value(item, mapping["name_path"])
        metadata = {
            key: value for key, path in mapping["metadata_paths"].items()
            if (value := self._dotted_value(item, path)) is not None
        }
        available_actions = ["discover", "bind", "refresh"]
        for action, operation_key, target_key in (
            ("read", "get_operation_id", "get_external_id_target"),
            ("update", "update_operation_id", "update_external_id_target"),
            ("delete", "delete_operation_id", "delete_external_id_target"),
        ):
            if mapping.get(operation_key) and mapping.get(target_key) and operations.get(mapping[operation_key], {}).get("approved"):
                available_actions.append(action)
        return {
            "external_id": str(external_id)[:256],
            "resource_type": mapping["resource_type"],
            "name": str(name if name is not None else external_id)[:500],
            "parent_external_id": self._dotted_text(item, mapping.get("parent_id_path")),
            "url": self._dotted_text(item, mapping.get("url_path")),
            "remote_status": self._dotted_text(item, mapping.get("status_path")),
            "metadata": metadata,
            "available_actions": available_actions,
        }

    @staticmethod
    def _dotted_value(value: Any, path: str | None) -> Any:
        if not path:
            return None
        if path == "$":
            return value
        current = value
        for segment in path.split("."):
            if not isinstance(current, dict) or segment not in current:
                return None
            current = current[segment]
        return current

    @classmethod
    def _dotted_text(cls, value: Any, path: str | None) -> str:
        resolved = cls._dotted_value(value, path)
        return str(resolved)[:2_000] if isinstance(resolved, (str, int, float)) else ""

    def approve_mcp_tools(self, integration_id: str, connection_id: str, tool_names: list[str]) -> dict[str, Any]:
        if any(_TOOL_NAME.fullmatch(str(value)) is None for value in tool_names):
            raise PlatformValidationError("MCP tool names are invalid.")
        connection = self.connections.get(integration_id, connection_id)
        metadata = connection.get("metadata") if isinstance(connection.get("metadata"), dict) else {}
        discovered = metadata.get("mcp") if isinstance(metadata, dict) else {}
        discovered_tools = discovered.get("tools") if isinstance(discovered, dict) else []
        discovered_names = {
            str(item.get("name")) for item in discovered_tools
            if isinstance(item, dict) and item.get("name")
        }
        if not discovered_names or any(name not in discovered_names for name in tool_names):
            raise PlatformOperationError("MCP tools must be discovered before they can be approved.")
        configuration = dict(connection.get("configuration") or {})
        mcp = dict(configuration.get("mcp") or {})
        mcp["approved_tools"] = sorted(set(tool_names))
        configuration["mcp"] = mcp
        return self.connections.configure(integration_id, connection_id, configuration=configuration)

    def mcp_contract_targets(self) -> list[dict[str, Any]]:
        """Read-only host contract vocabulary suitable for mapped MCP output."""
        return [
            {
                "contract": item.contract_id,
                "operation": item.operation,
                "required_capability": item.required_actor_capability,
                "risk_class": item.risk_class,
                "output_schema": item.output_schema,
            }
            for item in self.control_plane.capabilities.descriptors()
            if isinstance(item.output_schema, dict)
        ]

    def manual_capability_options(self) -> list[str]:
        """Return host-owned capabilities relevant to data-only contract adapters."""
        contract_capabilities = {
            item.required_actor_capability
            for item in self.control_plane.capabilities.descriptors()
            if item.required_actor_capability in PUBLIC_CAPABILITIES
        }
        return sorted(contract_capabilities | {"network:use", "credentials:use"})

    def mcp_contract_mappings(self, integration_id: str, connection_id: str) -> list[dict[str, Any]]:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        return self._mcp_contract_mappings(connection)

    def create_mcp_contract_mapping(self, integration_id: str, connection_id: str, values: dict[str, Any]) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        mappings = self._mcp_contract_mappings(connection)
        if len(mappings) >= _MCP_MAX_CONTRACT_MAPPINGS:
            raise PlatformValidationError("MCP contract mapping limit has been reached.")
        mapping = self._validated_mcp_contract_mapping(integration, connection, values)
        if any(item["mapping_id"] == mapping["mapping_id"] for item in mappings):
            raise PlatformConflictError("An MCP contract mapping with that id already exists on this connection.")
        mappings.append(mapping)
        self._store_mcp_contract_mappings(integration_id, connection_id, connection, mappings)
        return mapping

    def update_mcp_contract_mapping(
        self, integration_id: str, connection_id: str, mapping_id: str, values: dict[str, Any],
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        mappings = self._mcp_contract_mappings(connection)
        index = next((index for index, item in enumerate(mappings) if item["mapping_id"] == mapping_id), None)
        if index is None:
            raise PlatformNotFoundError("MCP contract mapping not found.")
        candidate = dict(values)
        candidate["mapping_id"] = mapping_id
        mapping = self._validated_mcp_contract_mapping(integration, connection, candidate)
        mappings[index] = mapping
        self._store_mcp_contract_mappings(integration_id, connection_id, connection, mappings)
        return mapping

    def delete_mcp_contract_mapping(self, integration_id: str, connection_id: str, mapping_id: str) -> None:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        mappings = self._mcp_contract_mappings(connection)
        remaining = [item for item in mappings if item["mapping_id"] != mapping_id]
        if len(remaining) == len(mappings):
            raise PlatformNotFoundError("MCP contract mapping not found.")
        self._store_mcp_contract_mappings(integration_id, connection_id, connection, remaining)

    def execute_mcp_contract_mapping(
        self, integration_id: str, connection_id: str, mapping_id: str, arguments: dict[str, Any] | None = None,
        *, idempotency_key: str | None = None, confirm_destructive: bool = False,
        caller: CapabilityActor | None = None,
        invocation_context: CapabilityInvocationContext | None = None,
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        if not integration["enabled"]:
            raise PlatformOperationError("Enable the integration before executing an MCP contract mapping.")
        connection = self.connections.get(integration_id, connection_id)
        if not connection["enabled"]:
            raise PlatformOperationError("Enable the connection before executing an MCP contract mapping.")
        stored_mapping = next((item for item in self._mcp_contract_mappings(connection) if item["mapping_id"] == mapping_id and item["enabled"]), None)
        if stored_mapping is None:
            raise PlatformNotFoundError("Enabled MCP contract mapping not found.")
        mapping = self._validated_mcp_contract_mapping(integration, connection, stored_mapping)
        if caller is not None:
            require_capability(caller, mapping["required_capability"])
            if invocation_context is None:
                raise PlatformValidationError("Caller-scoped mapping execution requires an invocation context.")
            sdk = self._sdk_factory().create(
                caller=caller, integration_id=integration_id, connection_id=connection_id,
                project_id=invocation_context.project_id, invocation_context=invocation_context,
            )
        else:
            sdk = self._sdk(integration_id, connection_id)
        if mapping["write_enabled"]:
            if not idempotency_key or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
                raise PlatformValidationError("Mapped writes require an explicit idempotency key; unknown write outcomes are never retried.")
            if mapping["destructive"] and not confirm_destructive:
                raise PlatformValidationError("Destructive MCP contract mappings require explicit confirmation.")
        source = arguments or {}
        if not isinstance(source, dict):
            raise PlatformValidationError("Mapped contract arguments must be an object.")
        tool_arguments = self._map_mcp_values(source, mapping["argument_mapping"], source_label="input")
        cached: dict[str, Any] | None = None
        if mapping["write_enabled"]:
            cached = self._reserve_contract_action(
                integration_id=integration_id,
                connection_id=connection_id,
                mapping_id=mapping["mapping_id"],
                idempotency_key=idempotency_key or "",
                arguments=tool_arguments,
            )
        if cached is not None:
            return cached
        try:
            remote = self.mcp.call_tool(sdk, tool_name=mapping["tool_name"], arguments=tool_arguments, connection_id=connection_id)
        except Exception as exc:
            self.connections.record_health(integration_id, connection_id, state="DEGRADED", error_code="mcp_contract_mapping_failed")
            if mapping["write_enabled"]:
                self._finish_contract_action(idempotency_key=idempotency_key or "", status="failed", result=None, error=type(exc).__name__)
            if isinstance(exc, PlatformServiceError):
                raise
            raise PlatformOperationError("External MCP mapped operation failed; the outcome is unknown and will not be retried.") from exc
        try:
            output = self._map_mcp_values(remote, mapping["output_mapping"], source_label="output")
            self._validate_contract_output(output, mapping["output_schema"])
        except Exception as exc:
            self.connections.record_health(integration_id, connection_id, state="DEGRADED", error_code="mcp_contract_output_invalid")
            if mapping["write_enabled"]:
                self._finish_contract_action(idempotency_key=idempotency_key or "", status="failed", result=None, error=type(exc).__name__)
            raise PlatformOperationError("External MCP tool returned output incompatible with the mapped Lynewright contract.") from exc
        result = {
            "target_contract": mapping["target_contract"],
            "target_operation": mapping["target_operation"],
            "output": output,
        }
        if mapping["write_enabled"]:
            result.update({
                "mapping_id": mapping["mapping_id"],
                "tool_name": mapping["tool_name"],
                "risk_class": mapping["risk_class"],
                "destructive": mapping["destructive"],
                "provenance": {
                    "integration_id": integration_id,
                    "connection_id": connection_id,
                    "remote_operation_id": mapping["tool_name"],
                },
            })
        if mapping["write_enabled"]:
            self._finish_contract_action(idempotency_key=idempotency_key or "", status="completed", result=result, error="")
            sdk.logger.write(
                "info",
                "Mapped MCP write completed.",
                details={
                    "mapping_id": mapping["mapping_id"],
                    "tool_name": mapping["tool_name"],
                    "target_contract": mapping["target_contract"],
                    "target_operation": mapping["target_operation"],
                    "risk_class": mapping["risk_class"],
                    "destructive": mapping["destructive"],
                },
            )
        return result

    @staticmethod
    def _require_manual_mcp(integration: dict[str, Any]) -> None:
        if IntegrationManagementService._manual_kind(integration) != "mcp":
            raise PlatformOperationError("This integration is not a manual MCP integration.")

    @staticmethod
    def _mcp_contract_mappings(connection: dict[str, Any]) -> list[dict[str, Any]]:
        mcp = (connection.get("configuration") or {}).get("mcp")
        values = mcp.get("contract_mappings", []) if isinstance(mcp, dict) else []
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise PlatformValidationError("MCP contract mappings must be a list of objects.")
        return [dict(item) for item in values]

    def _validated_mcp_contract_mapping(
        self, integration: dict[str, Any], connection: dict[str, Any], values: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise PlatformValidationError("MCP contract mapping must be an object.")
        mapping_id = str(values.get("mapping_id") or f"mcp-{secrets.token_hex(6)}")
        tool_name = str(values.get("tool_name") or "")
        contract = str(values.get("target_contract") or "")
        operation = str(values.get("target_operation") or "")
        if _MCP_MAPPING_ID.fullmatch(mapping_id) is None or _TOOL_NAME.fullmatch(tool_name) is None:
            raise PlatformValidationError("MCP contract mapping id or tool name is invalid.")
        if not contract.startswith("lynewright.") or _ACTION_ID.fullmatch(operation) is None:
            raise PlatformValidationError("MCP target contract or operation is invalid.")
        metadata = connection.get("metadata") if isinstance(connection.get("metadata"), dict) else {}
        discovered = metadata.get("mcp") if isinstance(metadata, dict) else {}
        remote_tool = next(
            (item for item in discovered.get("tools", []) if isinstance(item, dict) and str(item.get("name")) == tool_name),
            None,
        ) if isinstance(discovered, dict) else None
        approved = set((connection.get("configuration") or {}).get("mcp", {}).get("approved_tools", []))
        if remote_tool is None:
            raise PlatformOperationError("MCP tool must be discovered before it can be mapped.")
        if tool_name not in approved:
            raise PlatformOperationError("MCP tool must be approved before it can be mapped.")
        descriptor = next((item for item in self.control_plane.capabilities.descriptors() if item.contract_id == contract and item.operation == operation), None)
        if descriptor is None:
            raise PlatformValidationError("Mapped Lynewright contract operation does not exist.")
        if not isinstance(descriptor.output_schema, dict):
            raise PlatformValidationError("Mapped Lynewright contract operation has no output schema.")
        if descriptor.required_actor_capability not in set(integration.get("granted_capabilities") or []):
            raise PlatformOperationError("Integration lacks the capability grant required by the target contract.")
        write_enabled = bool(values.get("write_enabled", False))
        safety_known = remote_tool.get("safety_known") is True
        read_only = remote_tool.get("read_only")
        destructive = remote_tool.get("destructive") is True
        remote_writes = read_only is False or destructive
        target_writes = descriptor.required_actor_capability.endswith(":write")
        if (remote_writes or target_writes) and not write_enabled:
            raise PlatformOperationError("MCP write contract mappings require explicit write opt-in.")
        if write_enabled and not target_writes:
            raise PlatformOperationError("The mapped write path must target a contract that declares a write capability.")
        if write_enabled and not safety_known:
            raise PlatformOperationError("MCP write contract mappings require explicit remote tool safety metadata.")
        if write_enabled and read_only is not False:
            raise PlatformOperationError("A write-enabled MCP mapping must target a tool explicitly marked non-read-only.")
        argument_mapping = self._validated_mcp_value_mapping(values.get("argument_mapping") or {}, label="argument")
        output_mapping = self._validated_mcp_value_mapping(values.get("output_mapping") or {}, label="output")
        return {
            "mapping_id": mapping_id, "tool_name": tool_name, "target_contract": contract,
            "target_operation": operation, "argument_mapping": argument_mapping,
            "output_mapping": output_mapping, "enabled": bool(values.get("enabled", True)),
            "write_enabled": write_enabled,
            "required_capability": descriptor.required_actor_capability,
            "risk_class": descriptor.risk_class,
            "destructive": destructive,
            "output_schema": descriptor.output_schema,
        }

    @staticmethod
    def _validated_mcp_value_mapping(value: Any, *, label: str) -> dict[str, Any]:
        if not isinstance(value, dict) or len(value) > 20:
            raise PlatformValidationError(f"MCP {label} mapping must contain at most 20 values.")
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError(f"MCP {label} mapping must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise PlatformValidationError(f"MCP {label} mapping exceeds the 16 KiB limit.")
        for target, source in value.items():
            if not isinstance(target, str) or _MCP_MAPPING_TARGET.fullmatch(target) is None:
                raise PlatformValidationError(f"MCP {label} mapping target is invalid.")
            if isinstance(source, dict):
                if set(source) != {"$literal"} or not isinstance(source["$literal"], (str, int, float, bool, type(None))):
                    raise PlatformValidationError(f"MCP {label} mapping literal is invalid.")
            elif isinstance(source, str) and _MCP_MAPPING_PATH.fullmatch(source) is None:
                raise PlatformValidationError(f"MCP {label} mapping source path is invalid.")
            elif not isinstance(source, (str, int, float, bool, type(None))):
                raise PlatformValidationError(f"MCP {label} mapping values must be paths or scalar literals.")
        return dict(value)

    def _store_mcp_contract_mappings(
        self, integration_id: str, connection_id: str, connection: dict[str, Any], mappings: list[dict[str, Any]],
    ) -> None:
        configuration = dict(connection.get("configuration") or {})
        mcp = dict(configuration.get("mcp") or {})
        mcp["contract_mappings"] = mappings
        configuration["mcp"] = mcp
        self.connections.configure(integration_id, connection_id, configuration=configuration)

    @classmethod
    def _map_mcp_values(cls, source: Any, mapping: dict[str, Any], *, source_label: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for target, value in mapping.items():
            resolved = value["$literal"] if isinstance(value, dict) else cls._dotted_value(source, value) if isinstance(value, str) else value
            if resolved is None and isinstance(value, str):
                raise PlatformOperationError(f"MCP {source_label} mapping source is missing.")
            current = result
            segments = str(target).split(".")
            for segment in segments[:-1]:
                current = current.setdefault(segment, {})
            current[segments[-1]] = resolved
        return result

    @classmethod
    def _validate_contract_output(cls, value: Any, schema: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("Mapped contract output must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise PlatformValidationError("Mapped contract output exceeds the 64 KiB limit.")
        cls._validate_schema_value(value, schema, label="output")

    @classmethod
    def _validate_schema_value(cls, value: Any, schema: dict[str, Any], *, label: str) -> None:
        kind = schema.get("type")
        if kind == "object":
            if not isinstance(value, dict):
                raise PlatformValidationError(f"Mapped contract {label} must be an object.")
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            if any(not isinstance(key, str) or key not in value for key in required):
                raise PlatformValidationError(f"Mapped contract {label} is missing required fields.")
            if schema.get("additionalProperties") is False and any(key not in properties for key in value):
                raise PlatformValidationError(f"Mapped contract {label} has unsupported fields.")
            for key, nested in properties.items():
                if key in value and isinstance(nested, dict):
                    cls._validate_schema_value(value[key], nested, label=f"{label}.{key}")
        elif kind == "array":
            if not isinstance(value, list):
                raise PlatformValidationError(f"Mapped contract {label} must be an array.")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value[:10_000]):
                    cls._validate_schema_value(item, item_schema, label=f"{label}[{index}]")
        elif kind == "string" and not isinstance(value, str):
            raise PlatformValidationError(f"Mapped contract {label} must be a string.")
        elif kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise PlatformValidationError(f"Mapped contract {label} must be an integer.")
        elif kind == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise PlatformValidationError(f"Mapped contract {label} must be a number.")
        elif kind == "boolean" and not isinstance(value, bool):
            raise PlatformValidationError(f"Mapped contract {label} must be a boolean.")

    def openapi_operations(self, integration_id: str, connection_id: str | None = None) -> list[dict[str, Any]]:
        integration = self.integrations.get(integration_id)
        if self._manual_kind(integration) != "openapi":
            raise PlatformOperationError("This integration is not a manual OpenAPI integration.")
        document = (integration.get("configuration") or {}).get("manual", {}).get("document")
        client = IntegrationOpenAPIClient(document, self._http_adapter())
        connection_values = self.connections.list(integration_id)
        connection = next((item for item in connection_values if connection_id is None or item["id"] == connection_id), None)
        if connection is None:
            raise PlatformNotFoundError("Integration connection not found.")
        approved = set((connection.get("configuration") or {}).get("openapi", {}).get("approved_operations", []))
        manual = (integration.get("configuration") or {}).get("manual", {})
        declaration = manual.get("declaration", {}) if isinstance(manual, dict) else {}
        declared_operations = declaration.get("operations", []) if isinstance(declaration, dict) else []
        overrides = {
            str(item.get("operation_id")): item
            for item in declared_operations if isinstance(item, dict) and item.get("operation_id")
        }
        result: list[dict[str, Any]] = []
        for operation in client.operations():
            path_item = document.get("paths", {}).get(operation.path, {}) if isinstance(document, dict) else {}
            raw_parameters = []
            if isinstance(path_item, dict) and isinstance(path_item.get("parameters"), list):
                raw_parameters.extend(path_item["parameters"])
            if isinstance(operation.value.get("parameters"), list):
                raw_parameters.extend(operation.value["parameters"])
            parameters = []
            for parameter in raw_parameters:
                if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str):
                    continue
                parameters.append({
                    "name": str(parameter["name"]),
                    "in": str(parameter.get("in") or ""),
                    "required": bool(parameter.get("required")),
                    "schema": dict(parameter.get("schema") or {}),
                })
            request_body_schema: dict[str, Any] | None = None
            request_body_content_type = ""
            request_body_required = False
            request_body = operation.value.get("requestBody")
            resolved_request_body = client._resolve(request_body) if request_body is not None else None
            if isinstance(resolved_request_body, dict):
                request_body_required = bool(resolved_request_body.get("required"))
                content = resolved_request_body.get("content")
                if isinstance(content, dict):
                    for candidate_type in ("multipart/form-data", "application/octet-stream", "application/json"):
                        media = content.get(candidate_type)
                        if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                            request_body_content_type = candidate_type
                            request_body_schema = dict(client._resolve(media["schema"]))
                            break
            override = overrides.get(operation.operation_id, {})
            result.append({
                "operation_id": operation.operation_id,
                "method": operation.method.upper(),
                "path": operation.path,
                "summary": str(
                    override.get("description")
                    or operation.value.get("summary") or operation.value.get("description") or ""
                )[:500],
                "approved": operation.operation_id in approved,
                **{
                    key: copy.deepcopy(override[key])
                    for key in ("input_hint", "output_hint") if key in override
                },
                "destructive": operation.method.lower() in {"delete", "put", "patch", "post"},
                "parameters": parameters,
                "request_body_schema": request_body_schema,
                "request_body_content_type": request_body_content_type,
                "request_body_required": request_body_required,
            })
        return result

    def approve_openapi_operations(self, integration_id: str, connection_id: str, operation_ids: list[str]) -> dict[str, Any]:
        operations = {item["operation_id"] for item in self.openapi_operations(integration_id, connection_id)}
        if any(operation_id not in operations for operation_id in operation_ids):
            raise PlatformValidationError("OpenAPI approval references an unknown operation.")
        connection = self.connections.get(integration_id, connection_id)
        configuration = dict(connection.get("configuration") or {})
        openapi = dict(configuration.get("openapi") or {})
        openapi["approved_operations"] = sorted(set(operation_ids))
        configuration["openapi"] = openapi
        return self.connections.configure(integration_id, connection_id, configuration=configuration)

    def openapi_resource_mappings(self, integration_id: str) -> list[dict[str, Any]]:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        return self._resource_mappings(integration)

    def validate_openapi_resource_mapping(
        self,
        integration_id: str,
        values: dict[str, Any],
        *,
        connection_id: str | None = None,
        invoke: bool = False,
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        mapping = self._validated_resource_mapping(integration, values)
        if not invoke:
            return mapping
        if not connection_id:
            raise PlatformValidationError("A connection is required for a live OpenAPI mapping preview.")
        self.connections.get(integration_id, connection_id)
        self._consume_mapping_read_limit(integration_id, connection_id)
        result = self._discover_openapi_mapping(
            integration_id,
            connection_id,
            mapping,
            persist=False,
            allow_disabled_integration_for_preview=True,
        )
        preview = self._mapping_preview(mapping, result, kind_label="OpenAPI")
        self._audit_mapping_read(
            self._sdk(
                integration_id,
                connection_id,
                True,
            ),
            event="openapi_mapping_preview",
            tool_name=str(mapping["list_operation_id"]),
            arguments={},
            truncated=bool(preview["raw_sample_truncated"]),
        )
        return preview

    def create_openapi_resource_mapping(self, integration_id: str, values: dict[str, Any]) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        mappings = self._resource_mappings(integration)
        if len(mappings) >= _MAX_RESOURCE_MAPPINGS:
            raise PlatformValidationError("OpenAPI resource mapping limit has been reached.")
        mapping = self._validated_resource_mapping(integration, values)
        if any(item["resource_type"] == mapping["resource_type"] for item in mappings):
            raise PlatformConflictError("An OpenAPI resource mapping already exists for this resource type.")
        mappings.append(mapping)
        self._store_resource_mappings(integration, mappings)
        return mapping

    def update_openapi_resource_mapping(self, integration_id: str, mapping_id: str, values: dict[str, Any]) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        mappings = self._resource_mappings(integration)
        current = next((item for item in mappings if item["mapping_id"] == mapping_id), None)
        if current is None:
            raise PlatformNotFoundError("OpenAPI resource mapping not found.")
        candidate = self._validated_resource_mapping(integration, {**current, **values, "mapping_id": mapping_id})
        if any(item["mapping_id"] != mapping_id and item["resource_type"] == candidate["resource_type"] for item in mappings):
            raise PlatformConflictError("An OpenAPI resource mapping already exists for this resource type.")
        identity_fields = ("resource_type", "list_operation_id", "items_path", "id_path", "parent_id_path")
        if any(current.get(field) != candidate.get(field) for field in identity_fields):
            self._refuse_mapping_change_with_bindings(integration_id, {current["resource_type"], candidate["resource_type"]})
        self._store_resource_mappings(integration, [candidate if item["mapping_id"] == mapping_id else item for item in mappings])
        return candidate

    def delete_openapi_resource_mapping(self, integration_id: str, mapping_id: str) -> None:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        mappings = self._resource_mappings(integration)
        current = next((item for item in mappings if item["mapping_id"] == mapping_id), None)
        if current is None:
            raise PlatformNotFoundError("OpenAPI resource mapping not found.")
        self._refuse_mapping_change_with_bindings(integration_id, {current["resource_type"]})
        self._store_resource_mappings(integration, [item for item in mappings if item["mapping_id"] != mapping_id])

    def mcp_resource_mappings(self, integration_id: str, connection_id: str) -> list[dict[str, Any]]:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        return self._mcp_resource_mappings(integration, connection)

    def validate_mcp_resource_mapping(
        self,
        integration_id: str,
        connection_id: str,
        values: dict[str, Any],
        *,
        invoke: bool = False,
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        mapping = self._validated_mcp_resource_mapping(integration, connection, values)
        if not invoke:
            return mapping
        self._consume_mapping_read_limit(integration_id, connection_id)
        result = self._discover_mcp_mapping(
            integration_id,
            connection_id,
            mapping,
            persist=False,
            record_health=False,
            allow_disabled_integration_for_preview=True,
        )
        preview = self._mapping_preview(mapping, result, kind_label="MCP")
        self._audit_mapping_read(
            self._sdk(
                integration_id,
                connection_id,
                True,
            ),
            event="mcp_mapping_preview",
            tool_name=str(mapping["list_tool_name"]),
            arguments=dict(mapping["list_arguments"]),
            truncated=bool(preview["raw_sample_truncated"]),
        )
        return preview

    def preview_mcp_resource_mapping(
        self,
        integration_id: str,
        connection_id: str,
        values: dict[str, Any],
        *,
        invoke: bool = False,
    ) -> dict[str, Any]:
        return self.validate_mcp_resource_mapping(integration_id, connection_id, values, invoke=invoke)

    def probe_mcp_tool(
        self,
        integration_id: str,
        connection_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        allow_disabled_integration_for_preview: bool = False,
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        metadata = connection.get("metadata") if isinstance(connection.get("metadata"), dict) else {}
        mcp = metadata.get("mcp") if isinstance(metadata, dict) else {}
        tools = mcp.get("tools", []) if isinstance(mcp, dict) else []
        tool = next((item for item in tools if isinstance(item, dict) and item.get("name") == tool_name), None)
        if tool is None:
            raise PlatformNotFoundError("MCP probe tool has not been discovered on this connection.")
        approved = set((connection.get("configuration") or {}).get("mcp", {}).get("approved_tools", []))
        if tool_name not in approved:
            raise PlatformOperationError("MCP probe requires the tool to be approved for this connection.")
        if not (
            tool.get("read_only") is True
            and tool.get("destructive") is False
            and tool.get("safety_known") is True
        ):
            raise PlatformOperationError(
                "MCP probe requires an approved tool explicitly marked read-only and non-destructive."
            )
        probe_arguments = self._validated_mcp_list_arguments(arguments or {})
        self._consume_mapping_read_limit(integration_id, connection_id)
        if allow_disabled_integration_for_preview:
            sdk = self._sdk(integration_id, connection_id, allow_disabled_integration_for_preview=True)
        else:
            sdk = self._sdk(integration_id, connection_id)
        response = self.mcp.call_tool(
            sdk,
            tool_name=tool_name,
            arguments=probe_arguments,
            connection_id=connection_id,
            require_read_only=True,
        )
        safe_result, truncated, size = bounded_json(response, max_bytes=MAX_PROBE_RESULT_BYTES)
        self._audit_mapping_read(
            sdk,
            event="mcp_tool_probe",
            tool_name=tool_name,
            arguments=probe_arguments,
            truncated=truncated,
        )
        return {
            "integration_id": integration_id,
            "connection_id": connection_id,
            "tool_name": tool_name,
            "result": safe_result,
            "truncated": truncated,
            "serialized_bytes": size,
        }

    def mcp_tool_input_schema(
        self,
        integration_id: str,
        connection_id: str,
        tool_name: str,
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        metadata = connection.get("metadata") if isinstance(connection.get("metadata"), dict) else {}
        mcp = metadata.get("mcp") if isinstance(metadata, dict) else {}
        tools = mcp.get("tools", []) if isinstance(mcp, dict) else []
        if not any(isinstance(item, dict) and item.get("name") == tool_name for item in tools):
            raise PlatformNotFoundError("MCP tool has not been discovered on this connection.")
        self._consume_mapping_read_limit(integration_id, connection_id)
        sdk = self._sdk(integration_id, connection_id)
        schema = self.mcp.tool_input_schema(sdk, tool_name=tool_name, connection_id=connection_id)
        safe_schema, truncated, size = bounded_json(schema, max_bytes=_MAX_MCP_TOOL_SCHEMA_BYTES)
        self._audit_mapping_read(
            sdk,
            event="mcp_tool_schema_view",
            tool_name=tool_name,
            arguments={},
            truncated=truncated,
        )
        return {
            "integration_id": integration_id,
            "connection_id": connection_id,
            "tool_name": tool_name,
            "input_schema": safe_schema,
            "truncated": truncated,
            "serialized_bytes": size,
        }

    def _mapping_preview(self, mapping: dict[str, Any], result: dict[str, Any], *, kind_label: str) -> dict[str, Any]:
        sample, truncated, size = bounded_json(result.get("sample"), max_bytes=MAX_MAPPING_SAMPLE_BYTES)
        return {
            "mapping": mapping,
            "projected_resources": result.get("items", []),
            "raw_sample": sample,
            "raw_sample_truncated": truncated,
            "raw_sample_serialized_bytes": size,
            "suggestions": suggest_mapping_paths(result.get("sample")),
            "path_errors": list(result.get("path_errors") or []),
            "runtime": kind_label.lower(),
        }

    def _consume_mapping_read_limit(self, integration_id: str, connection_id: str) -> None:
        try:
            allowed = get_rate_limiter().consume(
                namespace="integration_mapping_read",
                key=f"{self.tenant_id}:{integration_id}:{connection_id}",
                maximum=_MAPPING_READ_RATE_LIMIT,
                window_seconds=_MAPPING_READ_RATE_WINDOW_SECONDS,
            )
        except RateLimiterUnavailable as exc:
            raise PlatformOperationError("Mapping probe is unavailable because the rate limiter is unavailable.") from exc
        if not allowed:
            raise PlatformOperationError("Mapping probe rate limit exceeded; retry after the current minute.")

    @staticmethod
    def _audit_mapping_read(
        sdk: Any,
        *,
        event: str,
        tool_name: str,
        arguments: dict[str, Any],
        truncated: bool,
    ) -> None:
        sdk.logger.write(
            "info",
            f"{event} completed",
            details={
                "event": event,
                "tool_name": tool_name,
                "arguments": _redact(arguments),
                "truncated": truncated,
            },
        )

    def create_mcp_resource_mapping(self, integration_id: str, connection_id: str, values: dict[str, Any]) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        mappings = self._mcp_resource_mappings(integration, connection)
        if len(mappings) >= _MAX_RESOURCE_MAPPINGS:
            raise PlatformValidationError("MCP resource mapping limit has been reached.")
        mapping = self._validated_mcp_resource_mapping(integration, connection, values)
        if any(item["resource_type"] == mapping["resource_type"] for item in mappings):
            raise PlatformConflictError("An MCP resource mapping already exists for this resource type.")
        self._store_resource_mappings(integration, [*mappings, mapping], kind_label="MCP")
        return mapping

    def update_mcp_resource_mapping(self, integration_id: str, connection_id: str, mapping_id: str, values: dict[str, Any]) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        mappings = self._mcp_resource_mappings(integration, connection)
        current = next((item for item in mappings if item["mapping_id"] == mapping_id), None)
        if current is None:
            raise PlatformNotFoundError("MCP resource mapping not found.")
        candidate = self._validated_mcp_resource_mapping(integration, connection, {**current, **values, "mapping_id": mapping_id})
        if any(item["mapping_id"] != mapping_id and item["resource_type"] == candidate["resource_type"] for item in mappings):
            raise PlatformConflictError("An MCP resource mapping already exists for this resource type.")
        identity_fields = (
            "resource_type", "list_tool_name", "list_arguments", "items_path", "id_path", "name_path",
            "parent_id_path", "url_path", "status_path",
        )
        if any(current.get(field) != candidate.get(field) for field in identity_fields):
            self._refuse_mapping_change_with_bindings(integration_id, {current["resource_type"], candidate["resource_type"]})
        self._store_resource_mappings(
            integration,
            [candidate if item["mapping_id"] == mapping_id else item for item in mappings],
            kind_label="MCP",
        )
        return candidate

    def delete_mcp_resource_mapping(self, integration_id: str, connection_id: str, mapping_id: str) -> None:
        integration = self.integrations.get(integration_id)
        self._require_manual_mcp(integration)
        connection = self.connections.get(integration_id, connection_id)
        mappings = self._mcp_resource_mappings(integration, connection)
        current = next((item for item in mappings if item["mapping_id"] == mapping_id), None)
        if current is None:
            raise PlatformNotFoundError("MCP resource mapping not found.")
        self._refuse_mapping_change_with_bindings(integration_id, {current["resource_type"]})
        self._store_resource_mappings(
            integration,
            [item for item in mappings if item["mapping_id"] != mapping_id],
            kind_label="MCP",
        )

    def openapi_contract_mappings(self, integration_id: str, connection_id: str) -> list[dict[str, Any]]:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        connection = self.connections.get(integration_id, connection_id)
        return self._openapi_contract_mappings(connection)

    def create_openapi_contract_mapping(self, integration_id: str, connection_id: str, values: dict[str, Any]) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        connection = self.connections.get(integration_id, connection_id)
        mappings = self._openapi_contract_mappings(connection)
        if len(mappings) >= _MCP_MAX_CONTRACT_MAPPINGS:
            raise PlatformValidationError("OpenAPI contract mapping limit has been reached.")
        mapping = self._validated_openapi_contract_mapping(integration, connection_id, values)
        if any(item["mapping_id"] == mapping["mapping_id"] for item in mappings):
            raise PlatformConflictError("An OpenAPI contract mapping with that id already exists on this connection.")
        mappings.append(mapping)
        self._store_openapi_contract_mappings(integration, connection_id, connection, mappings)
        return mapping

    def update_openapi_contract_mapping(self, integration_id: str, connection_id: str, mapping_id: str, values: dict[str, Any]) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        connection = self.connections.get(integration_id, connection_id)
        mappings = self._openapi_contract_mappings(connection)
        current = next((item for item in mappings if item["mapping_id"] == mapping_id), None)
        if current is None:
            raise PlatformNotFoundError("OpenAPI contract mapping not found.")
        candidate = self._validated_openapi_contract_mapping(integration, connection_id, {**current, **values, "mapping_id": mapping_id})
        self._store_openapi_contract_mappings(integration, connection_id, connection, [candidate if item["mapping_id"] == mapping_id else item for item in mappings])
        return candidate

    def delete_openapi_contract_mapping(self, integration_id: str, connection_id: str, mapping_id: str) -> None:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        connection = self.connections.get(integration_id, connection_id)
        mappings = self._openapi_contract_mappings(connection)
        if not any(item["mapping_id"] == mapping_id for item in mappings):
            raise PlatformNotFoundError("OpenAPI contract mapping not found.")
        self._store_openapi_contract_mappings(integration, connection_id, connection, [item for item in mappings if item["mapping_id"] != mapping_id])

    def execute_openapi_contract_mapping(
        self, integration_id: str, connection_id: str, mapping_id: str, arguments: dict[str, Any] | None = None,
        *, idempotency_key: str | None = None, caller: Any = None, invocation_context: Any = None,
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        if not integration["enabled"]:
            raise PlatformOperationError("Enable the integration before executing an OpenAPI contract mapping.")
        connection = self.connections.get(integration_id, connection_id)
        if not connection["enabled"]:
            raise PlatformOperationError("Enable the connection before executing an OpenAPI contract mapping.")
        stored_mapping = next((item for item in self._openapi_contract_mappings(connection) if item["mapping_id"] == mapping_id and item["enabled"]), None)
        if stored_mapping is None:
            raise PlatformNotFoundError("Enabled OpenAPI contract mapping not found.")
        mapping = self._validated_openapi_contract_mapping(integration, connection_id, stored_mapping)
        if mapping["write_enabled"] and not idempotency_key:
            raise PlatformValidationError("Mapped writes require an explicit idempotency key; unknown write outcomes are never retried.")
        source = arguments or {}
        if not isinstance(source, dict):
            raise PlatformValidationError("Mapped contract arguments must be an object.")
        remote_arguments = self._map_mcp_values(source, mapping["argument_mapping"], source_label="input")
        try:
            remote = self.execute_openapi(
                integration_id,
                connection_id,
                mapping["operation_id"],
                remote_arguments,
                confirm_destructive=bool(mapping["write_enabled"]),
                idempotency_key=idempotency_key,
                caller=caller,
                invocation_context=invocation_context,
            )
        except PlatformServiceError:
            self.connections.record_health(integration_id, connection_id, state="DEGRADED", error_code="openapi_contract_mapping_failed")
            raise
        try:
            output = self._map_mcp_values(remote, mapping["output_mapping"], source_label="output")
            self._validate_contract_output(output, mapping["output_schema"])
        except Exception as exc:
            self.connections.record_health(integration_id, connection_id, state="DEGRADED", error_code="openapi_contract_output_invalid")
            raise PlatformOperationError("OpenAPI operation returned output incompatible with the mapped Lynewright contract.") from exc
        return {
            "mapping_id": mapping["mapping_id"],
            "operation_id": mapping["operation_id"],
            "target_contract": mapping["target_contract"],
            "target_operation": mapping["target_operation"],
            "risk_class": mapping["risk_class"],
            "destructive": mapping["destructive"],
            "output": output,
            "provenance": {"integration_id": integration_id, "connection_id": connection_id, "remote_operation_id": mapping["operation_id"]},
        }

    @staticmethod
    def _openapi_contract_mappings(connection: dict[str, Any]) -> list[dict[str, Any]]:
        openapi = (connection.get("configuration") or {}).get("openapi")
        values = openapi.get("contract_mappings", []) if isinstance(openapi, dict) else []
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise PlatformValidationError("OpenAPI contract mappings must be a list of objects.")
        return [dict(item) for item in values]

    def _validated_openapi_contract_mapping(self, integration: dict[str, Any], connection_id: str, values: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise PlatformValidationError("OpenAPI contract mapping must be an object.")
        mapping_id = str(values.get("mapping_id") or f"openapi-{secrets.token_hex(6)}")
        operation_id = str(values.get("operation_id") or "").strip()
        contract = str(values.get("target_contract") or "").strip()
        operation = str(values.get("target_operation") or "").strip()
        if _MCP_MAPPING_ID.fullmatch(mapping_id) is None or not operation_id:
            raise PlatformValidationError("OpenAPI contract mapping id or operation is invalid.")
        remote_operation = next((item for item in self.openapi_operations(str(integration["id"]), connection_id) if item["operation_id"] == operation_id), None)
        if remote_operation is None:
            raise PlatformNotFoundError("Mapped OpenAPI operation was not found.")
        if not remote_operation["approved"]:
            raise PlatformOperationError("Mapped OpenAPI operation must be approved for this connection.")
        descriptor = next((item for item in self.control_plane.capabilities.descriptors() if item.contract_id == contract and item.operation == operation), None)
        if descriptor is None or not isinstance(descriptor.output_schema, dict):
            raise PlatformValidationError("Mapped Lynewright contract operation does not exist or has no output schema.")
        if descriptor.required_actor_capability not in set(integration.get("granted_capabilities") or []):
            raise PlatformOperationError("Integration lacks the capability grant required by the target contract.")
        write_enabled = bool(values.get("write_enabled", False))
        if remote_operation["destructive"] and not write_enabled:
            raise PlatformOperationError("Destructive OpenAPI contract mappings require explicit write opt-in.")
        if write_enabled and not descriptor.required_actor_capability.endswith(":write"):
            raise PlatformOperationError("The mapped write path must target a contract that declares a write capability.")
        argument_mapping = self._validated_mcp_value_mapping(values.get("argument_mapping") or {}, label="argument")
        output_mapping = self._validated_mcp_value_mapping(values.get("output_mapping") or {}, label="output")
        return {
            "mapping_id": mapping_id, "operation_id": operation_id,
            "target_contract": contract, "target_operation": operation,
            "argument_mapping": argument_mapping, "output_mapping": output_mapping,
            "enabled": bool(values.get("enabled", True)), "write_enabled": write_enabled,
            "required_capability": descriptor.required_actor_capability, "risk_class": descriptor.risk_class,
            "destructive": bool(remote_operation["destructive"]),
            "output_schema": descriptor.output_schema,
        }

    def _store_openapi_contract_mappings(
        self, integration: dict[str, Any], connection_id: str, connection: dict[str, Any], mappings: list[dict[str, Any]],
    ) -> None:
        if integration.get("enabled"):
            raise PlatformOperationError("Disable the integration before changing OpenAPI contract mappings.")
        configuration = dict(connection.get("configuration") or {})
        openapi = dict(configuration.get("openapi") or {})
        openapi["contract_mappings"] = mappings
        configuration["openapi"] = openapi
        self.connections.configure(str(integration["id"]), connection_id, configuration=configuration)

    def _require_manual_openapi(self, integration: dict[str, Any]) -> None:
        if self._manual_kind(integration) != "openapi":
            raise PlatformOperationError("This integration is not a manual OpenAPI integration.")

    def _resource_mappings(self, integration: dict[str, Any]) -> list[dict[str, Any]]:
        return [self._validated_resource_mapping(integration, item) for item in self._stored_resource_mappings(integration, kind_label="OpenAPI")]

    @staticmethod
    def _stored_resource_mappings(integration: dict[str, Any], *, kind_label: str) -> list[dict[str, Any]]:
        manual = (integration.get("configuration") or {}).get("manual")
        values = manual.get("resource_mappings", []) if isinstance(manual, dict) else []
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise PlatformValidationError(f"{kind_label} resource mappings are invalid.")
        return [dict(item) for item in values]

    def _mcp_resource_mappings(self, integration: dict[str, Any], connection: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [self._validated_mcp_resource_mapping(integration, connection, item) for item in self._stored_resource_mappings(integration, kind_label="MCP")]

    @staticmethod
    def _validated_resource_mapping_common(values: dict[str, Any], *, kind_label: str) -> dict[str, Any]:
        if not isinstance(values, dict):
            raise PlatformValidationError(f"{kind_label} resource mapping must be an object.")
        mapping_id = str(values.get("mapping_id") or f"map_{secrets.token_hex(8)}")
        if _OPENAPI_MAPPING_ID.fullmatch(mapping_id) is None:
            raise PlatformValidationError(f"{kind_label} resource mapping id is invalid.")
        resource_type = str(values.get("resource_type") or "")
        if _OPENAPI_RESOURCE_TYPE.fullmatch(resource_type) is None:
            raise PlatformValidationError(f"{kind_label} resource type must be a bounded lowercase identifier.")
        label = str(values.get("label") or resource_type.replace("_", " ").title()).strip()
        plural_label = str(values.get("plural_label") or f"{label}s").strip()
        if not label or not plural_label or len(label) > 120 or len(plural_label) > 120:
            raise PlatformValidationError(f"{kind_label} resource labels are required and bounded.")
        paths: dict[str, str | None] = {}
        for key in ("items_path", "id_path", "name_path", "parent_id_path", "url_path", "status_path"):
            value = str(values.get(key) or "").strip() or None
            if key in {"items_path", "id_path", "name_path"} and value is None:
                raise PlatformValidationError(f"{kind_label} resource mapping requires {key}.")
            if value is not None and not (key == "items_path" and value == "$") and _OPENAPI_PATH.fullmatch(value) is None:
                raise PlatformValidationError(f"{kind_label} resource mapping {key} is invalid.")
            paths[key] = value
        display_fields = values.get("display_fields") or []
        if not isinstance(display_fields, list) or len(display_fields) > 20 or any(not isinstance(item, str) or _OPENAPI_PATH.fullmatch(item) is None for item in display_fields):
            raise PlatformValidationError(f"{kind_label} resource mapping display fields are invalid.")
        metadata_paths = values.get("metadata_paths") or {}
        if not isinstance(metadata_paths, dict) or len(metadata_paths) > 20:
            raise PlatformValidationError(f"{kind_label} resource mapping metadata paths are invalid.")
        if any(
            _OPENAPI_RESOURCE_TYPE.fullmatch(str(key)) is None
            or not isinstance(path, str)
            or _OPENAPI_PATH.fullmatch(path) is None
            or _OPENAPI_SENSITIVE_PATH.search(str(key))
            or _OPENAPI_SENSITIVE_PATH.search(path)
            for key, path in metadata_paths.items()
        ):
            raise PlatformValidationError(f"{kind_label} resource mapping metadata paths are invalid.")
        return {
            "mapping_id": mapping_id,
            "resource_type": resource_type,
            "label": label,
            "plural_label": plural_label,
            **paths,
            "display_fields": list(display_fields),
            "metadata_paths": {str(key): str(path) for key, path in metadata_paths.items()},
        }

    def _validated_resource_mapping(self, integration: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        common = self._validated_resource_mapping_common(values, kind_label="OpenAPI")
        document = (integration.get("configuration") or {}).get("manual", {}).get("document")
        client = IntegrationOpenAPIClient(document, self._http_adapter())
        operations = {item.operation_id: item for item in client.operations()}
        list_operation_id = str(values.get("list_operation_id") or "")
        list_operation = operations.get(list_operation_id)
        if list_operation is None:
            raise PlatformValidationError("OpenAPI resource mapping references an unknown list operation.")
        if list_operation.method not in {"get", "head"}:
            raise PlatformValidationError("OpenAPI resource discovery requires a read-only list operation.")
        optional_operations: dict[str, str | None] = {}
        for key in ("get_operation_id", "create_operation_id", "update_operation_id", "delete_operation_id"):
            value = str(values.get(key) or "").strip() or None
            if value is not None and value not in operations:
                raise PlatformValidationError(f"OpenAPI resource mapping references an unknown {key}.")
            optional_operations[key] = value
        external_id_targets: dict[str, str | None] = {}
        for key in ("get_external_id_target", "update_external_id_target", "delete_external_id_target"):
            value = str(values.get(key) or "").strip() or None
            operation_key = key.replace("_external_id_target", "_operation_id")
            if value is not None and _OPENAPI_INPUT_TARGET.fullmatch(value) is None:
                raise PlatformValidationError(f"OpenAPI resource mapping {key} is invalid.")
            if optional_operations[operation_key] is not None and value is None:
                raise PlatformValidationError(f"OpenAPI resource mapping requires {key} when {operation_key} is declared.")
            external_id_targets[key] = value
        pagination = self._validated_openapi_pagination(values.get("pagination"))
        return {
            **common,
            "list_operation_id": list_operation_id,
            **optional_operations,
            **external_id_targets,
            "pagination": pagination,
            "enabled": bool(values.get("enabled", True)),
        }

    def _validated_mcp_resource_mapping(
        self,
        integration: dict[str, Any],
        connection: dict[str, Any] | None,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        common = self._validated_resource_mapping_common(values, kind_label="MCP")
        tool_fields = ("list_tool_name", "get_tool_name", "create_tool_name", "update_tool_name", "delete_tool_name")
        tools: dict[str, str | None] = {}
        for key in tool_fields:
            value = str(values.get(key) or "").strip() or None
            if value is not None and _TOOL_NAME.fullmatch(value) is None:
                raise PlatformValidationError(f"MCP resource mapping {key} is invalid.")
            tools[key] = value
        list_tool_name = tools["list_tool_name"]
        if list_tool_name is None:
            raise PlatformValidationError("MCP resource mapping requires list_tool_name.")
        declared_tools = {value for value in tools.values() if value}
        if connection is not None:
            metadata = connection.get("metadata") if isinstance(connection.get("metadata"), dict) else {}
            mcp = metadata.get("mcp") if isinstance(metadata, dict) else {}
            discovered_tools = mcp.get("tools", []) if isinstance(mcp, dict) else []
            discovered_names = {
                str(item.get("name")) for item in discovered_tools
                if isinstance(item, dict) and item.get("name")
            }
            unknown = declared_tools - discovered_names
            if unknown:
                raise PlatformOperationError("MCP resource mapping references a tool that has not been discovered.")
            approved = set((connection.get("configuration") or {}).get("mcp", {}).get("approved_tools", []))
            if list_tool_name not in approved:
                raise PlatformOperationError("Approve the mapped MCP list tool before saving or discovering resources.")
        targets: dict[str, str | None] = {}
        for action in ("get", "update", "delete"):
            key = f"{action}_external_id_target"
            value = str(values.get(key) or "").strip() or None
            tool_key = f"{action}_tool_name"
            if value is not None and _MCP_MAPPING_TARGET.fullmatch(value) is None:
                raise PlatformValidationError(f"MCP resource mapping {key} is invalid.")
            if tools[tool_key] is not None and value is None:
                raise PlatformValidationError(f"MCP resource mapping requires {key} when {tool_key} is declared.")
            targets[key] = value
        return {
            **common,
            **tools,
            **targets,
            "list_arguments": self._validated_mcp_list_arguments(values.get("list_arguments") or {}),
            "enabled": bool(values.get("enabled", True)),
        }

    @staticmethod
    def _validated_mcp_list_arguments(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or len(value) > 20:
            raise PlatformValidationError("MCP resource mapping list_arguments must contain at most 20 scalar values.")
        for key, item in value.items():
            if not isinstance(key, str) or _MCP_MAPPING_TARGET.fullmatch(key) is None:
                raise PlatformValidationError("MCP resource mapping list_arguments keys are invalid.")
            if not isinstance(item, (str, int, float, bool, type(None))):
                raise PlatformValidationError("MCP resource mapping list_arguments must contain scalar values only.")
            if isinstance(item, str) and any(marker in item for marker in ("${", "{{", "}}")):
                raise PlatformValidationError("MCP resource mapping list_arguments cannot contain template expressions.")
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("MCP resource mapping list_arguments must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise PlatformValidationError("MCP resource mapping list_arguments exceeds the 16 KiB limit.")
        return dict(value)

    @staticmethod
    def _validated_openapi_pagination(value: Any) -> dict[str, Any] | None:
        if value in (None, {}):
            return None
        if not isinstance(value, dict) or set(value) - {"cursor_query_param", "next_cursor_path", "max_pages", "max_resources"}:
            raise PlatformValidationError("OpenAPI resource mapping pagination is invalid.")
        cursor_query_param = str(value.get("cursor_query_param") or "")
        next_cursor_path = str(value.get("next_cursor_path") or "")
        max_pages = value.get("max_pages", 10)
        max_resources = value.get("max_resources", 1_000)
        if _OPENAPI_QUERY_PARAMETER.fullmatch(cursor_query_param) is None or _OPENAPI_PATH.fullmatch(next_cursor_path) is None:
            raise PlatformValidationError("OpenAPI resource mapping pagination paths are invalid.")
        if not isinstance(max_pages, int) or not 1 <= max_pages <= _OPENAPI_MAX_PAGES:
            raise PlatformValidationError("OpenAPI resource mapping pagination max_pages is invalid.")
        if not isinstance(max_resources, int) or not 1 <= max_resources <= _MAX_RESOURCE_RESOURCES:
            raise PlatformValidationError("OpenAPI resource mapping pagination max_resources is invalid.")
        return {"cursor_query_param": cursor_query_param, "next_cursor_path": next_cursor_path, "max_pages": max_pages, "max_resources": max_resources}

    def _store_resource_mappings(self, integration: dict[str, Any], mappings: list[dict[str, Any]], *, kind_label: str = "OpenAPI") -> None:
        if integration.get("enabled"):
            raise PlatformOperationError(f"Disable the integration before changing {kind_label} resource mappings.")
        configuration = dict(integration.get("configuration") or {})
        manual = dict(configuration.get("manual") or {})
        manual["resource_mappings"] = mappings
        configuration["manual"] = manual
        self.integrations.configure(str(integration["id"]), configuration)

    def _refuse_mapping_change_with_bindings(self, integration_id: str, resource_types: set[str]) -> None:
        if any(binding["integration_id"] == integration_id and binding["resource_type"] in resource_types and binding["state"] not in {"DETACHED", "RETIRING"} for binding in self.bindings.list()):
            raise PlatformConflictError("Detach active resource bindings before changing or deleting this resource mapping.")

    def test_connection(self, integration_id: str, connection_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        connection = self.connections.get(integration_id, connection_id)
        if not integration["enabled"] or not connection["enabled"]:
            raise PlatformOperationError("Enable the integration and connection before testing.")
        kind = self._manual_kind(integration)
        if kind == "mcp":
            discovered = self._discover_mcp(integration_id, connection_id)
            return {"state": "HEALTHY", "protocol_version": discovered["protocol_version"], "tools": len(discovered["tools"]), "resources": len(discovered["resources"])}
        if kind == "openapi":
            operations = self.openapi_operations(integration_id, connection_id)
            self.connections.record_health(integration_id, connection_id, state="CONFIGURED", remote_version="openapi")
            return {"state": "CONFIGURED", "operations": len(operations)}
        self.connections.record_health(integration_id, connection_id, state="CONFIGURED")
        return {"state": "CONFIGURED"}

    def set_authorization(
        self,
        integration_id: str,
        *,
        permission_mode: str,
        granted_capabilities: list[str],
        requested_capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        if permission_mode not in {"restricted", "custom", "full"}:
            raise PlatformValidationError("Unsupported integration permission mode.")
        candidate = set(str(item) for item in granted_capabilities)
        if not candidate.issubset(PUBLIC_CAPABILITIES):
            raise PlatformValidationError("Only known public capabilities may be granted.")
        if permission_mode == "full" and candidate:
            raise PlatformValidationError("Full access does not accept a custom capability grant list.")
        if requested_capabilities is not None:
            if self._manual_kind(integration) is None:
                raise PlatformOperationError("Only a manual integration can change its operator-authored capability declaration.")
            if integration["enabled"]:
                raise PlatformOperationError("Disable the manual integration before changing its capability declaration.")
            declared = {str(item) for item in requested_capabilities}
            if not declared.issubset(PUBLIC_CAPABILITIES):
                raise PlatformValidationError("Only known public capabilities may be declared.")
            declared.add("network:use")
            if any(connection.get("credential_refs") for connection in self.connections.list(integration_id)):
                declared.add("credentials:use")
            if not candidate.issubset(declared):
                raise PlatformValidationError("Capabilities must be declared before they can be granted.")
            manifest = dict(integration.get("manifest") or {})
            manifest["requested_capabilities"] = sorted(declared)
            self.integrations.refresh_manifest(manifest)
            integration = self.integrations.get(integration_id)
        requested = set(str(item) for item in integration.get("requested_capabilities") or [])
        if not candidate.issubset(requested):
            raise PlatformValidationError("Capabilities must be requested by the integration manifest.")
        result = self.integrations.set_authorization(
            integration_id,
            permission_mode=permission_mode,
            granted_capabilities=sorted(candidate),
        )
        for connection in self.connections.list(integration_id):
            self.connections.record_health(
                integration_id,
                str(connection["id"]),
                state="UNKNOWN",
                error_code="authorization_changed",
            )
        return self.detail(str(result["id"]))

    def execute_openapi(
        self,
        integration_id: str,
        connection_id: str,
        operation_id: str,
        arguments: dict[str, Any],
        *,
        confirm_destructive: bool = False,
        idempotency_key: str | None = None,
        allow_disabled_integration_for_preview: bool = False,
        caller: CapabilityActor | None = None,
        invocation_context: CapabilityInvocationContext | None = None,
    ) -> Any:
        integration = self.integrations.get(integration_id)
        if self._manual_kind(integration) != "openapi":
            raise PlatformOperationError("This integration is not a manual OpenAPI integration.")
        operation = next((item for item in self.openapi_operations(integration_id, connection_id) if item["operation_id"] == operation_id), None)
        if operation is None:
            raise PlatformNotFoundError("OpenAPI operation not found.")
        if not operation["approved"]:
            raise PlatformOperationError("OpenAPI operation is not approved for this connection.")
        if operation["destructive"] and not confirm_destructive:
            raise PlatformValidationError("Destructive OpenAPI operations require explicit confirmation.")
        if caller is not None:
            require_capability(caller, "network:use")
            if invocation_context is None:
                raise PlatformValidationError("Caller-scoped OpenAPI execution requires an invocation context.")
            sdk = self._sdk_factory().create(
                caller=caller,
                integration_id=integration_id,
                connection_id=connection_id,
                project_id=invocation_context.project_id,
                invocation_context=invocation_context,
            )
        else:
            sdk = self._sdk(
                integration_id,
                connection_id,
                allow_disabled_integration_for_preview,
            )
        document = (integration.get("configuration") or {}).get("manual", {}).get("document")
        client = IntegrationOpenAPIClient(document, self._http_adapter())
        return client.execute(
            sdk, operation_id, arguments,
            connection_id=connection_id,
            idempotency_key=idempotency_key,
        )

    # -------------------------------------------------------------- mutations

    def create_manual(
        self,
        *,
        name: str,
        runtime_kind: str,
        endpoint_url: str,
        document: dict[str, Any] | str | None = None,
        credential_refs: list[str] | None = None,
        verification: dict[str, Any] | None = None,
        event_type: str | None = None,
        payload_mapping: dict[str, str] | None = None,
        integration_id: str | None = None,
        version: str = "1.0.0",
        lynewright_version: str = "^0.1.0",
        declaration_metadata: dict[str, Any] | None = None,
        settings_schema: dict[str, Any] | None = None,
        approved_openapi_operations: list[str] | None = None,
        project_id: str | None = None,
        internal_endpoint_ref: str = "",
        provenance_source: str = "",
        artifact_sha256: str = "",
        provenance_actor: str = "",
        provenance_confirmed_at: str | None = None,
    ) -> dict[str, Any]:
        runtime_kind = str(runtime_kind or "").strip().lower()
        if runtime_kind not in _MANUAL_KINDS:
            raise PlatformValidationError("Manual integration runtime must be mcp, openapi, or webhook.")
        if not name.strip() or len(name.strip()) > 120:
            raise PlatformValidationError("Manual integration name is required and bounded.")
        if runtime_kind == "openapi" and document is None:
            raise PlatformValidationError("Manual OpenAPI integrations require a document.")
        if not str(endpoint_url or "").strip() and not internal_endpoint_ref:
            raise PlatformValidationError("Manual integrations require an endpoint URL or internal endpoint reference.")
        normalized_document = document
        if runtime_kind == "openapi":
            # Validation happens before persistence; the helper is the single
            # supported OpenAPI interpreter used by manual integrations.
            client = IntegrationOpenAPIClient(document or {}, self._http_adapter())
            normalized_document = client.document
            requested_approvals = set(approved_openapi_operations or [])
            if requested_approvals - {item.operation_id for item in client.operations()}:
                raise PlatformValidationError("OpenAPI approval references an unknown operation.")
        webhook_configuration: dict[str, Any] | None = None
        if runtime_kind == "webhook":
            webhook_configuration = self._validate_manual_webhook(
                verification=verification,
                event_type=event_type,
                payload_mapping=payload_mapping,
                credential_refs=credential_refs or [],
            )
        slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:48] or "integration"
        integration_id = integration_id or f"lynewright.manual.{slug}-{__import__('secrets').token_hex(6)}"
        requested = ["network:use"]
        if credential_refs:
            requested.append("credentials:use")
        manifest = {
            "schema_version": "1.2",
            "id": integration_id,
            "name": name.strip(),
            "version": version,
            "type": "service",
            "lynewright_version": lynewright_version,
            "sdk_version": "^1.0.0",
            "requested_capabilities": requested,
            "service": {"kind": "http", "health_check": "/health"},
            "backend": "manual-runtime",
            "management": {
                "sections": ["overview", "connections", "operations", "external_resources", "bindings", "health", "diagnostics"],
                "supports_webhooks": runtime_kind == "webhook",
                "supports_health": True,
                "supports_diagnostics": True,
                "supports_settings": bool(settings_schema),
            },
        }
        if is_reserved_first_party_feature_id(integration_id):
            raise PlatformConflictError("Manual integrations cannot claim a reserved first-party feature identity.")
        configuration = {"manual": {"runtime": runtime_kind}}
        if normalized_document is not None:
            configuration["manual"]["document"] = normalized_document
        if declaration_metadata:
            configuration["manual"]["declaration"] = declaration_metadata
        if settings_schema:
            configuration["manual"]["settings_schema"] = settings_schema
        created = self.integrations.register(manifest, configuration=configuration)
        try:
            # The connection table uses a tenant-wide primary key. Preserve the
            # conventional first id while avoiding a collision with any existing
            # integration's primary connection.
            with self.store.connection() as db:
                self.connections.ensure_schema(db)
                primary_exists = db.execute(
                    "SELECT 1 FROM platform_integration_connections WHERE tenant_id=? AND id='primary' LIMIT 1",
                    (self.tenant_id,),
                ).fetchone() is not None
            default_connection_id = "primary" if not primary_exists else f"primary-{secrets.token_hex(6)}"
            connection_configuration: dict[str, Any] = {}
            if runtime_kind == "openapi" and approved_openapi_operations:
                connection_configuration["openapi"] = {
                    "approved_operations": sorted(set(approved_openapi_operations)),
                }
            if runtime_kind == "mcp":
                connection_configuration["mcp"] = {"approved_tools": []}
            if webhook_configuration is not None:
                connection_configuration["webhook"] = webhook_configuration
            self.connections.create(
                integration_id,
                connection_id=default_connection_id,
                name="Primary",
                endpoint_url=endpoint_url,
                credential_refs=credential_refs or [],
                project_id=project_id,
                configuration=connection_configuration,
                enabled=True,
                origin_kind="internal_endpoint" if internal_endpoint_ref else "public",
                internal_endpoint_ref=internal_endpoint_ref,
            )
            source = provenance_source or f"manual:{runtime_kind}"
            record_provenance(
                self.store, self.tenant_id, integration_id, kind="outside",
                source=source, confirmed_by=provenance_actor or self.actor_id,
                confirmed_at=provenance_confirmed_at,
                entry_version=version, sha256=artifact_sha256 or None,
            )
        except Exception:
            # This record is new and was never activated. Roll back any partial
            # local projections as well as its source record.
            try:
                for connection in self.connections.list(integration_id):
                    self.connections.remove(integration_id, str(connection["id"]))
            except Exception:
                pass
            try:
                record_provenance(self.store, self.tenant_id, integration_id, kind=None)
            except Exception:
                pass
            self.integrations.uninstall(integration_id)
            raise
        return self.detail(str(created["id"]))

    def _validate_manual_webhook(
        self,
        *,
        verification: dict[str, Any] | None,
        event_type: str | None,
        payload_mapping: dict[str, str] | None,
        credential_refs: list[str],
    ) -> dict[str, Any]:
        if not isinstance(verification, dict):
            raise PlatformValidationError("Manual webhook integrations require verification settings.")
        mode = str(verification.get("mode") or "")
        if mode not in {"hmac_sha256", "shared_secret"}:
            raise PlatformValidationError("Manual webhooks require HMAC-SHA256 or shared-secret verification.")
        credential_ref = str(verification.get("credential_ref") or "").strip()
        if not credential_ref or credential_ref not in credential_refs:
            raise PlatformValidationError("Webhook verification must reference one supplied credential ref.")
        if not self.connections.credentials.get_status(credential_ref).get("configured"):
            raise PlatformValidationError("Webhook verification credential is not configured.")
        for key in ("signature_header", "timestamp_header", "delivery_header"):
            value = str(verification.get(key) or "")
            if value and _WEBHOOK_HEADER.fullmatch(value) is None:
                raise PlatformValidationError(f"Webhook {key} is invalid.")
        tolerance = verification.get("tolerance_seconds", 300)
        if not isinstance(tolerance, int) or not 1 <= tolerance <= 86_400:
            raise PlatformValidationError("Webhook timestamp tolerance must be between one second and one day.")
        normalized_event_type = str(event_type or "").strip()
        if _WEBHOOK_ID.fullmatch(normalized_event_type) is None:
            raise PlatformValidationError("Webhook event_type must be a bounded lowercase identifier.")
        mapping = payload_mapping or {}
        if not isinstance(mapping, dict) or len(mapping) > 50:
            raise PlatformValidationError("Webhook payload mapping is invalid.")
        for target, source in mapping.items():
            if _WEBHOOK_ID.fullmatch(str(target)) is None or not re.fullmatch(r"^[A-Za-z0-9_.-]{1,200}$", str(source)):
                raise PlatformValidationError("Webhook payload mapping must contain bounded field paths.")
        return {
            "handler_id": "inbound",
            "verification": {
                "mode": mode,
                "credential_ref": credential_ref,
                "signature_header": str(verification.get("signature_header") or "x-signature"),
                "timestamp_header": str(verification.get("timestamp_header") or ""),
                "tolerance_seconds": tolerance,
                "delivery_header": str(verification.get("delivery_header") or ""),
            },
            "event_type": normalized_event_type,
            "payload_mapping": {str(key): str(value) for key, value in mapping.items()},
        }

    def set_integration_enabled(self, integration_id: str, *, enabled: bool) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        if bool(integration["enabled"]) == enabled:
            raise PlatformOperationError("Integration is already in the requested enabled state.")
        if enabled:
            provenance = get_provenance(self.store, self.tenant_id, integration_id)
            if provenance and provenance.get("kind") == "outside":
                if provenance.get("status") == "awaiting_confirmation":
                    raise PlatformOperationError(
                        "This outside package is awaiting admin confirmation before it can be enabled."
                    )
                if current_install_policy() == "catalogue-only":
                    raise InstallPolicyForbidden()
        product_metadata = product_metadata_for(integration_id)
        if enabled and (
            product_metadata["product_role"] == PRODUCT_ROLE_FIRST_PARTY_FEATURE
            and not product_metadata["available"]
        ):
            raise PlatformOperationError("This first-party feature package is not shipped in the current build.")
        declares_runtime = manifest_declares_runtime(integration.get("manifest") or {})
        if self.lifecycle is not None and self._manual_kind(integration) is None and declares_runtime:
            if enabled:
                self.ensure_runtime_definition_registered(integration_id)
                result = self.lifecycle.enable(integration_id)
            elif self.lifecycle.is_active(integration_id):
                result = self.lifecycle.disable(integration_id)
            else:
                # A disabled/unavailable package may have no runtime
                # definition registered in this process. Disabling the durable
                # operator intent still needs to work in that state.
                result = self.integrations.disable(integration_id)
        else:
            result = self.integrations.enable(integration_id) if enabled else self.integrations.disable(integration_id)
        return self.detail(str(result["id"]))

    def create_connection(self, integration_id: str, **values: Any) -> dict[str, Any]:
        self.integrations.get(integration_id)
        return self.connections.create(integration_id, **values)

    def configure_connection(self, integration_id: str, connection_id: str, **values: Any) -> dict[str, Any]:
        self.integrations.get(integration_id)
        return self.connections.configure(integration_id, connection_id, **values)

    def set_connection_enabled(self, integration_id: str, connection_id: str, *, enabled: bool) -> dict[str, Any]:
        return self.connections.set_enabled(integration_id, connection_id, enabled=enabled)

    def remove_connection(self, integration_id: str, connection_id: str) -> None:
        # A connection with a durable binding is not silently removed. The
        # operator must detach those canonical relationships first.
        active = [item for item in self.bindings.list() if item["integration_id"] == integration_id and item["connection_id"] == connection_id and item["state"] not in {"DETACHED", "RETIRING"}]
        if active:
            raise PlatformConflictError("Detach resource bindings before removing this connection.")
        self.connections.remove(integration_id, connection_id)

    def uninstall_manual(self, integration_id: str) -> None:
        """Remove a disabled, unbound manual integration without touching its provider.

        This deliberately uses the registry's uninstall primitive for the final
        registry removal. All checks happen before local projections are cleaned
        so an unsafe uninstall cannot alter the integration at all.
        """
        integration = self.integrations.get(integration_id)
        if self._manual_kind(integration) is None:
            raise PlatformOperationError("Only manual integrations can be uninstalled here.")
        if integration["enabled"]:
            raise PlatformOperationError("Disable the integration before uninstalling it.")
        bindings = [item for item in self.bindings.list() if item["integration_id"] == integration_id]
        if any(item["state"] != "DETACHED" for item in bindings):
            raise PlatformConflictError("Detach or complete all resource lifecycle work before uninstalling this integration.")
        with self.store.connection() as db:
            pending_action = db.execute(
                """SELECT 1 FROM platform_integration_resource_actions
                   WHERE tenant_id=? AND integration_id=? AND status='in_progress' LIMIT 1""",
                (self.tenant_id, integration_id),
            ).fetchone()
        if pending_action is not None:
            raise PlatformConflictError("Complete pending resource mutations before uninstalling this integration.")

        # These are local projections/configuration only. No provider client is
        # constructed or called; credentials intentionally remain in the vault.
        self.inventory.remove_integration(integration_id)
        for connection in self.connections.list(integration_id):
            self.connections.remove(integration_id, str(connection["id"]))
        with self.store.connection() as db, db:
            for table in ("platform_integration_oauth_states", "platform_integration_oauth_tokens"):
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if exists is not None:
                    db.execute(f"DELETE FROM {table} WHERE tenant_id=? AND integration_id=?", (self.tenant_id, integration_id))
            db.execute(
                "DELETE FROM platform_integration_resource_actions WHERE tenant_id=? AND integration_id=?",
                (self.tenant_id, integration_id),
            )
        self.integrations.uninstall(integration_id)
        record_provenance(self.store, self.tenant_id, integration_id, kind=None)

    def bind(
        self,
        inventory_id: str,
        *,
        project_id: str | None,
        resource_type: str,
        resource_id: str,
        role: str,
        ownership_mode: str,
    ) -> dict[str, Any]:
        remote = self.inventory.get(inventory_id)
        if remote["resource_type"] != resource_type:
            raise PlatformValidationError("Binding resource type does not match the discovered resource.")
        integration = self.integrations.get(remote["integration_id"])
        connection = self.connections.get(remote["integration_id"], str(remote["connection_id"]))
        descriptor = next((item for item in self._resource_descriptors(self._descriptor(self._manifest(integration)), integration, connection) if item["type"] == resource_type), None)
        if descriptor is None or not descriptor["bindable"] or "bind" not in descriptor["operations"]:
            raise PlatformOperationError("This resource type does not support binding.")
        return self.bindings.create(
            project_id=project_id,
            resource_type=resource_type,
            resource_id=resource_id,
            role=role,
            integration_id=remote["integration_id"],
            connection_id=remote["connection_id"],
            ownership_mode=ownership_mode,  # type: ignore[arg-type]
            state="PENDING",
            external_resource_id=remote["external_id"],
            external_resource_url=remote["url"],
            metadata={"inventory_id": inventory_id, "external_name": remote["name"]},
        )

    def unbind(self, binding_id: str, *, expected_revision: int) -> dict[str, Any]:
        binding = self.bindings.get(binding_id)
        return self.bindings.transition(binding_id, state="DETACHED", expected_revision=expected_revision, metadata={**binding.get("metadata", {}), "detached_by_operator": True})

    def resource_action(
        self,
        integration_id: str,
        inventory_id: str,
        *,
        action: str,
        arguments: dict[str, Any] | None = None,
        confirm_destructive: bool = False,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Execute one descriptor-declared remote resource action.

        Only a provider package's declared service contract can execute the
        remote call.  Destructive actions additionally require a managed active
        binding, an expected binding revision, explicit confirmation, and an
        idempotency key.  A failed remote call never changes the binding.
        """
        normalized_action = str(action or "").strip().lower()
        if _ACTION_ID.fullmatch(normalized_action) is None:
            raise PlatformValidationError("External resource action is invalid.")
        remote = self.inventory.get(inventory_id)
        if remote["integration_id"] != integration_id:
            raise PlatformNotFoundError("External resource was not found for this integration.")
        integration = self.integrations.get(integration_id)
        descriptor = next(
            (
                item for item in self._resource_descriptors(
                    self._descriptor(self._manifest(integration)), integration,
                    self.connections.get(integration_id, str(remote["connection_id"])),
                ) if item["type"] == remote["resource_type"]
            ),
            None,
        )
        if descriptor is None or normalized_action not in descriptor["operations"]:
            raise PlatformOperationError("This external resource action is not declared by the integration.")
        advertised = set(str(value) for value in remote.get("available_actions") or [])
        if advertised and normalized_action not in advertised:
            raise PlatformOperationError("This external resource does not advertise that action.")
        values = arguments or {}
        if not isinstance(values, dict):
            raise PlatformValidationError("External resource action arguments must be an object.")
        try:
            if len(json.dumps(values, sort_keys=True).encode("utf-8")) > 64 * 1024:
                raise PlatformValidationError("External resource action arguments exceed the 64 KiB limit.")
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("External resource action arguments must be JSON serializable.") from exc

        if normalized_action in _DESTRUCTIVE_ACTIONS:
            if not confirm_destructive:
                raise PlatformValidationError("Destructive external resource actions require explicit confirmation.")
            if not idempotency_key or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
                raise PlatformValidationError("Destructive external resource actions require a valid idempotency key.")
            cached = self._completed_resource_action(
                integration_id=integration_id,
                inventory_id=inventory_id,
                action=normalized_action,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
            if cached is not None:
                return cached

        binding = self._binding_for_remote(remote)
        if normalized_action in _MUTATING_ACTIONS and normalized_action != "create" and binding is None:
            raise PlatformOperationError("Bind the external resource before changing or removing it.")
        if normalized_action in _MUTATING_ACTIONS and normalized_action != "create":
            assert binding is not None
            if binding["ownership_mode"] != "managed":
                raise PlatformOperationError("Only managed bindings may use remote resource mutations.")
            if expected_revision is None:
                raise PlatformValidationError("Mutating a bound resource requires its expected revision.")
            if binding["revision"] != expected_revision:
                raise PlatformConflictError("Resource binding revision is stale.")
        if normalized_action in _DESTRUCTIVE_ACTIONS:
            assert idempotency_key is not None
        if self._manual_kind(integration) == "mcp":
            return self._manual_mcp_resource_action(
                integration=integration,
                remote=remote,
                binding=binding,
                action=normalized_action,
                arguments=values,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        if self._manual_kind(integration) == "openapi":
            return self._manual_openapi_resource_action(
                integration=integration,
                remote=remote,
                binding=binding,
                action=normalized_action,
                arguments=values,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        if normalized_action == "read" and not descriptor.get("operation_contract"):
            return {"action": normalized_action, "resource": remote, "binding": binding}

        contract = descriptor.get("operation_contract")
        operation_names = descriptor.get("operation_names") if isinstance(descriptor.get("operation_names"), dict) else {}
        operation = operation_names.get(normalized_action) if isinstance(operation_names, dict) else None
        if not contract or not operation:
            raise PlatformOperationError("This resource action has no host-owned provider operation.")

        if normalized_action in _DESTRUCTIVE_ACTIONS:
            cached = self._reserve_resource_action(
                integration_id=integration_id,
                inventory_id=inventory_id,
                binding=binding,
                action=normalized_action,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key or "",
            )
            if cached is not None:
                return cached

        project_id = binding.get("project_id") if binding else None
        context = CapabilityInvocationContext.from_actor(
            tenant_id=self.tenant_id,
            actor_type="operator",
            actor_id="integration-management",
            entry_point="integration-management.resource-action",
            project_id=project_id,
            resource_type=str(remote["resource_type"]),
            resource_id=str(remote["external_id"]),
            integration_id=integration_id,
            connection_id=str(remote["connection_id"]),
            idempotency_key=idempotency_key,
        )
        try:
            result = IntegrationServiceContractRegistry(
                self.contributions,
                sdk_factory=self._sdk_factory(),
            ).invoke(
                str(contract),
                str(operation),
                {
                    "resource_type": remote["resource_type"],
                    "external_resource_id": remote["external_id"],
                    "resource": remote,
                    "arguments": values,
                },
                project_id=project_id,
                actor=self._host_actor(),
                invocation_context=context,
                provider_integration_id=integration_id,
            )
        except Exception as exc:
            if normalized_action in _DESTRUCTIVE_ACTIONS:
                self._finish_resource_action(
                    idempotency_key=idempotency_key or "",
                    status="failed",
                    result=None,
                    error=type(exc).__name__,
                )
            if isinstance(exc, PlatformServiceError):
                raise
            raise PlatformOperationError("External resource action failed.") from exc

        safe_result = self._safe_action_result(result)
        updated_binding = binding
        updated_remote = remote
        if normalized_action == "update":
            # A successful provider update must immediately reconcile the
            # inventory so its name/domain/status remain provider truth.
            self.discover(integration_id, str(remote["connection_id"]))
            updated_remote = self.inventory.get(inventory_id)
        if normalized_action in _DESTRUCTIVE_ACTIONS and binding is not None:
            # Deletion is authoritative even if the provider's subsequent
            # list call is briefly stale; reconcile first, then mark the
            # known inventory row missing deterministically.
            try:
                self.discover(integration_id, str(remote["connection_id"]))
            except Exception:
                pass
            updated_binding = self.bindings.transition(
                binding["id"],
                state="RETIRING",
                expected_revision=int(binding["revision"]),
                metadata={
                    **binding.get("metadata", {}),
                    "last_remote_action": normalized_action,
                    "last_remote_action_completed_at": datetime.now(UTC).isoformat(),
                },
            )
            updated_remote = self.inventory.mark_remote_missing(inventory_id, action=normalized_action)
        response = {
            "action": normalized_action,
            "inventory": updated_remote,
            "binding": updated_binding,
            "result": safe_result,
        }
        if normalized_action in _DESTRUCTIVE_ACTIONS:
            self._finish_resource_action(
                idempotency_key=idempotency_key or "",
                status="completed",
                result=response,
                error="",
            )
        return response

    def create_resource(
        self,
        integration_id: str,
        connection_id: str,
        resource_type: str,
        *,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create one provider-owned resource through its manifest contract.

        Creation has no inventory id yet, so it is deliberately a separate
        bounded entrypoint from ``resource_action``. The provider response is
        followed by one normal discovery refresh; the inventory remains the
        source of truth for the resource's subsequent bind/update/delete flow.
        """
        integration = self.integrations.get(integration_id)
        if not integration["enabled"]:
            raise PlatformOperationError("Enable the integration before creating a remote resource.")
        connection = self.connections.get(integration_id, connection_id)
        if not connection["enabled"]:
            raise PlatformOperationError("Enable the connection before creating a remote resource.")
        descriptor = next(
            (item for item in self._resource_descriptors(self._descriptor(self._manifest(integration)), integration, connection)
             if item["type"] == resource_type),
            None,
        )
        if descriptor is None or "create" not in descriptor["operations"]:
            raise PlatformOperationError("This resource type does not declare a create operation.")
        if self._manual_kind(integration) == "mcp":
            return self._manual_mcp_resource_create(
                integration=integration,
                connection=connection,
                resource_type=resource_type,
                arguments=arguments,
            )
        contract = descriptor.get("operation_contract")
        operation = (descriptor.get("operation_names") or {}).get("create")
        if not contract or not operation:
            raise PlatformOperationError("This resource action has no host-owned provider operation.")
        values = arguments or {}
        if not isinstance(values, dict):
            raise PlatformValidationError("Resource creation arguments must be an object.")
        values = self._validated_resource_arguments(descriptor, values)
        try:
            if len(json.dumps(values, sort_keys=True).encode("utf-8")) > 64 * 1024:
                raise PlatformValidationError("Resource creation arguments exceed the 64 KiB limit.")
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("Resource creation arguments must be JSON serializable.") from exc
        context = CapabilityInvocationContext.from_actor(
            tenant_id=self.tenant_id,
            actor_type="operator",
            actor_id="integration-management",
            entry_point="integration-management.resource-create",
            integration_id=integration_id,
            connection_id=connection_id,
        )
        try:
            result = IntegrationServiceContractRegistry(
                self.contributions, sdk_factory=self._sdk_factory(),
            ).invoke(
                str(contract), str(operation),
                {"resource_type": resource_type, "arguments": values},
                actor=self._host_actor(), invocation_context=context,
                provider_integration_id=integration_id,
            )
        except PlatformServiceError:
            raise
        except Exception as exc:
            raise PlatformOperationError("External resource creation failed.") from exc
        refreshed = self.discover(integration_id, connection_id)
        return {"action": "create", "result": self._safe_action_result(result), "refresh": refreshed}

    def create_openapi_resource(
        self,
        integration_id: str,
        connection_id: str,
        resource_type: str,
        *,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self._require_manual_openapi(integration)
        if not integration["enabled"]:
            raise PlatformOperationError("Enable the integration before creating a remote resource.")
        connection = self.connections.get(integration_id, connection_id)
        if not connection["enabled"]:
            raise PlatformOperationError("Enable the connection before creating a remote resource.")
        mapping = next((item for item in self._resource_mappings(integration) if item["resource_type"] == resource_type and item["enabled"]), None)
        if mapping is None or not mapping.get("create_operation_id"):
            raise PlatformOperationError("This resource type does not declare a create operation.")
        operations = {item["operation_id"]: item for item in self.openapi_operations(integration_id, connection_id)}
        operation = operations.get(mapping["create_operation_id"])
        if operation is None or not operation["approved"]:
            raise PlatformOperationError("Approve the mapped create operation on this connection before creating resources.")
        values = arguments or {}
        if not isinstance(values, dict):
            raise PlatformValidationError("OpenAPI create arguments must be an object.")
        result = self.execute_openapi(integration_id, connection_id, mapping["create_operation_id"], values, confirm_destructive=True)
        refresh = self.discover(integration_id, connection_id)
        return {"action": "create", "result": self._safe_action_result({"response": result}), "refresh": refresh}

    def _manual_mcp_resource_create(
        self,
        *,
        integration: dict[str, Any],
        connection: dict[str, Any],
        resource_type: str,
        arguments: dict[str, Any] | None,
    ) -> dict[str, Any]:
        mapping = next(
            (item for item in self._mcp_resource_mappings(integration, connection)
             if item["resource_type"] == resource_type and item["enabled"]),
            None,
        )
        if mapping is None or not mapping.get("create_tool_name"):
            raise PlatformOperationError("This MCP resource type does not declare a create tool.")
        tool_name = str(mapping["create_tool_name"])
        approved = set((connection.get("configuration") or {}).get("mcp", {}).get("approved_tools", []))
        if tool_name not in approved:
            raise PlatformOperationError("Approve the mapped MCP create tool before creating resources.")
        values = arguments or {}
        if not isinstance(values, dict):
            raise PlatformValidationError("MCP resource creation arguments must be an object.")
        try:
            if len(json.dumps(values, sort_keys=True).encode("utf-8")) > 64 * 1024:
                raise PlatformValidationError("MCP resource creation arguments exceed the 64 KiB limit.")
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("MCP resource creation arguments must be JSON serializable.") from exc
        try:
            result = self.mcp.call_tool(
                self._sdk(str(integration["id"]), str(connection["id"])),
                tool_name=tool_name,
                arguments=values,
                connection_id=str(connection["id"]),
            )
        except PlatformServiceError:
            raise
        except Exception as exc:
            raise PlatformOperationError("MCP resource creation failed.") from exc
        refresh = self.discover(str(integration["id"]), str(connection["id"]))
        return {"action": "create", "result": self._safe_action_result({"response": result}), "refresh": refresh}

    def _manual_mcp_resource_action(
        self,
        *,
        integration: dict[str, Any],
        remote: dict[str, Any],
        binding: dict[str, Any] | None,
        action: str,
        arguments: dict[str, Any],
        expected_revision: int | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        integration_id = str(integration["id"])
        connection_id = str(remote["connection_id"])
        if not integration["enabled"]:
            raise PlatformOperationError("Enable the integration before executing a remote resource action.")
        connection = self.connections.get(integration_id, connection_id)
        if not connection["enabled"]:
            raise PlatformOperationError("Enable the connection before executing a remote resource action.")
        mapping = next(
            (item for item in self._mcp_resource_mappings(integration, connection)
             if item["resource_type"] == remote["resource_type"] and item["enabled"]),
            None,
        )
        if mapping is None:
            raise PlatformOperationError("No enabled MCP resource mapping exists for this resource.")
        if action == "read" and not mapping.get("get_tool_name"):
            return {"action": action, "resource": remote, "binding": binding}
        operation_key = {
            "read": "get_tool_name", "update": "update_tool_name", "delete": "delete_tool_name",
        }.get(action)
        target_key = {
            "read": "get_external_id_target", "update": "update_external_id_target", "delete": "delete_external_id_target",
        }.get(action)
        if operation_key is None or target_key is None or not mapping.get(operation_key) or not mapping.get(target_key):
            raise PlatformOperationError("This MCP resource action is not declared by the mapping.")
        tool_name = str(mapping[operation_key])
        approved = set((connection.get("configuration") or {}).get("mcp", {}).get("approved_tools", []))
        if tool_name not in approved:
            raise PlatformOperationError("Approve the mapped MCP resource action tool before executing this action.")
        request_arguments = self._mcp_arguments_with_external_id(arguments, str(mapping[target_key]), str(remote["external_id"]))
        if action in _DESTRUCTIVE_ACTIONS:
            assert idempotency_key is not None
            cached = self._reserve_resource_action(
                integration_id=integration_id,
                inventory_id=str(remote["id"]),
                binding=binding,
                action=action,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
            if cached is not None:
                return cached
        try:
            result = self.mcp.call_tool(
                self._sdk(integration_id, connection_id),
                tool_name=tool_name,
                arguments=request_arguments,
                connection_id=connection_id,
            )
        except Exception as exc:
            if action in _DESTRUCTIVE_ACTIONS:
                self._finish_resource_action(
                    idempotency_key=idempotency_key or "", status="failed", result=None, error=type(exc).__name__,
                )
            if isinstance(exc, PlatformServiceError):
                raise
            raise PlatformOperationError("Mapped MCP resource action failed.") from exc
        safe_result = self._safe_action_result({"response": result})
        updated_remote = remote
        updated_binding = binding
        if action == "delete" and binding is not None:
            updated_binding = self.bindings.transition(
                binding["id"], state="RETIRING", expected_revision=int(binding["revision"]),
                metadata={
                    **binding.get("metadata", {}), "last_remote_action": action,
                    "last_remote_action_completed_at": datetime.now(UTC).isoformat(),
                },
            )
            updated_remote = self.inventory.mark_remote_missing(str(remote["id"]), action=action)
        elif action == "update":
            self.discover(integration_id, connection_id)
            updated_remote = self.inventory.get(str(remote["id"]))
        response = {"action": action, "inventory": updated_remote, "binding": updated_binding, "result": safe_result}
        if action in _DESTRUCTIVE_ACTIONS:
            self._finish_resource_action(
                idempotency_key=idempotency_key or "", status="completed", result=response, error="",
            )
        return response

    def _manual_openapi_resource_action(
        self,
        *,
        integration: dict[str, Any],
        remote: dict[str, Any],
        binding: dict[str, Any] | None,
        action: str,
        arguments: dict[str, Any],
        expected_revision: int | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        integration_id = str(integration["id"])
        connection_id = str(remote["connection_id"])
        if not integration["enabled"]:
            raise PlatformOperationError("Enable the integration before executing a remote resource action.")
        if not self.connections.get(integration_id, connection_id)["enabled"]:
            raise PlatformOperationError("Enable the connection before executing a remote resource action.")
        mapping = next((item for item in self._resource_mappings(integration) if item["resource_type"] == remote["resource_type"] and item["enabled"]), None)
        if mapping is None:
            raise PlatformOperationError("No enabled OpenAPI resource mapping exists for this resource.")
        operation_key = {"read": "get_operation_id", "update": "update_operation_id", "delete": "delete_operation_id"}.get(action)
        target_key = {"read": "get_external_id_target", "update": "update_external_id_target", "delete": "delete_external_id_target"}.get(action)
        if operation_key is None or target_key is None or not mapping.get(operation_key) or not mapping.get(target_key):
            raise PlatformOperationError("This OpenAPI resource action is not declared by the mapping.")
        operations = {item["operation_id"]: item for item in self.openapi_operations(integration_id, connection_id)}
        operation = operations.get(mapping[operation_key])
        if operation is None or not operation["approved"]:
            raise PlatformOperationError("Approve the mapped OpenAPI operation on this connection before executing this action.")
        request_arguments = self._openapi_arguments_with_external_id(arguments, str(mapping[target_key]), str(remote["external_id"]))
        if action in _DESTRUCTIVE_ACTIONS:
            assert idempotency_key is not None
            cached = self._reserve_resource_action(
                integration_id=integration_id,
                inventory_id=str(remote["id"]),
                binding=binding,
                action=action,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
            if cached is not None:
                return cached
        try:
            result = self.execute_openapi(
                integration_id, connection_id, str(mapping[operation_key]), request_arguments,
                confirm_destructive=action in _DESTRUCTIVE_ACTIONS,
            )
        except Exception as exc:
            if action in _DESTRUCTIVE_ACTIONS:
                self._finish_resource_action(idempotency_key=idempotency_key or "", status="failed", result=None, error=type(exc).__name__)
            if isinstance(exc, PlatformServiceError):
                raise
            raise PlatformOperationError("Mapped OpenAPI resource action failed.") from exc
        safe_result = self._safe_action_result({"response": result})
        updated_remote = remote
        updated_binding = binding
        if action == "delete" and binding is not None:
            updated_binding = self.bindings.transition(
                binding["id"], state="RETIRING", expected_revision=int(binding["revision"]),
                metadata={**binding.get("metadata", {}), "last_remote_action": action, "last_remote_action_completed_at": datetime.now(UTC).isoformat()},
            )
            updated_remote = self.inventory.mark_remote_missing(str(remote["id"]), action=action)
        elif action == "update":
            self.discover(integration_id, connection_id)
            updated_remote = self.inventory.get(str(remote["id"]))
        response = {"action": action, "inventory": updated_remote, "binding": updated_binding, "result": safe_result}
        if action in _DESTRUCTIVE_ACTIONS:
            self._finish_resource_action(idempotency_key=idempotency_key or "", status="completed", result=response, error="")
        return response

    @staticmethod
    def _openapi_arguments_with_external_id(arguments: dict[str, Any], target: str, external_id: str) -> dict[str, Any]:
        try:
            value = json.loads(json.dumps(arguments))
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("OpenAPI resource action arguments must be JSON serializable.") from exc
        if _OPENAPI_INPUT_TARGET.fullmatch(target) is None:
            raise PlatformValidationError("OpenAPI resource mapping input target is invalid.")
        section, *segments = target.split(".")
        current = value.setdefault(section, {})
        if not isinstance(current, dict):
            raise PlatformValidationError(f"OpenAPI action {section} arguments must be an object.")
        for segment in segments[:-1]:
            nested = current.setdefault(segment, {})
            if not isinstance(nested, dict):
                raise PlatformValidationError("OpenAPI resource mapping input target conflicts with supplied arguments.")
            current = nested
        current[segments[-1]] = external_id
        return value

    # -------------------------------------------------------------- internals

    def _ensure_action_schema(self) -> None:
        with self.store.connection() as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS platform_integration_resource_actions (
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    integration_id TEXT NOT NULL,
                    inventory_id TEXT NOT NULL,
                    binding_id TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    expected_revision INTEGER,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, idempotency_key)
                )""",
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS platform_integration_contract_actions (
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    integration_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    mapping_id TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, idempotency_key)
                )""",
            )

    def _reserve_contract_action(
        self,
        *,
        integration_id: str,
        connection_id: str,
        mapping_id: str,
        idempotency_key: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        arguments_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.store.connection() as db, db:
            row = db.execute(
                """SELECT * FROM platform_integration_contract_actions
                   WHERE tenant_id=? AND idempotency_key=?""",
                (self.tenant_id, idempotency_key),
            ).fetchone()
            if row is not None:
                identity = (integration_id, connection_id, mapping_id, arguments_hash)
                if identity != (row["integration_id"], row["connection_id"], row["mapping_id"], row["arguments_hash"]):
                    raise PlatformConflictError("Idempotency key is already assigned to a different contract action.")
                if row["status"] == "completed":
                    return self._json_result(row["result_json"])
                raise PlatformConflictError("This contract action is already in progress or has failed for that idempotency key.")
            db.execute(
                """INSERT INTO platform_integration_contract_actions (
                    tenant_id,idempotency_key,integration_id,connection_id,mapping_id,arguments_hash,status
                ) VALUES (?,?,?,?,?,?, 'in_progress')""",
                (self.tenant_id, idempotency_key, integration_id, connection_id, mapping_id, arguments_hash),
            )
        return None

    def _finish_contract_action(
        self,
        *,
        idempotency_key: str,
        status: str,
        result: dict[str, Any] | None,
        error: str,
    ) -> None:
        safe_result = self._safe_action_result(result or {})
        with self.store.connection() as db, db:
            db.execute(
                """UPDATE platform_integration_contract_actions
                   SET status=?,result_json=?,error=?,updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND idempotency_key=?""",
                (
                    status,
                    json.dumps(safe_result, sort_keys=True, default=str),
                    str(error)[:500],
                    self.tenant_id,
                    idempotency_key,
                ),
            )

    def _binding_for_remote(self, remote: dict[str, Any]) -> dict[str, Any] | None:
        for binding in self.bindings.list():
            if (
                binding["integration_id"] == remote["integration_id"]
                and binding["connection_id"] == remote["connection_id"]
                and binding["resource_type"] == remote["resource_type"]
                and binding["external_resource_id"] == remote["external_id"]
                and binding["state"] not in {"DETACHED", "RETIRING"}
            ):
                return binding
        return None

    def _reserve_resource_action(
        self,
        *,
        integration_id: str,
        inventory_id: str,
        binding: dict[str, Any] | None,
        action: str,
        expected_revision: int | None,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        with self.store.connection() as db, db:
            row = db.execute(
                """SELECT * FROM platform_integration_resource_actions
                   WHERE tenant_id=? AND idempotency_key=?""",
                (self.tenant_id, idempotency_key),
            ).fetchone()
            if row is not None:
                if (
                    row["integration_id"] != integration_id
                    or row["inventory_id"] != inventory_id
                    or row["action"] != action
                    or row["expected_revision"] != expected_revision
                ):
                    raise PlatformConflictError("Idempotency key is already assigned to a different resource action.")
                if row["status"] == "completed":
                    return self._json_result(row["result_json"])
                raise PlatformConflictError("This resource action is already in progress or has failed for that idempotency key.")
            db.execute(
                """INSERT INTO platform_integration_resource_actions (
                    tenant_id,idempotency_key,integration_id,inventory_id,binding_id,
                    action,expected_revision,status
                ) VALUES (?,?,?,?,?,?,?, 'in_progress')""",
                (
                    self.tenant_id, idempotency_key, integration_id, inventory_id,
                    str(binding["id"]) if binding else "", action, expected_revision,
                ),
            )
        return None

    def _completed_resource_action(
        self,
        *,
        integration_id: str,
        inventory_id: str,
        action: str,
        expected_revision: int | None,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        with self.store.connection() as db:
            row = db.execute(
                """SELECT * FROM platform_integration_resource_actions
                   WHERE tenant_id=? AND idempotency_key=?""",
                (self.tenant_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if (
            row["integration_id"] != integration_id
            or row["inventory_id"] != inventory_id
            or row["action"] != action
            or row["expected_revision"] != expected_revision
        ):
            raise PlatformConflictError("Idempotency key is already assigned to a different resource action.")
        if row["status"] == "completed":
            return self._json_result(row["result_json"])
        raise PlatformConflictError("This resource action is already in progress or has failed for that idempotency key.")

    def _finish_resource_action(
        self,
        *,
        idempotency_key: str,
        status: str,
        result: dict[str, Any] | None,
        error: str,
    ) -> None:
        safe_result = self._safe_action_result(result or {})
        with self.store.connection() as db, db:
            db.execute(
                """UPDATE platform_integration_resource_actions
                   SET status=?,result_json=?,error=?,updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND idempotency_key=?""",
                (
                    status, json.dumps(safe_result, sort_keys=True, default=str),
                    str(error)[:500], self.tenant_id, idempotency_key,
                ),
            )

    @staticmethod
    def _safe_action_result(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PlatformValidationError("Resource action providers must return an object.")
        safe = _redact(value)
        try:
            encoded = json.dumps(safe, sort_keys=True, default=str)
        except (TypeError, ValueError, RecursionError) as exc:
            raise PlatformValidationError("Resource action result must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise PlatformValidationError("Resource action result exceeds the 64 KiB limit.")
        return safe

    @staticmethod
    def _json_result(value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(str(value or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlatformOperationError("Stored resource action result is invalid.") from exc
        if not isinstance(parsed, dict):
            raise PlatformOperationError("Stored resource action result is invalid.")
        return parsed

    def _discover_mcp(self, integration_id: str, connection_id: str) -> dict[str, Any]:
        sdk = self._sdk(integration_id, connection_id)
        return self.mcp.discover(sdk, connection_id=connection_id)

    def _invoke_discovery(self, integration_id: str, connection_id: str, contract: str | None, operation: str | None) -> dict[str, Any]:
        if not contract or not operation:
            raise PlatformValidationError("A discoverable resource has no discovery contract.")
        context = CapabilityInvocationContext.from_actor(
            tenant_id=self.tenant_id,
            actor_type="operator",
            actor_id="integration-management",
            entry_point="integration-management.discover",
            integration_id=integration_id,
            connection_id=connection_id,
        )
        return IntegrationServiceContractRegistry(
            self.contributions,
            sdk_factory=self._sdk_factory(),
        ).invoke(
            contract,
            operation,
            {},
            actor=self._host_actor(),
            invocation_context=context,
            provider_integration_id=integration_id,
        )

    def _sdk(
        self,
        integration_id: str,
        connection_id: str,
        allow_disabled_integration_for_preview: bool = False,
    ) -> Any:
        return self._sdk_factory().create(
            caller=self._host_actor(), integration_id=integration_id, connection_id=connection_id,
            invocation_context=CapabilityInvocationContext.from_actor(
                tenant_id=self.tenant_id, actor_type="operator", actor_id="integration-management",
                entry_point=(
                    "integration-management.mapping-preview"
                    if allow_disabled_integration_for_preview
                    else "integration-management.connection"
                ), integration_id=integration_id,
                connection_id=connection_id,
            ),
            allow_disabled_integration_for_preview=allow_disabled_integration_for_preview,
        )

    def _sdk_factory(self) -> IntegrationSDKFactory:
        from app.services.integration_capabilities import IntegrationAuthorizationService
        return IntegrationSDKFactory(
            self.store,
            tenant_id=self.tenant_id,
            authorization=IntegrationAuthorizationService(self.integrations),
            connections=self.connections,
        )

    def _http_adapter(self) -> Any:
        from app.services.integration_http import IntegrationHTTPAdapter
        return IntegrationHTTPAdapter(self.connections)

    @staticmethod
    def _host_actor() -> CapabilityActor:
        return CapabilityActor(
            client_id="internal-integration-management",
            name="Integration Management",
            client_type="internal",
            permission_mode="internal",
            granted_capabilities=frozenset(PUBLIC_CAPABILITIES) | {"capabilities:internal"},
            internal=True,
        )

    def _health(self, integration_id: str) -> dict[str, Any]:
        if self.lifecycle is not None and self._manual_kind(self.integrations.get(integration_id)) is None:
            try:
                return self.lifecycle.health(integration_id)
            except PlatformNotFoundError:
                pass
        return {
            "integration_id": integration_id,
            "status": self.integrations.get(integration_id)["status"],
            "connections": [item["health"] for item in self.connections.list(integration_id)],
        }

    @staticmethod
    def _safe_connection(value: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: value[key] for key in (
                "id", "integration_id", "name", "project_id", "endpoint_url", "origin_kind",
                "internal_endpoint_ref", "credential_refs", "enabled", "health",
                "last_health_check_at", "last_error", "created_at", "updated_at",
            ) if key in value
        }
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        mcp = metadata.get("mcp") if isinstance(metadata, dict) else None
        if isinstance(mcp, dict) and isinstance(mcp.get("tools"), list):
            configuration = value.get("configuration") if isinstance(value.get("configuration"), dict) else {}
            configured_mcp = configuration.get("mcp") if isinstance(configuration, dict) else {}
            approved_tools = {
                str(item) for item in (configured_mcp.get("approved_tools", []) if isinstance(configured_mcp, dict) else [])
            }
            result["discovered_tools"] = [
                {
                    "name": item.get("name", ""),
                    "description": str(item.get("description", ""))[:500],
                    "input_schema": item.get("input_schema", {}) if isinstance(item.get("input_schema"), dict) else {},
                    "read_only": item.get("read_only") if isinstance(item.get("read_only"), bool) else None,
                    "destructive": item.get("destructive") if isinstance(item.get("destructive"), bool) else None,
                    "safety_known": item.get("safety_known") is True,
                    "approved": str(item.get("name")) in approved_tools,
                }
                for item in mcp["tools"] if isinstance(item, dict) and item.get("name")
            ]
        return result

    @staticmethod
    def _manifest(integration: dict[str, Any]) -> ExtensionManifest:
        manifest = integration.get("manifest")
        if not isinstance(manifest, dict):
            raise PlatformValidationError("Integration record carries no manifest.")
        return validate_extension_manifest(
            manifest,
            lynewright_version="0.1.0",
            sdk_version="1.0.0",
        )

    @staticmethod
    def _descriptor(
        manifest: ExtensionManifest,
        *,
        product_role: str = "external_integration",
        has_settings: bool = False,
    ) -> dict[str, Any]:
        value = manifest.management.model_dump(mode="json")
        sections = list(value.get("sections") or [])
        if "overview" not in sections:
            sections.insert(0, "overview")
        if product_role == PRODUCT_ROLE_FIRST_PARTY_FEATURE:
            # A local UI feature has no connections, credentials, provider
            # health, or diagnostics. Its explicit overview is the complete
            # management surface; do not add generic integration sections.
            value["sections"] = sections
            return value
        if "connections" not in sections:
            sections.append("connections")
        if value.get("resources") and "external_resources" not in sections:
            sections.append("external_resources")
        if value.get("resources") and "bindings" not in sections:
            sections.append("bindings")
        if value.get("supports_health") and "health" not in sections:
            sections.append("health")
        if value.get("supports_diagnostics") and "diagnostics" not in sections:
            sections.append("diagnostics")
        if value.get("supports_oauth") and "oauth" not in sections:
            sections.append("oauth")
        if value.get("supports_webhooks") and "webhooks" not in sections:
            sections.append("webhooks")
        if value.get("supports_settings") and "settings" not in sections and (manifest.settings_schema or has_settings):
            sections.append("settings")
        value["sections"] = sections
        return value

    def _attention_for_bindings(
        self,
        binding_ids: set[str],
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if not binding_ids:
            return []
        return [
            item for item in self.reconciliation.list_open(limit=limit)
            if item.get("binding_id") in binding_ids
        ]

    def _resource_descriptors(
        self,
        descriptor: dict[str, Any],
        integration: dict[str, Any],
        connection: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        values = [dict(item) for item in descriptor.get("resources", [])]
        if self._manual_kind(integration) == "mcp":
            values.append({
                "type": "mcp_resource", "label": "MCP resource", "plural_label": "MCP resources",
                "parent_type": None, "discoverable": True, "bindable": True,
                "operations": ["discover", "read", "bind", "refresh"], "display_fields": ["name", "url"],
                "discovery_contract": None, "discovery_operation": None,
            })
            for mapping in self._mcp_resource_mappings(integration, connection):
                if not mapping["enabled"]:
                    continue
                values.append({
                    "type": mapping["resource_type"],
                    "label": mapping["label"],
                    "plural_label": mapping["plural_label"],
                    "parent_type": None,
                    "discoverable": True,
                    "bindable": True,
                    "operations": [
                        "discover", "bind", "refresh", "read",
                        *(["create"] if mapping.get("create_tool_name") else []),
                        *(["update"] if mapping.get("update_tool_name") else []),
                        *(["delete"] if mapping.get("delete_tool_name") else []),
                    ],
                    "display_fields": mapping["display_fields"],
                    "discovery_contract": None,
                    "discovery_operation": mapping["list_tool_name"],
                    "mcp_mapping": mapping,
                })
        if self._manual_kind(integration) == "openapi":
            for mapping in self._resource_mappings(integration):
                if not mapping["enabled"]:
                    continue
                values.append({
                    "type": mapping["resource_type"], "label": mapping["label"], "plural_label": mapping["plural_label"],
                    "parent_type": None, "discoverable": True, "bindable": True,
                    "operations": [
                        "discover", "bind", "refresh",
                        *(["read"] if mapping.get("get_operation_id") else []),
                        *(["create"] if mapping.get("create_operation_id") else []),
                        *(["update"] if mapping.get("update_operation_id") else []),
                        *(["delete"] if mapping.get("delete_operation_id") else []),
                    ], "display_fields": mapping["display_fields"],
                    "discovery_contract": None, "discovery_operation": mapping["list_operation_id"],
                })
        return values

    @staticmethod
    def _mcp_arguments_with_external_id(arguments: dict[str, Any], target: str, external_id: str) -> dict[str, Any]:
        try:
            value = json.loads(json.dumps(arguments))
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("MCP resource action arguments must be JSON serializable.") from exc
        if _MCP_MAPPING_TARGET.fullmatch(target) is None:
            raise PlatformValidationError("MCP resource mapping input target is invalid.")
        if not isinstance(value, dict):
            raise PlatformValidationError("MCP resource action arguments must be an object.")
        segments = target.split(".")
        current = value
        for segment in segments[:-1]:
            nested = current.setdefault(segment, {})
            if not isinstance(nested, dict):
                raise PlatformValidationError("MCP resource mapping input target conflicts with supplied arguments.")
            current = nested
        current[segments[-1]] = external_id
        return value

    @staticmethod
    def _validated_resource_arguments(descriptor: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize declared resource-form fields at the host boundary."""
        fields = descriptor.get("fields") or []
        if not fields:
            return values
        by_key = {str(field.get("key")): field for field in fields if isinstance(field, dict) and field.get("key")}
        unknown = set(values) - set(by_key)
        if unknown:
            raise PlatformValidationError("Resource arguments contain undeclared fields.")
        normalized = dict(values)
        for key, field in by_key.items():
            value = normalized.get(key)
            if field.get("required") and (value is None or value == "" or value == []):
                raise PlatformValidationError(f"Resource field {key} is required.")
            if value is None:
                continue
            field_type = field.get("type", "text")
            if field_type == "text":
                if not isinstance(value, str) or len(value) > 4_096:
                    raise PlatformValidationError(f"Resource field {key} must be bounded text.")
                normalized[key] = value.strip()
                if field.get("normalization") == "hostname":
                    normalized[key] = normalized[key].lower().rstrip(".")
            elif field_type == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise PlatformValidationError(f"Resource field {key} must be numeric.")
                if field.get("min") is not None and value < field["min"]:
                    raise PlatformValidationError(f"Resource field {key} is below its minimum.")
                if field.get("max") is not None and value > field["max"]:
                    raise PlatformValidationError(f"Resource field {key} exceeds its maximum.")
            elif field_type == "boolean":
                if not isinstance(value, bool):
                    raise PlatformValidationError(f"Resource field {key} must be boolean.")
            elif field_type == "enum":
                if not isinstance(value, str) or value not in field.get("options", []):
                    raise PlatformValidationError(f"Resource field {key} is not an allowed option.")
            elif field_type == "list":
                if not isinstance(value, list) or len(value) > int(field.get("max_items") or 50) or any(not isinstance(item, str) or len(item) > 512 for item in value):
                    raise PlatformValidationError(f"Resource field {key} must be a bounded list.")
            else:
                raise PlatformValidationError(f"Resource field {key} uses an unsupported type.")
        return normalized

    @staticmethod
    def _manual_kind(integration: dict[str, Any]) -> str | None:
        value = (integration.get("configuration") or {}).get("manual")
        kind = value.get("runtime") if isinstance(value, dict) else None
        return str(kind) if kind in _MANUAL_KINDS else None

    @staticmethod
    def _extract_resources(value: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("resources", "items", "websites", "data"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return []

    @staticmethod
    def _mcp_resource(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "external_id": str(value.get("uri") or value.get("name") or "")[:256],
            "name": value.get("name") or value.get("uri") or "MCP resource",
            "url": value.get("uri") or "",
            "metadata": {"description": value.get("description", ""), "mime_type": value.get("mime_type", "")},
            "available_actions": ["discover", "read", "bind", "refresh"],
        }


__all__ = ["IntegrationManagementService"]
