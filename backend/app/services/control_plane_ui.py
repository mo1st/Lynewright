"""Host-validated declarative UI contributions for Control Plane regions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.control_plane_registry import HostSurfaceTree
from app.services.platform_services import PlatformValidationError


ALLOWED_PRIMITIVES = frozenset({
    "metric_grid", "data_table", "form", "timeseries", "status_card",
    "job_panel", "key_value", "action_bar", "approval_card", "alert_list",
    "markdown_text", "setup_requirement", "resource_binding_status",
    "dependency_status", "trace_timeline",
})


@dataclass(frozen=True, slots=True)
class UIContribution:
    schema_version: str
    component_id: str
    primitive: str
    capability_ids: tuple[str, ...]
    semantic_tags: tuple[str, ...] = ()
    placement_preference: str | None = None
    data_binding: dict[str, Any] | None = None
    empty_state: dict[str, Any] | None = None
    actions: tuple[dict[str, Any], ...] = ()


class DeclarativeUISchemaValidator:
    """Validate bounded, host-owned UI metadata before it enters the registry."""

    MAX_COMPONENT_ID = 160
    MAX_CAPABILITIES = 16
    MAX_ACTIONS = 16
    MAX_CAPABILITY_ID = 160
    MAX_SEMANTIC_TAGS = 32
    MAX_SEMANTIC_TAG = 80
    MAX_ACTION_ID = 160
    MAX_JSON_BYTES = 32 * 1024

    def __init__(self, surfaces: HostSurfaceTree | None = None) -> None:
        self.surfaces = surfaces or HostSurfaceTree()

    def validate(self, contribution: UIContribution) -> UIContribution:
        if contribution.schema_version != "1":
            raise PlatformValidationError("Only declarative UI schema version 1 is supported.")
        if not contribution.component_id or len(contribution.component_id) > self.MAX_COMPONENT_ID:
            raise PlatformValidationError("UI component_id is required and bounded.")
        if contribution.primitive not in ALLOWED_PRIMITIVES:
            raise PlatformValidationError("UI primitive is not allowed by the host schema.")
        if not contribution.capability_ids or len(contribution.capability_ids) > self.MAX_CAPABILITIES:
            raise PlatformValidationError("UI contributions require a bounded capability list.")
        if (
            any(not isinstance(item, str) or not item.strip() or len(item) > self.MAX_CAPABILITY_ID for item in contribution.capability_ids)
            or len(set(contribution.capability_ids)) != len(contribution.capability_ids)
        ):
            raise PlatformValidationError("UI capability ids must be unique, nonblank, and bounded.")
        if len(contribution.semantic_tags) > self.MAX_SEMANTIC_TAGS or (
            any(not isinstance(item, str) or not item.strip() or len(item) > self.MAX_SEMANTIC_TAG for item in contribution.semantic_tags)
            or len(set(contribution.semantic_tags)) != len(contribution.semantic_tags)
        ):
            raise PlatformValidationError("UI semantic tags must be unique and bounded.")
        if len(contribution.actions) > self.MAX_ACTIONS:
            raise PlatformValidationError("UI action count exceeds the host bound.")
        action_ids: list[str] = []
        for action in contribution.actions:
            if not isinstance(action, dict):
                raise PlatformValidationError("UI actions must be objects.")
            action_id = action.get("action") or action.get("id")
            if action_id is not None:
                if not isinstance(action_id, str) or not action_id.strip() or len(action_id) > self.MAX_ACTION_ID:
                    raise PlatformValidationError("UI action identifiers must be nonblank and bounded.")
                action_ids.append(action_id)
        if len(set(action_ids)) != len(action_ids):
            raise PlatformValidationError("UI action identifiers must be unique.")
        if contribution.placement_preference:
            self.surfaces.require(contribution.placement_preference)
        payload = {
            "schema_version": contribution.schema_version,
            "component_id": contribution.component_id,
            "primitive": contribution.primitive,
            "capability_ids": contribution.capability_ids,
            "semantic_tags": contribution.semantic_tags,
            "placement_preference": contribution.placement_preference,
            "data_binding": contribution.data_binding,
            "empty_state": contribution.empty_state,
            "actions": contribution.actions,
        }
        self._reject_raw_html(payload)
        import json
        if len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")) > self.MAX_JSON_BYTES:
            raise PlatformValidationError("UI contribution exceeds the host size bound.")
        return contribution

    def _reject_raw_html(self, value: Any) -> None:
        if isinstance(value, str) and ("<" in value or ">" in value):
            raise PlatformValidationError("Raw HTML is not allowed in declarative UI contributions.")
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in {"html", "raw_html", "dangerouslysetinnerhtml"}:
                    raise PlatformValidationError("Raw HTML fields are not allowed in declarative UI contributions.")
                self._reject_raw_html(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                self._reject_raw_html(nested)


__all__ = ["ALLOWED_PRIMITIVES", "DeclarativeUISchemaValidator", "UIContribution"]
