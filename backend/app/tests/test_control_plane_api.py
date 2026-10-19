from __future__ import annotations

import pytest
from fastapi import HTTPException, Response

from app.routers.control_plane import get_component, get_component_data, get_provider, get_region
from app.services.control_plane_host import register_control_plane_data_adapter, register_control_plane_registry, unregister_control_plane_registry
from app.services.control_plane_registry import (
    CapabilityDescriptor,
    CapabilityPlacementEngine,
    ControlPlaneRegistry,
    HostSurfaceTree,
)
from app.services.control_plane_ui import UIContribution


def _registry() -> ControlPlaneRegistry:
    registry = ControlPlaneRegistry(placement=CapabilityPlacementEngine(
        HostSurfaceTree({"core.test.analytics"}),
    ))
    registry.capabilities.register(CapabilityDescriptor(
        id="analytics.web.summary", version="1.0.0", contract_id="lynewright.analytics", operation="summary",
        name="Summary", description="Summary", required_actor_capability="analytics:read", risk_class="read",
        project_scope="required", resource_scope="required", connection_scope="connection",
    ))
    registry.capabilities.implement(capability_id="analytics.web.summary", integration_id="umami", connection_id="primary")
    registry.register_ui(UIContribution(schema_version="1", component_id="analytics.summary", primitive="status_card", capability_ids=("analytics.web.summary",), placement_preference="core.test.analytics"))
    return registry


def test_control_plane_api_returns_bounded_region_provider_and_component() -> None:
    register_control_plane_registry("tenant-api", _registry())
    context = {"tenant_id": "tenant-api"}
    try:
        region_response = Response()
        region = get_region("core.test.analytics", project_id="project-a", limit=50, response=region_response, context=context)
        assert region_response.headers["ETag"].startswith('"')
        assert region["schema_version"] == "1" and region["components"][0]["component_id"] == "analytics.summary"
        assert region["components"][0]["readiness"] == {"state": "available"}
        provider = get_provider("umami", project_id="project-a", limit=50, response=Response(), context=context)
        assert provider["capabilities"][0]["providers"] == ["umami"]
        component = get_component("analytics.summary", response=Response(), context=context)
        assert component["component"]["primitive"] == "status_card"
    finally:
        unregister_control_plane_registry("tenant-api")


def test_control_plane_api_rejects_unowned_surface() -> None:
    register_control_plane_registry("tenant-api", ControlPlaneRegistry())
    try:
        with pytest.raises(HTTPException) as exc:
            get_region("provider.custom.surface", limit=50, response=Response(), context={"tenant_id": "tenant-api"})
        assert exc.value.status_code == 404
    finally:
        unregister_control_plane_registry("tenant-api")


def test_control_plane_api_marks_unknown_capabilities_for_provider_tools() -> None:
    registry = ControlPlaneRegistry(placement=CapabilityPlacementEngine(
        HostSurfaceTree({"core.test.analytics"}),
    ))
    registry.register_ui(UIContribution(schema_version="1", component_id="unknown.tool", primitive="status_card", capability_ids=("provider.unknown.read",), placement_preference="core.test.analytics"))
    register_control_plane_registry("tenant-api", registry)
    try:
        component = get_region("core.test.analytics", project_id=None, limit=50, response=Response(), context={"tenant_id": "tenant-api"})["components"][0]
        assert component["fallback"] == "provider_tools"
        assert component["fallback_reason"] == "capability_unavailable"
        assert component["readiness"] == {"state": "unavailable", "reason": "capability_unavailable"}
    finally:
        unregister_control_plane_registry("tenant-api")


def test_registered_host_data_adapter_serves_non_analytics_capability() -> None:
    from app.services.control_plane_host import get_control_plane_data

    registry = ControlPlaneRegistry()
    registry.capabilities.register(CapabilityDescriptor(
        id="media.video.status", version="1.0.0", contract_id="video", operation="status",
        name="Video status", description="Status", required_actor_capability="media:read", risk_class="read",
        project_scope="required", resource_scope="required", connection_scope="connection",
    ))
    registry.capabilities.implement(capability_id="media.video.status", integration_id="video-provider")
    register_control_plane_registry("tenant-api", registry)
    register_control_plane_data_adapter(
        "tenant-api", "media.video.status",
        lambda site_id, project_id: {
            "site_id": site_id, "project_id": project_id, "state": "completed",
        },
        implementation_id="video-provider", data_source="video-provider")
    try:
        result = get_control_plane_data("tenant-api", "media.video.status", site_id=7, project_id="project-a")
        assert result == {"site_id": 7, "project_id": "project-a", "state": "completed"}
    finally:
        unregister_control_plane_registry("tenant-api")


def test_component_data_endpoint_requires_declared_capability_and_uses_registry_adapter() -> None:
    from app.services.control_plane_host import register_control_plane_data_adapter

    registry = _registry()
    register_control_plane_registry("tenant-api", registry)
    register_control_plane_data_adapter(
        "tenant-api", "analytics.web.summary",
        lambda site_id, project_id: {"resource_id": site_id, "project_id": project_id, "health_score": 97},
        implementation_id="lynewright.integration.umami", data_source="umami",
    )
    try:
        response = Response()
        result = get_component_data(
            "analytics.summary", response=response, site_id=7, project_id="project-a", context={"tenant_id": "tenant-api"}
        )
        assert result["component_id"] == "analytics.summary"
        assert result["capability_id"] == "analytics.web.summary"
        assert result["data"] == {"resource_id": 7, "project_id": "project-a", "health_score": 97}
        assert response.headers["ETag"].startswith('"')
        with pytest.raises(HTTPException) as exc:
            get_component_data(
                "analytics.summary", response=Response(), site_id=7, project_id="project-a",
                capability_id="social.publish.create", context={"tenant_id": "tenant-api"},
            )
        assert exc.value.status_code == 404
    finally:
        unregister_control_plane_registry("tenant-api")


def test_control_plane_data_adapter_failures_are_sanitized() -> None:
    from app.services.control_plane_host import register_control_plane_data_adapter

    registry = _registry()
    register_control_plane_registry("tenant-api", registry)
    register_control_plane_data_adapter(
        "tenant-api", "analytics.web.summary",
        lambda _site_id, _project_id: (_ for _ in ()).throw(RuntimeError("provider secret")),
        implementation_id="lynewright.integration.umami", data_source="umami")
    try:
        with pytest.raises(HTTPException) as exc:
            get_component_data(
                "analytics.summary", site_id=7, project_id="project-a", response=Response(),
                context={"tenant_id": "tenant-api"},
            )
        assert exc.value.status_code == 409
        assert exc.value.detail == "Control Plane capability data is invalid."
    finally:
        unregister_control_plane_registry("tenant-api")


def test_default_adapter_never_displaces_a_provider_adapter() -> None:
    """A provider that already owns the capability keeps it."""
    from app.services.control_plane_defaults import seed_default_control_plane_adapters
    from app.services.control_plane_host import register_control_plane_data_adapter

    try:
        register_control_plane_data_adapter(
            "tenant-adapter-clash", "analytics.web.summary", lambda _s, _p: {"from": "provider"},
            implementation_id="lynewright.integration.umami", data_source="umami",
        )
        # Seeding is a no-op for a capability an integration already serves.
        assert seed_default_control_plane_adapters("tenant-adapter-clash") == []
    finally:
        unregister_control_plane_registry("tenant-adapter-clash")
