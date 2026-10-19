"""Extension-specific identities and capability intersection enforcement."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from app.services.extension_registry import ExtensionRegistry
from app.services.platform_capabilities import CapabilityActor, require_capability
from app.services.platform_services import PlatformOperationError


@dataclass(frozen=True, slots=True)
class ExtensionActor:
    """A distinct capability principal for one registered extension."""

    extension_id: str
    name: str
    permission_mode: str
    granted_capabilities: frozenset[str]
    enabled: bool
    client_type: str = "extension"
    internal: bool = False

    @property
    def client_id(self) -> str:
        """Compatibility identity for common capability primitives and audit context."""
        return f"extension:{self.extension_id}"


PlatformPolicy = Callable[[CapabilityActor, ExtensionActor, str, str | None], bool]
ProjectScopePolicy = Callable[[CapabilityActor, ExtensionActor, str], bool]


class ExtensionAuthorizationService:
    """Owner-controlled extension grants with caller/extension intersection checks."""

    def __init__(self, registry: ExtensionRegistry | None = None) -> None:
        self.registry = registry or ExtensionRegistry()

    def resolve_actor(self, extension_id: str) -> ExtensionActor:
        extension = self.registry.get(extension_id)
        return ExtensionActor(
            extension_id=str(extension["id"]),
            name=str(extension["name"]),
            permission_mode=str(extension["permission_mode"]),
            granted_capabilities=frozenset(str(item) for item in extension["granted_capabilities"]),
            enabled=bool(extension["enabled"]),
        )

    def set_permissions(
        self,
        extension_id: str,
        *,
        permission_mode: str,
        granted_capabilities: list[str],
        actor: CapabilityActor | ExtensionActor,
    ) -> dict[str, Any]:
        self._require_platform_manager(actor)
        return self.registry.set_authorization(
            extension_id,
            permission_mode=permission_mode,
            granted_capabilities=granted_capabilities,
        )

    def require_effective_capability(
        self,
        caller: CapabilityActor,
        extension_id: str,
        capability: str,
        *,
        project_id: str | None = None,
        project_scope: ProjectScopePolicy | None = None,
        platform_policy: PlatformPolicy | None = None,
        allow_disabled_integration_for_preview: bool = False,
    ) -> ExtensionActor:
        """Require both principals and optional platform policy to approve an operation."""
        extension = self.resolve_actor(extension_id)
        require_capability(caller, capability)
        if allow_disabled_integration_for_preview and not extension.enabled:
            extension = replace(extension, enabled=True)
        require_capability(extension, capability)  # type: ignore[arg-type]
        if project_id is not None:
            if project_scope is None:
                raise PlatformOperationError("Extension operations scoped to a project require a project-scope policy.")
            if not project_scope(caller, extension, project_id):
                raise PlatformOperationError("Project scope denied this extension operation.")
        if platform_policy is not None and not platform_policy(caller, extension, capability, project_id):
            raise PlatformOperationError("Platform policy denied this extension operation.")
        return extension

    @staticmethod
    def _require_platform_manager(actor: CapabilityActor | ExtensionActor) -> None:
        if not actor.internal or "capabilities:internal" not in actor.granted_capabilities:
            raise PlatformOperationError("Extension permissions may only be managed by the platform administrator.")
