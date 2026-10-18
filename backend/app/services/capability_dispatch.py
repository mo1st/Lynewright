"""Host infrastructure for executing a capability through its contract.

    selection decides WHO
    contract  decides WHAT
    SDK/context decides WITH WHAT AUTHORITY

The three were separately correct and never joined up. Compilation,
activation, recovery, selection, binding, and provenance were all real in
production, but nothing ever dispatched *into* an integration-provided
capability, because no `IntegrationSDKFactory` was constructed outside tests.
An `sdk_scoped` provider fails closed without one, so those providers had been
compiled and registered and never once invoked.

This module is where the host builds that infrastructure. The factory is
long-lived; the SDK it makes is not. Every invocation gets a fresh one, built
from the invocation's own tenant, project, actor, and trace, so nothing
carrying authority outlives the request that justified it.

There is deliberately no fallback between execution paths. If the selected
provider does not implement the contract, that is the answer -- quietly trying
a different implementation would return data under the selected provider's
name, which is the provenance defect in a subtler form.
"""
from __future__ import annotations

import threading
from typing import Any

from app.services.capability_context import CapabilityInvocationContext
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformNotFoundError, PlatformValidationError


_lock = threading.Lock()
_factories: dict[str, Any] = {}
_registries: dict[str, Any] = {}


def get_sdk_factory(tenant_id: str) -> Any:
    """The tenant's long-lived SDK factory.

    Long-lived on purpose: it holds only the wiring needed to *build* an SDK,
    never a built one. What it produces is per-request, which is the invariant
    that keeps one tenant's authority from being served to another.
    """
    if not tenant_id:
        raise PlatformValidationError("An SDK factory requires a tenant id.")
    with _lock:
        factory = _factories.get(tenant_id)
        if factory is None:
            from app.services.integration_capabilities import IntegrationAuthorizationService
            from app.services.integration_connections import ConnectionProfileService
            from app.services.integration_registry import IntegrationRegistry
            from app.services.integration_sdk import IntegrationSDKFactory
            from app.services.platform_services import PlatformStore

            store = PlatformStore()
            integrations = IntegrationRegistry(store, tenant_id=tenant_id)
            factory = IntegrationSDKFactory(
                store, tenant_id=tenant_id,
                authorization=IntegrationAuthorizationService(integrations),
                connections=ConnectionProfileService(
                    store, tenant_id=tenant_id, registry=integrations),
            )
            _factories[tenant_id] = factory
        return factory


def get_contract_registry(tenant_id: str) -> Any:
    """The tenant's contract registry, with an SDK factory attached."""
    if not tenant_id:
        raise PlatformValidationError("A contract registry requires a tenant id.")
    with _lock:
        registry = _registries.get(tenant_id)
        if registry is not None:
            return registry
    from app.services.integration_host import contributions
    from app.services.integration_capability_evidence import (
        IntegrationCapabilityEvidenceService,
    )
    from app.services.integration_provider_selection import (
        IntegrationProviderSelectionService,
    )
    from app.services.integration_registry import IntegrationRegistry
    from app.services.integration_service_contracts import (
        IntegrationServiceContractRegistry,
    )
    from app.services.platform_services import PlatformStore

    integrations = IntegrationRegistry(PlatformStore(), tenant_id=tenant_id)
    registry = IntegrationServiceContractRegistry(
        contributions,
        evidence=IntegrationCapabilityEvidenceService(integrations),
        selections=IntegrationProviderSelectionService(integrations, tenant_id=tenant_id),
        sdk_factory=get_sdk_factory(tenant_id),
    )
    with _lock:
        return _registries.setdefault(tenant_id, registry)


def reset_capability_dispatch() -> None:
    """Drop cached host wiring. For tests and for a reconfigured process."""
    with _lock:
        _factories.clear()
        _registries.clear()


def contract_implementation(
    contract: str, *, provider_id: str, tenant_id: str, project_id: str | None,
    registry: Any = None,
) -> str:
    """The implementation the selected provider offers for this contract, if any.

    Returns the contribution id rather than a boolean so the caller can report
    what actually ran, instead of asserting that something did.
    """
    resolved = registry or get_contract_registry(tenant_id)
    for contribution in resolved.contributions.list(kind="service"):
        if contribution.integration_id != provider_id:
            continue
        if str(contribution.metadata.get("contract") or "") == contract:
            return str(contribution.id)
    return ""



def _host_actor(tenant_id: str, integration_id: str) -> CapabilityActor:
    """A caller permitted exactly what the operator granted this integration.

    Not a privileged actor with a narrower scope, but a narrow actor: if the
    operator has granted nothing, the host can do nothing through this path,
    which is the same answer an unprivileged caller would get.
    """
    from app.services.extension_registry import ExtensionRegistry
    from app.services.platform_services import PlatformStore

    granted: frozenset[str] = frozenset()
    if integration_id:
        try:
            record = ExtensionRegistry(PlatformStore(), tenant_id=tenant_id).get(integration_id)
            granted = frozenset(record.get("granted_capabilities") or ())
        except Exception:  # noqa: BLE001 - an unknown integration grants nothing
            granted = frozenset()
    return CapabilityActor(
        client_id="internal-capability-dispatch", name="Capability Dispatch",
        client_type="internal", permission_mode="internal",
        granted_capabilities=granted, internal=True,
    )


def invoke_through_contract(
    *,
    contract: str,
    operation: str,
    payload: dict[str, Any],
    context: CapabilityInvocationContext,
    actor: CapabilityActor | None = None,
    registry: Any = None,
) -> dict[str, Any]:
    """Execute one capability through the selected provider's contract.

    The caller is an internal host actor bounded by exactly what the operator
    granted this integration. Effective authority is the intersection of the
    caller's capabilities, the integration's grants, and what its manifest
    currently requests, so making the caller the grant means the operator's
    decision is the binding constraint rather than something a broader host
    actor could widen. The platform manager is deliberately not used: it holds
    only internal capabilities, and giving it more would make every host-driven
    invocation the most privileged one in the system.
    """
    resolved = registry or get_contract_registry(tenant_id=context.tenant_id)
    if actor is None:
        actor = _host_actor(context.tenant_id, context.integration_id or "")
    return resolved.invoke(
        contract, operation, payload,
        project_id=context.project_id,
        actor=actor,
        invocation_context=context,
    )


__all__ = [
    "PlatformNotFoundError",
    "contract_implementation",
    "get_contract_registry",
    "get_sdk_factory",
    "invoke_through_contract",
    "reset_capability_dispatch",
]
