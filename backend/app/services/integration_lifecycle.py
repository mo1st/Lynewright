"""Controlled lifecycle runtime for registered generic integrations."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.activation_scope import IntegrationActivationScope
from app.services.dependency_readiness import Dependency, DependencyReadinessGraph
from app.services.integration_contributions import ContributionHandle, ContributionKind, ContributionRegistry
from app.services.integration_definition_identity import IntegrationDefinitionIdentity, IntegrationDefinitionIdentityService
from app.services.integration_execution import IntegrationExecutionRunner
from app.services.integration_jobs import IntegrationJobLifecycleService
from app.services.integration_registry import IntegrationRegistry
from app.services.extension_manifest import manifest_declares_runtime
from app.services.extension_registry import EXTENSION_STATUS_NOT_EXECUTABLE
from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformOperationError, PlatformValidationError


@dataclass(frozen=True, slots=True)
class IntegrationRuntimeDefinition:
    integration_id: str
    definition_version: str | None = None
    manifest_fingerprint: str | None = None
    contract_versions: dict[str, str] = field(default_factory=dict)
    activate: Callable[["IntegrationActivationContext"], None] | None = None
    deactivate: Callable[["IntegrationActivationContext"], None] | None = None
    configure: Callable[["IntegrationActivationContext", dict[str, Any]], None] | None = None
    diagnose: Callable[["IntegrationActivationContext"], dict[str, Any]] | None = None
    health: Callable[["IntegrationActivationContext"], dict[str, Any]] | None = None
    dry_run: Callable[["IntegrationActivationContext"], dict[str, Any]] | None = None
    execution_timeout_seconds: float = 30.0


@dataclass(slots=True)
class IntegrationActivationContext:
    integration_id: str
    configuration: dict[str, Any]
    contributions: ContributionRegistry
    handles: list[ContributionHandle] = field(default_factory=list)
    scope: IntegrationActivationScope = field(init=False)

    def __post_init__(self) -> None:
        self.scope = IntegrationActivationScope(self.integration_id)

    def contribute(
        self,
        *,
        local_id: str,
        kind: ContributionKind,
        handler: Callable[..., Any] | None = None,
        metadata: dict[str, Any] | None = None,
        connection_id: str | None = None,
    ) -> ContributionHandle:
        self.scope.ensure_accepting()
        handle = self.contributions.register(
            integration_id=self.integration_id,
            local_id=local_id,
            kind=kind,
            handler=handler,
            metadata=metadata,
            connection_id=connection_id,
            activation_scope=self.scope,
        )
        self.handles.append(handle)
        self.scope.register_disposer(handle.dispose)
        return handle

    def register_control_plane_service_handlers(
        self,
        *,
        require_verified: bool = True,
    ) -> Any:
        """Bind provider-neutral mutation actions for this tenant.

        Adapter activation code may call this after registering its service
        contribution.  The contract registry is built from the existing
        contribution, evidence, and provider-selection authorities; no new
        provider resolver or scheduler is introduced.
        """
        from app.services.control_plane_host import (
            register_control_plane_service_handlers,
            unregister_control_plane_service_handlers,
        )
        from app.services.integration_capability_evidence import IntegrationCapabilityEvidenceService
        from app.services.integration_provider_selection import IntegrationProviderSelectionService
        from app.services.integration_service_contracts import IntegrationServiceContractRegistry

        integrations = self.contributions.integrations
        services = IntegrationServiceContractRegistry(
            self.contributions,
            evidence=IntegrationCapabilityEvidenceService(integrations),
            selections=IntegrationProviderSelectionService(integrations),
        )
        dispatcher = register_control_plane_service_handlers(
            integrations.tenant_id,
            services,
            require_verified=require_verified,
        )
        disposer = lambda: unregister_control_plane_service_handlers(integrations.tenant_id, services)
        try:
            self.scope.register_disposer(disposer)
        except Exception:
            disposer()
            raise
        return dispatcher

    def register_control_plane_video_handlers(self, provider: Any) -> Any:
        """Bind a provider's video-render interface to this tenant's host."""
        from app.services.control_plane_host import (
            register_control_plane_video_handlers,
            unregister_control_plane_video_handlers,
        )

        dispatcher = register_control_plane_video_handlers(
            self.contributions.integrations.tenant_id,
            provider,
            integration_id=self.integration_id,
        )
        disposer = lambda: unregister_control_plane_video_handlers(
            self.contributions.integrations.tenant_id,
            provider,
            integration_id=self.integration_id,
        )
        try:
            self.scope.register_disposer(disposer)
        except Exception:
            disposer()
            raise
        return dispatcher

    def register_control_plane_data_adapter(
        self,
        capability_id: str,
        adapter: Callable[[int, str | None], dict[str, Any]],
    ) -> Callable[[], bool]:
        """Register a bounded read adapter and dispose it with this activation."""
        self.scope.ensure_accepting()
        from app.services.control_plane_host import register_control_plane_data_adapter

        disposer = register_control_plane_data_adapter(
            self.contributions.integrations.tenant_id,
            capability_id,
            adapter,
            # Taken from the activation context, not from the integration. An
            # integration that could name its own implementation id could
            # register an adapter claiming to be a different provider, and its
            # output would then be attributed to one.
            implementation_id=self.integration_id,
            data_source=self.integration_id,
        )
        try:
            self.scope.register_disposer(disposer)
        except Exception:
            disposer()
            raise
        return disposer


class IntegrationLifecycleRuntime:
    """Activates trusted adapter definitions without letting failures affect Core."""

    def __init__(
        self,
        integrations: IntegrationRegistry,
        contributions: ContributionRegistry,
        *,
        jobs: IntegrationJobLifecycleService | None = None,
        execution: IntegrationExecutionRunner | None = None,
        identities: IntegrationDefinitionIdentityService | None = None,
        dependency_graph: DependencyReadinessGraph | None = None,
    ) -> None:
        self.integrations = integrations
        self.contributions = contributions
        self.jobs = jobs or IntegrationJobLifecycleService(integrations.store, tenant_id=integrations.tenant_id)
        self.execution = execution or IntegrationExecutionRunner()
        self.identities = identities or IntegrationDefinitionIdentityService(integrations)
        self.dependency_graph = dependency_graph
        self._definitions: dict[str, list[IntegrationRuntimeDefinition]] = {}
        self._definition_identities: dict[int, IntegrationDefinitionIdentity] = {}
        self._contexts: dict[str, IntegrationActivationContext] = {}
        self._unsubscribe_disabled = integrations.on_disabled(self._on_disabled)

    def register_dependencies(
        self,
        integration_id: str,
        requires: list[Dependency] | tuple[Dependency, ...] = (),
    ) -> dict[str, object]:
        """Declare activation dependencies for an integration.

        Dependencies are optional for backwards compatibility.  When a graph
        is supplied, a node must be READY before activation; otherwise the
        integration remains enabled but degraded until its dependencies return.
        """
        if self.dependency_graph is None:
            raise PlatformOperationError("This lifecycle runtime has no dependency readiness graph.")
        self.integrations.get(integration_id)
        node = self.dependency_graph.register(integration_id, requires)
        return self.dependency_graph.diagnostics(node.node_id)

    def set_dependency_available(self, dependency: Dependency, *, available: bool = True) -> dict[str, object]:
        """Fence active integrations on dependency loss and reactivate on recovery."""
        if self.dependency_graph is None:
            raise PlatformOperationError("This lifecycle runtime has no dependency readiness graph.")
        self.dependency_graph.set_available(dependency, available=available)
        suspended: list[str] = []
        reactivated: list[str] = []
        failed: list[str] = []
        waiting: list[str] = []
        for node in self.dependency_graph.nodes():
            integration_id = node.node_id
            if node.state != "READY":
                waiting.append(integration_id)
                if self.is_active(integration_id):
                    self.suspend_for_dependency(integration_id, reason=f"dependency_unavailable:{dependency.key}")
                    suspended.append(integration_id)
                continue
            if self.integrations.get(integration_id)["enabled"] and not self.is_active(integration_id):
                try:
                    self.activate(integration_id)
                    reactivated.append(integration_id)
                except Exception:
                    failed.append(integration_id)
        return {
            "dependency": dependency.key,
            "available": available,
            "suspended": suspended,
            "reactivated": reactivated,
            "waiting": waiting,
            "failed": failed,
        }

    def suspend_for_dependency(self, integration_id: str, *, reason: str) -> dict[str, Any]:
        """Deactivate runtime effects while preserving enabled intent/configuration."""
        self.integrations.get(integration_id)
        if self.is_active(integration_id):
            self.deactivate(integration_id)
        return self.integrations.set_runtime_status(integration_id, status="degraded", error=reason[:1_000])

    def register_definition(self, definition: IntegrationRuntimeDefinition) -> None:
        self.integrations.get(definition.integration_id)
        identity = self.identities.identity_for(
            definition.integration_id,
            definition_version=definition.definition_version,
            manifest_fingerprint=definition.manifest_fingerprint,
            contract_versions=definition.contract_versions,
        )
        registered = self._definitions.setdefault(definition.integration_id, [])
        if any(self._definition_identities[id(item)] == identity for item in registered):
            raise PlatformConflictError("Integration runtime definition is already registered.")
        registered.append(definition)
        self._definition_identities[id(definition)] = identity
        self.identities.record_identity(identity)

    def unregister_definition(self, integration_id: str) -> int:
        """Remove process-local runtime definitions and their live effects."""
        selected = str(integration_id)
        context = self._contexts.pop(selected, None)
        if context is not None:
            try:
                context.scope.stop_accepting()
            finally:
                context.scope.dispose()
        self.jobs.deactivate_integration(selected)
        self.contributions.unregister_integration(selected)
        definitions = self._definitions.pop(selected, [])
        for definition in definitions:
            self._definition_identities.pop(id(definition), None)
        return len(definitions)

    def configure(self, integration_id: str, configuration: dict[str, Any]) -> dict[str, Any]:
        result = self.integrations.configure(integration_id, configuration)
        definition = self._definition(integration_id)
        if definition.configure is None:
            return result
        context = self._context(integration_id)
        try:
            self.execution.run("configure", definition.configure, context, configuration, timeout_seconds=definition.execution_timeout_seconds)
        except Exception as exc:
            self.integrations.set_runtime_status(integration_id, status="error", error=self._error(exc))
            raise PlatformOperationError("Integration configuration failed.") from exc
        return self.integrations.set_runtime_status(integration_id, status="configured")

    def apply_operator_settings(self, integration_id: str, configuration: dict[str, Any]) -> None:
        """Forward validated setting changes through an active provider hook."""
        self.integrations.get(integration_id)
        context = self._contexts.get(integration_id)
        if context is None:
            return
        definition = self._definition(integration_id)
        if definition.configure is None:
            context.configuration = dict(configuration)
            return
        previous = dict(context.configuration)
        try:
            self.execution.run(
                "configure", definition.configure, context, dict(configuration),
                timeout_seconds=definition.execution_timeout_seconds,
            )
        except Exception as exc:
            context.configuration = previous
            self.integrations.set_runtime_status(
                integration_id, status="error", error=self._error(exc),
            )
            raise PlatformOperationError("Integration settings could not be applied.") from exc
        context.configuration = dict(configuration)

    def enable(self, integration_id: str) -> dict[str, Any]:
        # Resolve the definition before persisting enabled intent. A missing
        # live definition must not turn a failed request into an enabled row.
        self._definition(integration_id)
        self.integrations.enable(integration_id)
        return self.activate(integration_id)

    def activate(self, integration_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        if not integration["enabled"]:
            raise PlatformOperationError("Integration must be enabled before activation.")
        if integration_id in self._contexts:
            raise PlatformOperationError("Integration is already active.")
        if self.dependency_graph is not None:
            try:
                node = self.dependency_graph.node(integration_id)
            except PlatformValidationError:
                node = None
            if node is not None and node.state != "READY":
                return self.integrations.set_runtime_status(
                    integration_id,
                    status="degraded",
                    error="Integration is waiting on declared dependencies: "
                    + ", ".join(str(item) for item in self.dependency_graph.diagnostics(integration_id)["waiting_on"]),
                )
        definition = self._definition(integration_id)
        context = self._context(integration_id)
        self.integrations.set_runtime_status(integration_id, status="activating")
        try:
            context.scope.set_state("ACTIVATING")
            self.identities.lock_or_verify(self.definition_identity(definition))
            if definition.activate is not None:
                self.execution.run("activate", definition.activate, context, timeout_seconds=definition.execution_timeout_seconds)
            context.scope.set_state("READY")
            self._contexts[integration_id] = context
        except Exception as exc:
            context.scope.set_state("FAILED")
            context.scope.dispose()
            self.contributions.unregister_integration(integration_id)
            self.integrations.set_runtime_status(integration_id, status="error", error=self._error(exc))
            raise PlatformOperationError("Integration activation failed.") from exc
        return self.integrations.set_runtime_status(integration_id, status="active", activated=True)

    def deactivate(self, integration_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        self.jobs.deactivate_integration(integration_id)
        context = self._contexts.pop(integration_id, None)
        try:
            definition = self._definition(integration_id)
        except PlatformNotFoundError:
            definition = None
        error = ""
        try:
            if context is not None:
                context.scope.stop_accepting()
            if context is not None and definition is not None and definition.deactivate is not None:
                self.execution.run("deactivate", definition.deactivate, context, timeout_seconds=definition.execution_timeout_seconds)
        except Exception as exc:
            error = self._error(exc)
        finally:
            if context is not None:
                dispose_errors = context.scope.dispose()
                if dispose_errors and not error:
                    error = "Activation scope disposal failed: " + ", ".join(dispose_errors)
            self.contributions.unregister_integration(integration_id)
        if not integration["enabled"]:
            return self.integrations.get(integration_id)
        return self.integrations.set_runtime_status(integration_id, status="configured" if not error else "error", error=error)

    def disable(self, integration_id: str) -> dict[str, Any]:
        self.deactivate(integration_id)
        return self.integrations.disable(integration_id)

    def reload(self, integration_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        if not integration["enabled"]:
            raise PlatformOperationError("Disabled integrations cannot be reloaded.")
        self.deactivate(integration_id)
        return self.activate(integration_id)

    def recover_enabled(self) -> dict[str, list[str]]:
        """Restore enabled integrations after adapter definitions are bootstrapped.

        The registry persists installation intent and configuration, while Python
        callback definitions are deliberately process-local.  A process must
        register the definitions it has available and then call this method once
        during startup.  One unavailable or failing adapter is isolated from the
        rest of Lynewright.
        """
        result: dict[str, list[str]] = {"activated": [], "already_active": [], "unavailable": [], "failed": [], "not_executable": []}
        for integration in self.integrations.list():
            integration_id = str(integration["id"])
            if not integration["enabled"]:
                continue
            if integration_id in self._contexts:
                result["already_active"].append(integration_id)
                continue
            if not self._definitions.get(integration_id):
                # Two very different situations produced the same red status.
                #
                # A definition is missing for an integration whose manifest
                # declares something executable: Lynewright expected this to run
                # and it did not. That is an error.
                #
                # A definition is missing for a record that declares nothing
                # executable: there was never anything to register. Calling
                # that "runtime definition unavailable after restart" invited
                # someone to go build one, and rewrote the same misleading
                # error on every restart.
                #
                # The check is registration rather than the manifest alone,
                # because integrations registered from durable configuration
                # rather than a manifest -- persisted sources -- declare no
                # contributions yet activate perfectly well. Their own
                # bootstrap reports its failures separately.
                manifest = integration.get("manifest") or {}
                declares_runtime = manifest_declares_runtime(manifest)
                if declares_runtime:
                    self.integrations.set_runtime_status(
                        integration_id,
                        status="error",
                        error="Integration runtime definition is unavailable after restart.",
                    )
                    result["unavailable"].append(integration_id)
                    continue
                # Not an activation failure. Clear a stale error from before
                # this distinction existed, and only write when it would
                # actually change, so repeated restarts stop rewriting the row.
                if integration.get("status") != EXTENSION_STATUS_NOT_EXECUTABLE or integration.get("last_error"):
                    self.integrations.set_runtime_status(
                        integration_id, status=EXTENSION_STATUS_NOT_EXECUTABLE, error="",
                    )
                result["not_executable"].append(integration_id)
                continue
            active_identity = self.identities.active(integration_id)
            if active_identity is not None and self.definition_for_identity(active_identity) is None:
                self.integrations.set_runtime_status(
                    integration_id,
                    status="error",
                    error="The exact activated integration definition is unavailable after restart.",
                )
                result["unavailable"].append(integration_id)
                continue
            # A real restart starts with an empty registry.  Clearing stale
            # in-process handles here also makes a repeated bootstrap safe.
            self.contributions.unregister_integration(integration_id)
            try:
                self.activate(integration_id)
                result["activated"].append(integration_id)
            except Exception:
                # activate() persists a bounded error; startup must continue.
                result["failed"].append(integration_id)
        return result

    def diagnose(self, integration_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        definition = self._definition(integration_id)
        context = self._context(integration_id)
        try:
            details = self.execution.run("diagnose", definition.diagnose, context, timeout_seconds=definition.execution_timeout_seconds) if definition.diagnose is not None else {}
            return {"integration_id": integration_id, "status": integration["status"], "result": details}
        except Exception as exc:
            self.integrations.record_health_check(integration_id, error=self._error(exc))
            return {"integration_id": integration_id, "status": "error", "error": type(exc).__name__}

    def health(self, integration_id: str) -> dict[str, Any]:
        integration = self.integrations.get(integration_id)
        definition = self._definition(integration_id)
        context = self._context(integration_id)
        try:
            runtime_result = self.execution.run("health", definition.health, context, timeout_seconds=definition.execution_timeout_seconds) if definition.health is not None else {"state": "UNKNOWN"}
            contribution_results = self.contributions.run_health_checks()
            owned = {key: value for key, value in contribution_results.items() if key.startswith(f"{integration_id}:")}
            self.integrations.record_health_check(integration_id)
            return {"integration_id": integration_id, "status": integration["status"], "runtime": runtime_result, "contributions": owned}
        except Exception as exc:
            self.integrations.record_health_check(integration_id, error=self._error(exc))
            return {"integration_id": integration_id, "status": "error", "error": type(exc).__name__}

    def _on_disabled(self, integration_id: str) -> None:
        if integration_id in self._contexts:
            self.deactivate(integration_id)

    def _definition(self, integration_id: str) -> IntegrationRuntimeDefinition:
        definitions = self._definitions.get(integration_id) or []
        active = self.identities.active(integration_id)
        if active is not None:
            for definition in definitions:
                if self.definition_identity(definition) == active:
                    return definition
            raise PlatformNotFoundError("The exact activated integration definition is unavailable.")
        if len(definitions) == 1:
            return definitions[0]
        if not definitions:
            raise PlatformNotFoundError("Integration runtime definition is not registered.")
        raise PlatformConflictError("More than one runtime definition is available before activation.")

    def definition_identity(self, definition: IntegrationRuntimeDefinition) -> IntegrationDefinitionIdentity:
        return self._definition_identities[id(definition)]

    def definition_for_identity(self, identity: IntegrationDefinitionIdentity) -> IntegrationRuntimeDefinition | None:
        for definition in self._definitions.get(identity.integration_id, []):
            if self.definition_identity(definition) == identity:
                return definition
        return None

    def definitions_for(self, integration_id: str) -> tuple[IntegrationRuntimeDefinition, ...]:
        """Return definitions registered for an integration in this process."""
        return tuple(self._definitions.get(str(integration_id), ()))

    def active_definition(self, integration_id: str) -> IntegrationRuntimeDefinition:
        return self._definition(integration_id)

    def is_active(self, integration_id: str) -> bool:
        return integration_id in self._contexts

    def _context(self, integration_id: str) -> IntegrationActivationContext:
        integration = self.integrations.get(integration_id)
        return IntegrationActivationContext(
            integration_id=integration_id,
            configuration=dict(integration["configuration"]),
            contributions=self.contributions,
        )

    @staticmethod
    def _error(exc: Exception) -> str:
        return f"{type(exc).__name__}: {str(exc)[:500]}"
