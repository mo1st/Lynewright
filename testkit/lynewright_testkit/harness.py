"""A small, direct-service harness for real integration line invocations."""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from app.services.control_plane_registry import CapabilityDescriptor, ControlPlaneRegistry
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_file import IntegrationFileService
from app.services.integration_lifecycle import IntegrationLifecycleRuntime
from app.services.integration_management import IntegrationManagementService
from app.services.integration_operations import IntegrationOperationCatalog
from app.services.integration_registry import IntegrationRegistry
from app.services.line_engine import ProductionLineEngine
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformStore, PlatformValidationError
from app.services.production_integrations import (
    compile_installed_definitions,
    discover_shipped_manifests,
    ensure_discovered_integrations_installed,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID
from app.services.tenancy import create_tenant_project


TEST_CONTRACT = "lynewright.testkit.invoke"
TEST_OPERATION = "invoke"
TEST_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": True,
}
_TERMINAL = {"done", "failed", "cancelled", "canceled", "rejected"}


class LynewrightHarness:
    """Temporary integration registries, runtime, catalogue, and line engine.

    The harness does not import ``app.main`` or start dashboard/background
    processes. It invokes the same integration catalogue and line runner used
    by production, against a private SQLite file and managed-media directory.
    """

    def __init__(self, root: Path, *, monkeypatch: Any) -> None:
        self.root = Path(root)
        self.monkeypatch = monkeypatch
        self._active_integrations: list[str] = []
        self._register_environment()

        self.store = PlatformStore()
        self.integrations = IntegrationRegistry(self.store, tenant_id=DEFAULT_TENANT_ID)
        self.connections = ConnectionProfileService(
            self.store, registry=self.integrations, endpoint_validator=lambda _url: {},
        )
        self.contributions = ContributionRegistry(self.integrations, connections=self.connections)
        self.lifecycle = IntegrationLifecycleRuntime(self.integrations, self.contributions)
        self.control_plane = ControlPlaneRegistry()
        self.control_plane.capabilities.register(CapabilityDescriptor(
            id="lynewright.testkit.operation",
            version="1.0.0",
            contract_id=TEST_CONTRACT,
            operation=TEST_OPERATION,
            name="Testkit operation",
            description="Provider-neutral target for a local MCP tool mapping.",
            required_actor_capability="network:use",
            risk_class="LOW_RISK",
            project_scope="required",
            resource_scope="none",
            connection_scope="connection",
            output_schema=TEST_OUTPUT_SCHEMA,
        ))
        self.management = IntegrationManagementService(
            self.integrations,
            self.connections,
            self.contributions,
            lifecycle=self.lifecycle,
            control_plane=self.control_plane,
        )
        self.catalog = IntegrationOperationCatalog(self.management)
        self.engine = ProductionLineEngine(self.store, tenant_id=DEFAULT_TENANT_ID)
        self.actor = CapabilityActor(
            client_id="lynewright-testkit",
            name="Lynewright testkit",
            client_type="automation",
            permission_mode="full",
            granted_capabilities=frozenset(),
        )
        self.project_id = "testkit-" + secrets.token_hex(8)
        create_tenant_project(
            tenant_id=DEFAULT_TENANT_ID,
            project_id=self.project_id,
            name="Lynewright integration test",
        )

        from app.services import integration_operations, line_runner

        # A deterministic in-process fallback makes tests independent from a
        # running worker while still using line_runner's normal invocation path.
        monkeypatch.setattr(line_runner, "_worker_is_configured", lambda: False)
        monkeypatch.setattr(
            integration_operations,
            "build_tenant_operation_catalog",
            lambda _tenant_id: self.catalog,
        )

    def _register_environment(self) -> None:
        self.monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(self.root / "lynewright-testkit.sqlite3"))
        self.monkeypatch.setenv("LYNEWRIGHT_MANAGED_MEDIA_DIR", str(self.root / "managed-media"))
        self.monkeypatch.setenv("LYNEWRIGHT_DEMO", "0")
        self.monkeypatch.setenv("LYNEWRIGHT_PLUGINS", "")
        self.monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", "")
        self.monkeypatch.setenv("LYNEWRIGHT_RUNTIME_WORKER_ENABLED", "0")
        self.monkeypatch.setenv("LYNEWRIGHT_RUNTIME_WORKER_MODE", "disabled")
        self.monkeypatch.setenv("LYNEWRIGHT_DISABLE_BACKGROUND_SCHEDULERS", "1")
        self.monkeypatch.setenv("LYNEWRIGHT_OPERATOR_AUTOMATION_ENABLED", "0")
        self.monkeypatch.setenv("LYNEWRIGHT_TELEGRAM_ENABLED", "0")

    def set_internal_endpoint(self, reference: str, url: str) -> str:
        """Bind one test server to a safe, named internal endpoint reference."""
        normalized = "".join(char if char.isalnum() else "_" for char in reference).strip("_").upper()
        if not normalized:
            raise ValueError("An internal endpoint reference is required.")
        self.monkeypatch.setenv(f"LYNEWRIGHT_INTERNAL_ENDPOINT__{normalized}", url.rstrip("/"))
        return reference.lower()

    def import_file(self, source: str | Path | dict[str, Any], *, endpoint_url: str = "") -> str:
        """Validate and import an OpenAPI or MCP integration file."""
        from app.services.integration_install_policy import InstallConfirmationRequired

        if isinstance(source, dict):
            declaration = dict(source)
            artifact_bytes = json.dumps(
                source, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")
        else:
            artifact_bytes = Path(source).read_bytes()
            declaration = IntegrationFileService.parse(artifact_bytes)
        if endpoint_url:
            declaration["endpoint_url"] = endpoint_url.rstrip("/")
        installer = IntegrationFileService(self.management)
        try:
            imported = installer.import_value(declaration, artifact_bytes=artifact_bytes)
        except InstallConfirmationRequired as confirmation:
            # K5.4 requires explicit confirmation for outside imports. The
            # testkit is the trusted test operator, so it confirms the exact
            # summary returned by the first validation pass.
            imported = installer.import_value(
                declaration,
                artifact_bytes=artifact_bytes,
                confirm={
                    "integration_id": str(confirmation.summary.get("integration_id") or ""),
                    "summary_hash": confirmation.summary_hash,
                },
            )
        detail = imported["integration"]
        record = detail.get("integration", detail)
        return str(record["id"])

    def connect(
        self,
        integration_id: str,
        *,
        name: str = "Local test endpoint",
        connection_id: str = "testkit-connection",
        internal_endpoint: str = "",
        endpoint_url: str = "",
        configuration: dict[str, Any] | None = None,
    ) -> str:
        ref = self.set_internal_endpoint(internal_endpoint, endpoint_url) if internal_endpoint else ""
        created = self.connections.create(
            integration_id,
            name=name,
            connection_id=connection_id,
            project_id=self.project_id,
            origin_kind="internal_endpoint" if ref else "public",
            internal_endpoint_ref=ref,
            endpoint_url=endpoint_url if not ref else "",
            configuration=configuration or {},
        )
        return str(created["id"])

    def authorize(self, integration_id: str, capabilities: list[str] | tuple[str, ...]) -> dict[str, Any]:
        return self.integrations.set_authorization(
            integration_id,
            permission_mode="custom",
            granted_capabilities=list(capabilities),
        )

    def enable(self, integration_id: str, *, activate_native: bool = True) -> dict[str, Any]:
        enabled = self.integrations.enable(integration_id)
        if activate_native:
            definitions = compile_installed_definitions(self.integrations)
            for definition in definitions["definitions"]:
                if definition.integration_id == integration_id:
                    try:
                        self.lifecycle.register_definition(definition)
                    except Exception as exc:
                        if "already registered" not in str(exc).lower():
                            raise
                    break
            if self.lifecycle.definitions_for(integration_id):
                self.lifecycle.activate(integration_id)
                self._active_integrations.append(integration_id)
        return enabled

    def install_native_package(
        self,
        distribution: str,
        *,
        granted_capabilities: list[str] | tuple[str, ...] = (),
    ) -> str:
        """Discover, install, compile, authorize, and activate one allowlisted package."""
        current = [item.strip() for item in os.getenv("LYNEWRIGHT_INTEGRATION_PACKAGES", "").split(",") if item.strip()]
        if distribution.casefold() not in {item.casefold() for item in current}:
            current.append(distribution)
        self.monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", ",".join(current))
        selected = [
            item for item in discover_shipped_manifests()
            if item.source == "package" and item.distribution.casefold() == distribution.casefold()
        ]
        if len(selected) != 1:
            raise PlatformValidationError(
                f"Expected one compatible manifest for {distribution!r}; found {len(selected)}."
            )
        integration_id = str(selected[0].manifest["id"])
        ensure_discovered_integrations_installed(self.integrations)
        from app.services.integration_install_policy import InstallConfirmationRequired

        try:
            self.management.confirm_package_install(integration_id, confirm=None)
        except InstallConfirmationRequired as confirmation:
            self.management.confirm_package_install(integration_id, confirm={
                "integration_id": integration_id,
                "summary_hash": confirmation.summary_hash,
            })
        compiled = compile_installed_definitions(self.integrations)
        for definition in compiled["definitions"]:
            try:
                self.lifecycle.register_definition(definition)
            except Exception as exc:
                if "already registered" not in str(exc).lower():
                    raise
        if granted_capabilities:
            self.authorize(integration_id, granted_capabilities)
        self.enable(integration_id)
        return integration_id

    def openapi_approve(self, integration_id: str, connection_id: str, operation_ids: list[str]) -> dict[str, Any]:
        return self.management.approve_openapi_operations(integration_id, connection_id, operation_ids)

    def discover_mcp(self, integration_id: str, connection_id: str) -> dict[str, Any]:
        return self.management.discover(integration_id, connection_id)

    def mcp_approve_and_map(
        self,
        integration_id: str,
        connection_id: str,
        *,
        tool_name: str,
        mapping_id: str = "testkit-mapping",
        argument_mapping: dict[str, str] | None = None,
        output_mapping: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.management.approve_mcp_tools(integration_id, connection_id, [tool_name])
        return self.management.create_mcp_contract_mapping(integration_id, connection_id, {
            "mapping_id": mapping_id,
            "tool_name": tool_name,
            "target_contract": TEST_CONTRACT,
            "target_operation": TEST_OPERATION,
            "argument_mapping": argument_mapping or {"query": "query"},
            "output_mapping": output_mapping or {"value": "value"},
            "enabled": True,
        })

    def update_settings(self, integration_id: str, values: dict[str, Any]) -> dict[str, Any]:
        return self.management.update_settings(integration_id, values)

    def operations(self, *, integration_id: str = "") -> list[dict[str, Any]]:
        result = self.catalog.operations_for_lines(self.actor, project_id=self.project_id)
        return [item for item in result if not integration_id or item["integration_id"] == integration_id]

    def run_line(
        self,
        operation: dict[str, Any],
        *,
        arguments: dict[str, Any] | None = None,
        stage_kind: str = "operation",
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Save and run a one-stage line through the production catalogue path."""
        stage = {
            "id": "invoke",
            "kind": stage_kind,
            "operation_id": operation["operation_id"],
            "contract": operation["contract"],
            "operation": operation["operation"],
            "args_template": dict(arguments or {}),
        }
        line_id = "testkit-line-" + secrets.token_hex(8)
        self.engine.db.put_line(
            line_id,
            name="Lynewright testkit line",
            project_id=self.project_id,
            definition={"stages": [stage]},
            enabled=True,
            actor_id="lynewright-testkit",
            operation_directory=self.catalog,
            operation_actor=self.actor,
            create_only=True,
        )
        run = self.engine.start(
            line_id,
            variables={},
            project_id=self.project_id,
            actor={"user_id": "lynewright-testkit"},
        )
        deadline = time.monotonic() + max(0.1, timeout)
        while run["status"] not in _TERMINAL and time.monotonic() < deadline:
            time.sleep(0.025)
            run = self.engine.get_run(run["id"])
        if run["status"] not in _TERMINAL:
            raise TimeoutError(f"Production line run {run['id']} did not finish within {timeout:.1f}s.")
        return run

    def run_stages(
        self,
        stages: list[dict[str, Any]],
        variables: dict[str, Any] | None = None,
        *,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Save and run caller-authored stages through the normal line validator."""
        if not isinstance(stages, list) or not stages:
            raise PlatformValidationError("At least one production-line stage is required.")
        line_id = "testkit-line-" + secrets.token_hex(8)
        self.engine.db.put_line(
            line_id,
            name="Lynewright testkit line",
            project_id=self.project_id,
            definition={"stages": stages},
            enabled=True,
            actor_id="lynewright-testkit",
            operation_directory=self.catalog,
            operation_actor=self.actor,
            create_only=True,
        )
        run = self.engine.start(
            line_id,
            variables=dict(variables or {}),
            project_id=self.project_id,
            actor={"user_id": "lynewright-testkit"},
        )
        return self.wait_for_run(run["id"], timeout=timeout)

    def approve_review(self, run: dict[str, Any] | str, *, value: dict[str, Any] | None = None) -> dict[str, Any]:
        """Approve a waiting review gate and return the continued run."""
        run_id = str(run.get("id") if isinstance(run, dict) else run)
        approval = {"approved": True, **(value or {})}
        continued = self.engine.continue_run(
            run_id,
            value=approval,
            actor={"user_id": "lynewright-testkit"},
            actor_id="lynewright-testkit",
        )
        return self.wait_for_run(continued["id"])

    def poll_until(
        self,
        run: dict[str, Any] | str,
        statuses: set[str] | list[str] | tuple[str, ...],
        *,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Poll a run until it reaches one of the requested statuses."""
        run_id = str(run.get("id") if isinstance(run, dict) else run)
        accepted = {str(status) for status in statuses}
        if not accepted:
            raise ValueError("At least one target run status is required.")
        deadline = time.monotonic() + max(0.1, timeout)
        latest = self.engine.get_run(run_id)
        while latest["status"] not in accepted and time.monotonic() < deadline:
            time.sleep(0.025)
            latest = self.engine.get_run(run_id)
        if latest["status"] not in accepted:
            raise TimeoutError(f"Production line run {run_id} did not reach {sorted(accepted)} within {timeout:.1f}s.")
        return latest

    def wait_for_run(self, run_id: str, *, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, timeout)
        run = self.engine.get_run(run_id)
        while run["status"] not in _TERMINAL and time.monotonic() < deadline:
            time.sleep(0.025)
            run = self.engine.get_run(run_id)
        if run["status"] not in _TERMINAL:
            raise TimeoutError(f"Production line run {run_id} did not finish within {timeout:.1f}s.")
        return run

    def operation_trace_count(self) -> int:
        with self.store.connection() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM platform_operation_traces WHERE tenant_id=? AND project_id=?",
                (DEFAULT_TENANT_ID, self.project_id),
            ).fetchone()[0])

    def close(self) -> None:
        for integration_id in reversed(self._active_integrations):
            try:
                self.lifecycle.deactivate(integration_id)
            except Exception:
                pass
        self._active_integrations.clear()
