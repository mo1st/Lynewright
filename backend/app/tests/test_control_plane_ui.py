from __future__ import annotations

import pytest

from app.services.control_plane_registry import CapabilityPlacementEngine, ControlPlaneRegistry, HostSurfaceTree
from app.services.control_plane_ui import DeclarativeUISchemaValidator, UIContribution
from app.services.platform_services import PlatformConflictError, PlatformValidationError


def _ui(**overrides):
    values = {
        "schema_version": "1", "component_id": "analytics.summary", "primitive": "status_card",
        "capability_ids": ("analytics.web.summary",), "placement_preference": "core.test.analytics",
    }
    values.update(overrides)
    return UIContribution(**values)


def test_validator_accepts_v1_primitive_and_host_surface() -> None:
    validator = DeclarativeUISchemaValidator(HostSurfaceTree({"core.test.analytics"}))
    assert validator.validate(_ui()).component_id == "analytics.summary"


@pytest.mark.parametrize("override", [
    {"primitive": "raw_html"},
    {"schema_version": "2"},
    {"placement_preference": "provider.custom.surface"},
    {"data_binding": {"raw_html": "<b>unsafe</b>"}},
])
def test_validator_rejects_unsafe_or_unowned_contributions(override) -> None:
    validator = DeclarativeUISchemaValidator(HostSurfaceTree({"core.test.analytics"}))
    with pytest.raises(PlatformValidationError):
        validator.validate(_ui(**override))


def test_registry_deduplicates_ui_and_filters_host_surface() -> None:
    registry = ControlPlaneRegistry(placement=CapabilityPlacementEngine(
        HostSurfaceTree({"core.test.analytics"}),
    ))
    registry.register_ui(_ui())
    registry.register_ui(_ui())
    assert [item.component_id for item in registry.ui_components(surface_path="core.test.analytics")] == ["analytics.summary"]


def test_registry_rejects_changed_definition_for_existing_ui_identity() -> None:
    registry = ControlPlaneRegistry(placement=CapabilityPlacementEngine(
        HostSurfaceTree({"core.test.analytics"}),
    ))
    registry.register_ui(_ui())
    with pytest.raises(PlatformConflictError):
        registry.register_ui(_ui(primitive="metric_grid"))


@pytest.mark.parametrize("override", [
    {"capability_ids": ("analytics.web.summary", "analytics.web.summary")},
    {"capability_ids": ("",)},
    {"semantic_tags": ("analytics", "analytics")},
    {"actions": ({"action": "refresh"}, {"action": "refresh"})},
    {"actions": ("refresh",)},
])
def test_validator_rejects_duplicate_or_malformed_metadata(override) -> None:
    with pytest.raises(PlatformValidationError):
        DeclarativeUISchemaValidator().validate(_ui(**override))
