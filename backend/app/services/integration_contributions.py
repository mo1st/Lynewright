"""Ownership-aware in-process contribution registry for integrations."""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from app.services.activation_scope import IntegrationActivationScope
from app.services.integration_connections import ConnectionProfileService
from app.services.capability_context import CapabilityInvocationContext, bind_capability_context
from app.services.integration_execution import IntegrationExecutionRunner
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_capabilities import PUBLIC_CAPABILITIES
from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformOperationError, PlatformValidationError


ContributionKind = Literal[
    "service", "job", "event", "mcp", "settings", "ui", "health", "webhook", "callback", "scheduled"
]
_KINDS = frozenset({"service", "job", "event", "mcp", "settings", "ui", "health", "webhook", "callback", "scheduled"})
_LOCAL_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")


@dataclass(frozen=True, slots=True)
class Contribution:
    id: str
    local_id: str
    kind: ContributionKind
    integration_id: str
    connection_id: str | None
    metadata: dict[str, Any]
    handler: Callable[..., Any] | None
    sequence: int
    activation_scope: IntegrationActivationScope | None = None


class ContributionHandle:
    """A generation-safe disposable returned to the integration runtime."""

    def __init__(self, registry: "ContributionRegistry", contribution: Contribution) -> None:
        self._registry = registry
        self._contribution = contribution

    @property
    def id(self) -> str:
        return self._contribution.id

    def dispose(self) -> bool:
        return self._registry._remove(self._contribution.id, sequence=self._contribution.sequence)


class ContributionRegistry:
    """The only integration-runtime registration surface for v1 contributions."""

    def __init__(
        self,
        integrations: IntegrationRegistry,
        *,
        connections: ConnectionProfileService | None = None,
        execution: IntegrationExecutionRunner | None = None,
        worker_bridge: Any | None = None,
        container_bridge: Any | None = None,
    ) -> None:
        self.integrations = integrations
        self.connections = connections
        self.execution = execution or IntegrationExecutionRunner()
        # Optional process boundary used by API/MCP compositions.  The worker
        # process leaves this unset and executes the same contribution locally.
        # Keeping the seam on the shared registry makes it available to every
        # future integration type without provider-specific dispatch branches.
        self.worker_bridge = worker_bridge
        self.container_bridge = container_bridge
        self._items: dict[str, Contribution] = {}
        self._lock = threading.RLock()
        self._sequence = 0
        self._changed_callbacks: list[Callable[[], None]] = []
        self._unsubscribe_disabled = self.integrations.on_disabled(self.unregister_integration)

    def register(
        self,
        *,
        integration_id: str,
        local_id: str,
        kind: ContributionKind,
        handler: Callable[..., Any] | None = None,
        metadata: dict[str, Any] | None = None,
        connection_id: str | None = None,
        activation_scope: IntegrationActivationScope | None = None,
    ) -> ContributionHandle:
        if kind not in _KINDS:
            raise PlatformValidationError("Unsupported integration contribution kind.")
        if _LOCAL_ID_RE.fullmatch(local_id) is None:
            raise PlatformValidationError("Contribution id must be a lowercase bounded identifier.")
        integration = self.integrations.get(integration_id)
        if not integration["enabled"]:
            raise PlatformOperationError("Integration must be enabled before it can register contributions.")
        if connection_id is not None:
            if self.connections is None:
                raise PlatformOperationError("Connection-scoped contributions require a connection profile service.")
            connection = self.connections.get(integration_id, connection_id)
            if not connection["enabled"]:
                raise PlatformOperationError("Connection must be enabled before it can register contributions.")
        if handler is None and kind not in {"settings", "ui"}:
            raise PlatformValidationError("This contribution kind requires a handler.")
        if kind == "mcp":
            self._validate_mcp_metadata(metadata or {})
        if kind == "job":
            self._validate_job_metadata(metadata or {})
        self._validate_execution_metadata(metadata or {})
        contribution_id = f"{integration_id}:{local_id}"
        with self._lock:
            if contribution_id in self._items:
                raise PlatformConflictError("Integration contribution id is already registered.")
            self._sequence += 1
            contribution = Contribution(
                id=contribution_id,
                local_id=local_id,
                kind=kind,
                integration_id=integration_id,
                connection_id=connection_id,
                metadata=dict(metadata or {}),
                handler=handler,
                sequence=self._sequence,
                activation_scope=activation_scope,
            )
            self._items[contribution_id] = contribution
        for callback in tuple(self._changed_callbacks):
            callback()
        return ContributionHandle(self, contribution)

    def on_changed(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._changed_callbacks.append(callback)
        return lambda: self._changed_callbacks.remove(callback) if callback in self._changed_callbacks else None

    def list(
        self,
        *,
        integration_id: str | None = None,
        kind: ContributionKind | None = None,
        connection_id: str | None = None,
    ) -> list[Contribution]:
        with self._lock:
            return [
                item for item in sorted(self._items.values(), key=lambda value: value.sequence)
                if (integration_id is None or item.integration_id == integration_id)
                and (kind is None or item.kind == kind)
                and (connection_id is None or item.connection_id == connection_id)
            ]

    def get(self, contribution_id: str) -> Contribution:
        with self._lock:
            contribution = self._items.get(contribution_id)
        if contribution is None:
            raise PlatformNotFoundError("Integration contribution not found.")
        return contribution

    def invoke(
        self,
        contribution_id: str,
        *args: Any,
        invocation_context: CapabilityInvocationContext | None = None,
        **kwargs: Any,
    ) -> Any:
        contribution = self.get(contribution_id)
        if contribution.handler is None:
            raise PlatformOperationError("Integration contribution has no runtime handler.")
        self._ensure_active(contribution)
        if (
            self.worker_bridge is not None
            and str(contribution.metadata.get("execution_plane") or "direct") == "worker"
        ):
            return self.worker_bridge.invoke(
                contribution,
                args,
                kwargs,
                invocation_context=invocation_context,
                timeout_seconds=contribution.metadata.get("execution_timeout_seconds"),
            )
        if str(contribution.metadata.get("execution_plane") or "direct") == "container":
            if self.container_bridge is None:
                raise PlatformOperationError("Container runner not connected.")
            return self.container_bridge.invoke(
                contribution, args, kwargs,
                invocation_context=invocation_context,
                timeout_seconds=contribution.metadata.get("execution_timeout_seconds"),
            )
        scope = contribution.activation_scope
        admission = scope.admit() if scope is not None else None
        if admission is not None:
            admission.__enter__()
        try:
            if invocation_context is None:
                return self.execution.run(
                    f"contribution:{contribution.id}",
                    contribution.handler,
                    *args,
                    timeout_seconds=contribution.metadata.get("execution_timeout_seconds"),
                    **kwargs,
                )
            with bind_capability_context(invocation_context):
                return self.execution.run(
                    f"contribution:{contribution.id}",
                    contribution.handler,
                    *args,
                    timeout_seconds=contribution.metadata.get("execution_timeout_seconds"),
                    **kwargs,
                )
        finally:
            if admission is not None:
                admission.__exit__(None, None, None)

    def dispatch_events(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch to matching event handlers without letting one failure stop another."""
        delivered = 0
        failures: list[str] = []
        for contribution in self.list(kind="event"):
            pattern = str(contribution.metadata.get("pattern") or "")
            if not self._event_matches(pattern, event_type):
                continue
            try:
                self.invoke(contribution.id, {"type": event_type, "payload": payload})
                delivered += 1
            except Exception:
                failures.append(contribution.id)
        return {"delivered": delivered, "failures": failures}

    def run_health_checks(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for contribution in self.list(kind="health"):
            try:
                value = self.invoke(contribution.id)
                results[contribution.id] = {"status": "ok", "result": value}
            except Exception as exc:
                results[contribution.id] = {"status": "error", "error": type(exc).__name__}
        return results

    def disable_integration(self, integration_id: str) -> dict[str, Any]:
        """Block new registrations, disable the owner, then dispose its runtime state."""
        return self.integrations.disable(integration_id)

    def unregister_integration(self, integration_id: str) -> int:
        """Remove every runtime contribution in reverse registration order."""
        with self._lock:
            owned = sorted(
                (item for item in self._items.values() if item.integration_id == integration_id),
                key=lambda value: value.sequence,
                reverse=True,
            )
        for contribution in owned:
            self._remove(contribution.id, sequence=contribution.sequence)
        return len(owned)

    def _remove(self, contribution_id: str, *, sequence: int | None = None) -> bool:
        with self._lock:
            contribution = self._items.get(contribution_id)
            if contribution is None or (sequence is not None and contribution.sequence != sequence):
                return False
            del self._items[contribution_id]
        dispose = contribution.metadata.get("dispose")
        if callable(dispose):
            try:
                dispose()
            except Exception:
                # Cleanup is best-effort; state has already been detached.
                pass
        for callback in tuple(self._changed_callbacks):
            callback()
        return True

    def _ensure_active(self, contribution: Contribution) -> None:
        integration = self.integrations.get(contribution.integration_id)
        if not integration["enabled"]:
            self.unregister_integration(contribution.integration_id)
            raise PlatformOperationError("Integration is disabled.")
        if contribution.connection_id is not None and self.connections is not None:
            connection = self.connections.get(contribution.integration_id, contribution.connection_id)
            if not connection["enabled"]:
                raise PlatformOperationError("Integration connection is disabled.")

    @staticmethod
    def _event_matches(pattern: str, event_type: str) -> bool:
        return pattern == "*" or pattern == event_type or (pattern.endswith(".*") and event_type.startswith(pattern[:-1]))

    @staticmethod
    def _validate_mcp_metadata(metadata: dict[str, Any]) -> None:
        name = str(metadata.get("name") or "")
        description = str(metadata.get("description") or "")
        input_schema = metadata.get("input_schema")
        caller_capability = str(metadata.get("caller_capability") or "")
        integration_capability = str(metadata.get("integration_capability") or "")
        project_scope = str(metadata.get("project_scope") or "")
        if not re.fullmatch(r"lynewright\.integration\.[a-z][a-z0-9_.-]{0,119}", name):
            raise PlatformValidationError("MCP contribution names must use the lynewright.integration namespace.")
        if not description or len(description) > 2_000:
            raise PlatformValidationError("MCP contribution descriptions are required and limited to 2,000 characters.")
        if not isinstance(input_schema, dict):
            raise PlatformValidationError("MCP contributions must declare an object input_schema.")
        try:
            if len(json.dumps(input_schema).encode("utf-8")) > 64 * 1024:
                raise PlatformValidationError("MCP contribution input_schema exceeds the 64 KiB limit.")
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("MCP contribution input_schema must be JSON serializable.") from exc
        if caller_capability not in PUBLIC_CAPABILITIES or integration_capability not in PUBLIC_CAPABILITIES:
            raise PlatformValidationError("MCP contribution capabilities must be known public capabilities.")
        if project_scope not in {"none", "optional", "required"}:
            raise PlatformValidationError("MCP contribution project_scope must be none, optional, or required.")

    @staticmethod
    def _validate_job_metadata(metadata: dict[str, Any]) -> None:
        policy = str(metadata.get("active_job_policy") or "drain")
        if policy not in {"drain", "cancel", "detach"}:
            raise PlatformValidationError("Integration job policy must be drain, cancel, or detach.")

    @staticmethod
    def _validate_execution_metadata(metadata: dict[str, Any]) -> None:
        execution_plane = str(metadata.get("execution_plane") or "direct")
        if execution_plane not in {"direct", "worker", "container"}:
            raise PlatformValidationError("Integration execution plane must be direct, worker, or container.")
        timeout = metadata.get("execution_timeout_seconds")
        if timeout is None:
            return
        try:
            value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("Integration execution timeout must be a number.") from exc
        if not 0.05 <= value <= 300:
            raise PlatformValidationError("Integration execution timeout must be between 0.05 and 300 seconds.")
