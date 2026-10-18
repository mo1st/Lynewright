"""Host-owned capability capabilities, surfaces, and deterministic placement."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformValidationError


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    id: str
    version: str
    contract_id: str
    operation: str
    name: str
    description: str
    required_actor_capability: str
    risk_class: str
    project_scope: Literal["none", "optional", "required"]
    resource_scope: Literal["none", "optional", "required"]
    connection_scope: Literal["integration", "connection"]
    required_resource_role: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    semantic_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.version or not self.contract_id or not self.operation:
            raise PlatformValidationError("Capability descriptors require stable identity and operation fields.")
        if self.project_scope not in {"none", "optional", "required"} or self.resource_scope not in {"none", "optional", "required"}:
            raise PlatformValidationError("Capability descriptor scope is invalid.")
        if self.connection_scope not in {"integration", "connection"} or not self.risk_class:
            raise PlatformValidationError("Capability descriptor policy fields are invalid.")


@dataclass(frozen=True, slots=True)
class Placement:
    capability_id: str
    surface_path: str
    reason: Literal["EXACT", "NAMESPACE", "FAMILY", "SEMANTIC", "PROVIDER_PREFERRED", "FALLBACK"]


_HOST_SURFACE_REGISTRY: set[str] = set()


def register_host_surface(path: str):
    """Register one host surface contributed by an optional extension."""
    key = str(path or "").strip()
    if not key:
        raise ValueError("A host surface path is required.")
    existed = key in _HOST_SURFACE_REGISTRY
    _HOST_SURFACE_REGISTRY.add(key)

    def unregister() -> bool:
        if existed or key not in _HOST_SURFACE_REGISTRY:
            return False
        _HOST_SURFACE_REGISTRY.remove(key)
        return True

    return unregister


class HostSurfaceTree:
    """Closed host-owned surface vocabulary for V1."""

    DEFAULT_SURFACES = frozenset()

    def __init__(self, surfaces: set[str] | frozenset[str] | None = None) -> None:
        self._surfaces = frozenset(_HOST_SURFACE_REGISTRY if surfaces is None else surfaces)

    def contains(self, path: str) -> bool:
        return path in self._surfaces

    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._surfaces))

    def require(self, path: str) -> None:
        if not self.contains(path):
            raise PlatformValidationError("Integration cannot create or target an unowned host surface.")


class CapabilityRegistry:
    """Core-owned descriptors and provider implementations."""

    def __init__(self) -> None:
        self._descriptors: dict[str, CapabilityDescriptor] = {}
        self._providers: dict[str, dict[str, str]] = {}

    def register(self, descriptor: CapabilityDescriptor) -> CapabilityDescriptor:
        existing = self._descriptors.get(descriptor.id)
        if existing is not None and existing != descriptor:
            raise PlatformConflictError("Capability descriptor identity is already registered with a different definition.")
        self._descriptors[descriptor.id] = descriptor
        return descriptor

    def implement(self, *, capability_id: str, integration_id: str, connection_id: str | None = None) -> None:
        if capability_id not in self._descriptors:
            raise PlatformNotFoundError("Capability descriptor is not registered by Core.")
        key = f"{integration_id}:{connection_id or ''}"
        self._providers.setdefault(capability_id, {})[key] = integration_id

    def unimplement(self, *, capability_id: str, integration_id: str, connection_id: str | None = None) -> bool:
        """Remove one activation-owned implementation without affecting peers."""
        implementations = self._providers.get(capability_id)
        if implementations is None:
            return False
        key = f"{integration_id}:{connection_id or ''}"
        removed = implementations.pop(key, None) is not None
        if not implementations:
            self._providers.pop(capability_id, None)
        return removed

    def descriptor(self, capability_id: str) -> CapabilityDescriptor:
        try:
            return self._descriptors[capability_id]
        except KeyError as exc:
            raise PlatformNotFoundError("Capability descriptor was not found.") from exc

    def providers(self, capability_id: str) -> tuple[str, ...]:
        return tuple(sorted(set(self._providers.get(capability_id, {}).values())))

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(sorted(self._descriptors.values(), key=lambda item: item.id))

    def capabilities_for_provider(self, integration_id: str) -> tuple[CapabilityDescriptor, ...]:
        return tuple(item for item in self.descriptors() if integration_id in self.providers(item.id))


class CapabilityPlacementEngine:
    """Resolves placement deterministically and never trusts provider IA."""

    def __init__(self, surfaces: HostSurfaceTree | None = None) -> None:
        self.surfaces = surfaces or HostSurfaceTree()
        self._exact: dict[str, str] = {}
        self._namespace: dict[str, str] = {}
        self._family: dict[str, str] = {}
        self._semantic: dict[str, str] = {}

    def map_exact(self, capability_id: str, surface_path: str) -> None:
        self.surfaces.require(surface_path)
        self._exact[capability_id] = surface_path

    def map_namespace(self, namespace: str, surface_path: str) -> None:
        self.surfaces.require(surface_path)
        self._namespace[namespace.rstrip(".")] = surface_path

    def map_family(self, family: str, surface_path: str) -> None:
        self.surfaces.require(surface_path)
        self._family[family.rstrip(".")] = surface_path

    def map_semantic(self, tag: str, surface_path: str) -> None:
        self.surfaces.require(surface_path)
        self._semantic[tag] = surface_path

    def resolve(self, descriptor: CapabilityDescriptor, *, provider_preferred: str | None = None) -> Placement:
        if descriptor.id in self._exact:
            return Placement(descriptor.id, self._exact[descriptor.id], "EXACT")
        parts = descriptor.id.split(".")
        for length in range(len(parts) - 1, 0, -1):
            namespace = ".".join(parts[:length])
            if namespace in self._namespace:
                return Placement(descriptor.id, self._namespace[namespace], "NAMESPACE")
        family = ".".join(parts[:2]) if len(parts) > 1 else descriptor.id
        if family in self._family:
            return Placement(descriptor.id, self._family[family], "FAMILY")
        for tag in sorted(descriptor.semantic_tags):
            if tag in self._semantic:
                return Placement(descriptor.id, self._semantic[tag], "SEMANTIC")
        if provider_preferred and self.surfaces.contains(provider_preferred):
            return Placement(descriptor.id, provider_preferred, "PROVIDER_PREFERRED")
        return Placement(descriptor.id, "settings.connections.provider_tools", "FALLBACK")


class ControlPlaneRegistry:
    """Single join point for descriptors, providers, readiness, and placement."""

    def __init__(self, *, capabilities: CapabilityRegistry | None = None, placement: CapabilityPlacementEngine | None = None) -> None:
        self.capabilities = capabilities or CapabilityRegistry()
        self.placement = placement or CapabilityPlacementEngine()
        self._readiness: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._ui: dict[str, Any] = {}

    def register_ui(self, contribution: Any) -> Any:
        from app.services.control_plane_ui import DeclarativeUISchemaValidator
        validated = DeclarativeUISchemaValidator(self.placement.surfaces).validate(contribution)
        existing = self._ui.get(validated.component_id)
        if existing is not None and existing != validated:
            raise PlatformConflictError("UI component identity is already registered with a different definition.")
        self._ui[validated.component_id] = validated
        return validated

    def ui_components(self, *, surface_path: str | None = None) -> tuple[Any, ...]:
        values = self._ui.values()
        if surface_path is not None:
            values = (item for item in values if item.placement_preference == surface_path)
        return tuple(sorted(values, key=lambda item: item.component_id))

    def component(self, component_id: str) -> Any:
        try:
            return self._ui[component_id]
        except KeyError as exc:
            raise PlatformNotFoundError("Control Plane component was not found.") from exc

    def set_readiness(self, *, capability_id: str, tenant_id: str, project_id: str | None, value: dict[str, Any]) -> None:
        self.capabilities.descriptor(capability_id)
        self._readiness[(tenant_id, project_id or "", capability_id)] = dict(value)

    def describe(self, *, tenant_id: str, capability_id: str, project_id: str | None = None, provider_preferred: str | None = None) -> dict[str, Any]:
        descriptor = self.capabilities.descriptor(capability_id)
        placement = self.placement.resolve(descriptor, provider_preferred=provider_preferred)
        return {
            "descriptor": descriptor,
            "providers": self.capabilities.providers(capability_id),
            "placement": placement,
            "readiness": self._readiness.get((tenant_id, project_id or "", capability_id), {"state": "UNAVAILABLE", "code": "NO_READINESS"}),
        }


__all__ = [
    "CapabilityRegistry", "CapabilityDescriptor", "CapabilityPlacementEngine",
    "ControlPlaneRegistry", "HostSurfaceTree", "Placement",
]
