"""Extension registry for optional Control Plane descriptors and adapters."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from app.services.control_plane_registry import ControlPlaneRegistry

RegistryContribution = Callable[[ControlPlaneRegistry], Any]
AdapterContribution = Callable[[str], Iterable[str]]
_registry_contributions: list[RegistryContribution] = []
_adapter_contributions: list[AdapterContribution] = []


def register_control_plane_defaults(
    *, registry: RegistryContribution | None = None,
    adapters: AdapterContribution | None = None,
) -> Callable[[], bool]:
    """Register optional defaults; Core starts with no business contributions."""
    if registry is not None:
        _registry_contributions.append(registry)
    if adapters is not None:
        _adapter_contributions.append(adapters)

    def unregister() -> bool:
        removed = False
        for target, contribution in (
            (_registry_contributions, registry), (_adapter_contributions, adapters),
        ):
            if contribution is not None and contribution in target:
                target.remove(contribution)
                removed = True
        return removed

    return unregister


def seed_default_control_plane_registry(registry: ControlPlaneRegistry) -> ControlPlaneRegistry:
    """Apply registered contributions. An empty registry is a valid default."""
    for contribution in tuple(_registry_contributions):
        contribution(registry)
    return registry


def seed_default_control_plane_adapters(tenant_id: str) -> list[str]:
    """Apply tenant-scoped adapter contributions, if any have been registered."""
    registered: list[str] = []
    for contribution in tuple(_adapter_contributions):
        registered.extend(str(item) for item in contribution(tenant_id))
    return registered


__all__ = [
    "register_control_plane_defaults",
    "seed_default_control_plane_adapters",
    "seed_default_control_plane_registry",
]
