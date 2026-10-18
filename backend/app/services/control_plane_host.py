"""Process-local host registry used by the bounded Control Plane API."""
from __future__ import annotations

from dataclasses import dataclass

import json
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable

from app.services.control_plane_registry import ControlPlaneRegistry
from app.services.control_plane_actions import ControlPlaneActionDispatcher
from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformOperationError, PlatformValidationError

if TYPE_CHECKING:
    from app.services.integration_service_contracts import IntegrationServiceContractRegistry


_registries: dict[str, ControlPlaneRegistry] = {}
_dispatchers: dict[str, ControlPlaneActionDispatcher] = {}
_data_adapters: dict[str, dict[str, Callable[[int, str | None], dict[str, Any]]]] = {}
_lock = RLock()


def get_control_plane_registry(tenant_id: str) -> ControlPlaneRegistry:
    if not tenant_id:
        raise ValueError("Control Plane registry requires a tenant id.")
    with _lock:
        registry = _registries.setdefault(tenant_id, ControlPlaneRegistry())
        if not getattr(registry, "_defaults_seeded", False):
            from app.services.control_plane_defaults import (
                seed_default_control_plane_adapters,
                seed_default_control_plane_registry,
            )
            seed_default_control_plane_registry(registry)
            # The default components declare capabilities whose data Core owns;
            # registering their adapters here keeps the read path uniform
            # instead of special-casing one capability in the router.
            seed_default_control_plane_adapters(tenant_id)
            registry._defaults_seeded = True
        return registry


@dataclass(frozen=True, slots=True)
class _AdapterRecord:
    """An adapter and the identity its output must be attributed to."""

    adapter: Callable[[int, str | None], dict[str, Any]]
    implementation_id: str
    data_source: str


def register_control_plane_registry(tenant_id: str, registry: ControlPlaneRegistry) -> None:
    if not tenant_id:
        raise ValueError("Control Plane registry requires a tenant id.")
    with _lock:
        registry._defaults_seeded = True
        _registries[tenant_id] = registry


def unregister_control_plane_registry(tenant_id: str) -> None:
    with _lock:
        _registries.pop(tenant_id, None)
        _dispatchers.pop(tenant_id, None)
        _data_adapters.pop(tenant_id, None)


def register_control_plane_data_adapter(
    tenant_id: str,
    capability_id: str,
    adapter: Callable[[int, str | None], dict[str, Any]],
    *,
    implementation_id: str,
    data_source: str,
) -> Callable[[], bool]:
    """Register one bounded host-owned read adapter, with its identity.

    `implementation_id` and `data_source` are required because an adapter with
    no identity is an adapter whose output can be attributed to whoever happens
    to be registered as the capability's provider. That is how locally computed
    numbers end up labelled as a provider's data, which is worse than having no
    data at all: nobody downstream can tell.

    Callers do not choose their own identity freely -- an integration's
    registration passes the id the activation context already knows, so an
    integration cannot register an adapter claiming to be a different provider.
    """
    if not tenant_id or not capability_id or not callable(adapter):
        raise PlatformValidationError("Control Plane data adapter registration is invalid.")
    if not implementation_id or not data_source:
        raise PlatformValidationError(
            "A Control Plane data adapter must declare its implementation and data source."
        )
    record = _AdapterRecord(adapter=adapter, implementation_id=implementation_id,
                           data_source=data_source)
    with _lock:
        adapters = _data_adapters.setdefault(tenant_id, {})
        existing = adapters.get(capability_id)
        if existing is not None and existing.adapter is not adapter:
            raise PlatformConflictError("Control Plane data adapter is already registered.")
        adapters[capability_id] = record

    def dispose() -> bool:
        with _lock:
            adapters = _data_adapters.get(tenant_id, {})
            current = adapters.get(capability_id)
            if current is None or current.adapter is not adapter:
                return False
            adapters.pop(capability_id, None)
            if not adapters:
                _data_adapters.pop(tenant_id, None)
            return True

    return dispose


def get_control_plane_implementation(tenant_id: str, capability_id: str) -> dict[str, str] | None:
    """Who would actually answer this capability here, if anyone."""
    with _lock:
        record = _data_adapters.get(tenant_id, {}).get(capability_id)
    if record is None:
        return None
    return {"implementation_id": record.implementation_id, "data_source": record.data_source}


def get_control_plane_data(
    tenant_id: str,
    capability_id: str,
    *,
    site_id: int,
    project_id: str | None,
    require_implementation_id: str = "",
) -> dict[str, Any] | None:
    """Invoke a registered read adapter and enforce its response bound.

    `require_implementation_id` refuses to let a different implementation stand
    in for the one the caller resolved. Silently answering from whatever is
    registered is a failover the caller never asked for, and one whose result
    would be attributed to the provider they *did* ask for.
    """
    with _lock:
        record = _data_adapters.get(tenant_id, {}).get(capability_id)
    if record is None:
        return None
    if require_implementation_id and record.implementation_id != require_implementation_id:
        return None
    try:
        value = record.adapter(site_id, project_id)
    except Exception as exc:
        raise PlatformOperationError("Control Plane capability data is unavailable.") from exc
    if not isinstance(value, dict):
        raise ValueError("Control Plane data adapters must return an object.")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Control Plane data adapter response must be JSON serializable.") from exc
    if len(encoded) > 64 * 1024:
        raise ValueError("Control Plane data adapter response exceeds the 64 KiB limit.")
    return value


def get_control_plane_dispatcher(tenant_id: str) -> ControlPlaneActionDispatcher:
    if not tenant_id:
        raise ValueError("Control Plane dispatcher requires a tenant id.")
    with _lock:
        dispatcher = _dispatchers.setdefault(tenant_id, ControlPlaneActionDispatcher())
        if not getattr(dispatcher, "_default_handlers_registered", False):
            from app.services.control_plane_handlers import register_default_control_plane_handlers
            register_default_control_plane_handlers(tenant_id=tenant_id, dispatcher=dispatcher)
            dispatcher._default_handlers_registered = True
        return dispatcher


def register_control_plane_service_handlers(
    tenant_id: str,
    services: IntegrationServiceContractRegistry,
    *,
    require_verified: bool = True,
) -> ControlPlaneActionDispatcher:
    """Bind provider-neutral service actions to a tenant's host dispatcher.

    Integration bootstrap code calls this after constructing its
    tenant-scoped ``IntegrationServiceContractRegistry``.  Keeping the seam
    explicit prevents the host from inventing a global provider or silently
    selecting an adapter during action dispatch.
    """
    dispatcher = get_control_plane_dispatcher(tenant_id)
    from app.services.control_plane_handlers import register_social_publishing_handlers

    register_social_publishing_handlers(
        dispatcher=dispatcher, services=services, require_verified=require_verified,
    )
    # Reflect active provider-neutral service contributions in the Core-owned
    # capability capabilities so discovery cannot advertise an unimplemented
    # social action.  The activation scope removes these entries on disable.
    registry = get_control_plane_registry(tenant_id)
    integration_ids = sorted({
        str(item.integration_id)
        for item in services.contributions.list(kind="service")
        if item.metadata.get("contract") == "lynewright.social.publisher"
    })
    registered: list[str] = []
    for integration_id in integration_ids:
        for capability_id in ("social.publish.create", "social.publish.schedule"):
            try:
                registry.capabilities.implement(capability_id=capability_id, integration_id=integration_id)
                registered.append(integration_id)
            except PlatformNotFoundError:
                pass
    dispatcher._social_integration_ids = tuple(sorted(set(registered)))
    return dispatcher


def unregister_control_plane_service_handlers(
    tenant_id: str,
    services: IntegrationServiceContractRegistry,
) -> bool:
    """Remove one activation's social service bridge from the tenant host."""
    with _lock:
        dispatcher = _dispatchers.get(tenant_id)
    if dispatcher is None:
        return False
    from app.services.control_plane_handlers import unregister_social_publishing_handlers

    removed = unregister_social_publishing_handlers(dispatcher=dispatcher, services=services)
    if removed:
        registry = get_control_plane_registry(tenant_id)
        for integration_id in getattr(dispatcher, "_social_integration_ids", ()):
            for capability_id in ("social.publish.create", "social.publish.schedule"):
                registry.capabilities.unimplement(capability_id=capability_id, integration_id=integration_id)
        dispatcher._social_integration_ids = ()
    return removed


def register_control_plane_video_handlers(
    tenant_id: str,
    provider: object,
    *,
    integration_id: str,
) -> ControlPlaneActionDispatcher:
    """Bind a provider-neutral video renderer to the tenant host."""
    dispatcher = get_control_plane_dispatcher(tenant_id)
    from app.services.control_plane_handlers import register_video_render_handlers

    register_video_render_handlers(
        dispatcher=dispatcher,
        provider=provider,
        integration_id=integration_id,
    )
    registry = get_control_plane_registry(tenant_id)
    for capability_id in ("media.video.render", "media.video.status"):
        try:
            registry.capabilities.implement(capability_id=capability_id, integration_id=integration_id)
        except PlatformNotFoundError:
            # Custom registries may intentionally omit the optional video
            # descriptors; action binding remains valid for their own capabilities.
            pass
    return dispatcher


def unregister_control_plane_video_handlers(
    tenant_id: str,
    provider: object,
    *,
    integration_id: str,
) -> bool:
    """Remove one activation's video bridge from the tenant host."""
    with _lock:
        dispatcher = _dispatchers.get(tenant_id)
    if dispatcher is None:
        return False
    from app.services.control_plane_handlers import unregister_video_render_handlers

    removed = unregister_video_render_handlers(
        dispatcher=dispatcher, provider=provider, integration_id=integration_id,
    )
    if removed:
        with _lock:
            registry = _registries.get(tenant_id)
        if registry is not None:
            for capability_id in ("media.video.render", "media.video.status"):
                registry.capabilities.unimplement(
                    capability_id=capability_id,
                    integration_id=integration_id,
                )
    return removed


__all__ = [
    "get_control_plane_dispatcher",
    "get_control_plane_data",
    "get_control_plane_registry",
    "register_control_plane_registry",
    "get_control_plane_implementation",
    "register_control_plane_data_adapter",
    "register_control_plane_service_handlers",
    "unregister_control_plane_service_handlers",
    "register_control_plane_video_handlers",
    "unregister_control_plane_video_handlers",
    "unregister_control_plane_registry",
]
