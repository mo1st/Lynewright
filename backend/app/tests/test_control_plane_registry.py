from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.control_plane_registry import (
    CapabilityRegistry,
    CapabilityDescriptor,
    CapabilityPlacementEngine,
    ControlPlaneRegistry,
    HostSurfaceTree,
    register_host_surface,
)
from app.services.control_plane_defaults import seed_default_control_plane_registry
from app.services.control_plane_ui import UIContribution
from app.services.platform_services import PlatformConflictError, PlatformValidationError


def _descriptor(capability_id: str = "reports.summary", tags: tuple[str, ...] = ()) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=capability_id, version="1.0.0", contract_id="lynewright.reports", operation="summary",
        name="Report summary", description="Summary", required_actor_capability="reports:read",
        risk_class="read", project_scope="required", resource_scope="required", connection_scope="connection",
        required_resource_role="reports.read", semantic_tags=tags,
    )


def test_host_surface_tree_rejects_provider_created_ia() -> None:
    surfaces = HostSurfaceTree()
    assert surfaces.paths() == ()
    with pytest.raises(PlatformValidationError):
        surfaces.require("core.test.reports")
    unregister = register_host_surface("core.test.reports")
    try:
        assert HostSurfaceTree().contains("core.test.reports")
    finally:
        unregister()


def test_placement_precedence_is_deterministic_and_falls_back() -> None:
    surfaces = HostSurfaceTree({
        "core.analytics.summary", "core.analytics.overview", "core.analytics.health",
    })
    engine = CapabilityPlacementEngine(surfaces)
    engine.map_semantic("reports", "core.analytics.summary")
    engine.map_family("reports", "core.analytics.overview")
    engine.map_namespace("reports", "core.analytics.health")
    engine.map_exact("reports.summary", "core.analytics.summary")
    assert engine.resolve(_descriptor()).reason == "EXACT"
    assert engine.resolve(_descriptor("reports.history")).reason == "NAMESPACE"
    assert engine.resolve(_descriptor("metrics.summary", ("reports",))).reason == "SEMANTIC"
    unknown = engine.resolve(_descriptor("provider.foo.bar"))
    assert unknown.reason == "FALLBACK" and unknown.surface_path == "settings.connections.provider_tools"


def test_control_plane_registry_joins_provider_and_readiness() -> None:
    capabilities = CapabilityRegistry()
    capabilities.register(_descriptor())
    capabilities.implement(capability_id="reports.summary", integration_id="umami", connection_id="primary")
    registry = ControlPlaneRegistry(capabilities=capabilities)
    registry.set_readiness(capability_id="reports.summary", tenant_id="tenant-a", project_id="project-a", value={"state": "READY"})
    described = registry.describe(tenant_id="tenant-a", project_id="project-a", capability_id="reports.summary")
    assert described["providers"] == ("umami",)
    assert described["readiness"]["state"] == "READY"


def test_capability_definition_cannot_be_risk_downgraded_after_registration() -> None:
    capabilities = CapabilityRegistry()
    registered = capabilities.register(_descriptor())

    with pytest.raises(PlatformConflictError):
        capabilities.register(replace(registered, risk_class="SAFE_OPERATION"))

    assert capabilities.descriptor(registered.id).risk_class == registered.risk_class


def test_default_registry_starts_empty_without_optional_contributions() -> None:
    registry = seed_default_control_plane_registry(ControlPlaneRegistry())
    assert registry.capabilities.descriptors() == ()
    assert registry.ui_components() == ()
    assert registry.placement.surfaces.paths() == ()


def test_one_provider_can_cover_multiple_capabilities_and_host_surfaces() -> None:
    surfaces = HostSurfaceTree({"core.analytics.summary", "core.analytics.history"})
    registry = ControlPlaneRegistry(placement=CapabilityPlacementEngine(surfaces))
    provider = "lynewright.integration.multi"
    capabilities = (
        _descriptor("reports.summary", ("reports",)),
        _descriptor("reports.history", ("reports",)),
    )
    for descriptor, surface, component in zip(
        capabilities,
        ("core.analytics.summary", "core.analytics.history"),
        ("reports.summary", "reports.history"),
    ):
        registry.capabilities.register(descriptor)
        registry.capabilities.implement(capability_id=descriptor.id, integration_id=provider, connection_id="primary")
        registry.capabilities.implement(capability_id=descriptor.id, integration_id=provider, connection_id="backup")
        registry.placement.map_exact(descriptor.id, surface)
        registry.register_ui(UIContribution(
            schema_version="1", component_id=component, primitive="status_card",
            capability_ids=(descriptor.id,), placement_preference=surface,
        ))

    assert registry.capabilities.capabilities_for_provider(provider) == tuple(sorted(capabilities, key=lambda item: item.id))
    assert registry.capabilities.providers("reports.summary") == (provider,)
    assert registry.capabilities.providers("reports.history") == (provider,)
    assert registry.placement.resolve(capabilities[0]).surface_path == "core.analytics.summary"
    assert registry.placement.resolve(capabilities[1]).surface_path == "core.analytics.history"
    assert {item.component_id for item in registry.ui_components()} == {"reports.summary", "reports.history"}
