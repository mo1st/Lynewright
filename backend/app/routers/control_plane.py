"""Bounded, tenant-authenticated Control Plane discovery endpoints."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response

from app.security import require_tenant_context
from app.services.control_plane_host import get_control_plane_data, get_control_plane_dispatcher, get_control_plane_registry
from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformOperationError, PlatformValidationError


router = APIRouter(prefix="/api/control-plane", tags=["control-plane"])


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _region_component(registry: Any, component: Any) -> dict[str, Any]:
    """Annotate unavailable capabilities so the client can use Provider Tools."""
    result = _jsonable(component)
    unavailable: list[str] = []
    for capability_id in tuple(getattr(component, "capability_ids", ())):
        try:
            registry.capabilities.descriptor(capability_id)
            if not registry.capabilities.providers(capability_id):
                unavailable.append(capability_id)
        except PlatformNotFoundError:
            unavailable.append(capability_id)
    if unavailable:
        result["fallback"] = "provider_tools"
        result["fallback_reason"] = "capability_unavailable"
        result["unavailable_capability_ids"] = unavailable[:16]
        result["readiness"] = {"state": "unavailable", "reason": "capability_unavailable"}
    else:
        result["readiness"] = {"state": "available"}
    return result


def _with_etag(response: Response, payload: dict[str, Any]) -> dict[str, Any]:
    """Attach a stable representation validator to bounded read responses."""
    encoded = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    response.headers["ETag"] = f'"{hashlib.sha256(encoded).hexdigest()}"'
    return payload


@router.get("/regions/{surface_path:path}")
def get_region(
    surface_path: str,
    response: Response,
    project_id: str | None = Query(default=None, max_length=160),
    limit: int = Query(default=50, ge=1, le=100),
    context: dict[str, Any] = Depends(require_tenant_context),
) -> dict[str, Any]:
    registry = get_control_plane_registry(context["tenant_id"])
    try:
        registry.placement.surfaces.require(surface_path)
    except PlatformValidationError as exc:
        raise HTTPException(status_code=404, detail="Control Plane surface was not found.") from exc
    components = registry.ui_components(surface_path=surface_path)[:limit]
    return _with_etag(response, {"schema_version": "1", "surface_path": surface_path, "project_id": project_id, "components": [_region_component(registry, item) for item in components]})


@router.get("/providers/{integration_id}")
def get_provider(
    integration_id: str,
    response: Response,
    project_id: str | None = Query(default=None, max_length=160),
    limit: int = Query(default=50, ge=1, le=100),
    context: dict[str, Any] = Depends(require_tenant_context),
) -> dict[str, Any]:
    registry = get_control_plane_registry(context["tenant_id"])
    descriptors = registry.capabilities.capabilities_for_provider(integration_id)[:limit]
    return _with_etag(response, {"schema_version": "1", "integration_id": integration_id, "project_id": project_id, "capabilities": [_jsonable(registry.describe(tenant_id=context["tenant_id"], project_id=project_id, capability_id=item.id)) for item in descriptors]})


@router.get("/components/{component_id}")
def get_component(component_id: str, response: Response, context: dict[str, Any] = Depends(require_tenant_context)) -> dict[str, Any]:
    registry = get_control_plane_registry(context["tenant_id"])
    try:
        component = registry.component(component_id)
    except PlatformNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Control Plane component was not found.") from exc
    return _with_etag(response, {"schema_version": "1", "component": _jsonable(component)})


@router.get("/components/{component_id}/data")
def get_component_data(
    component_id: str,
    response: Response,
    site_id: int = Query(..., ge=1),
    project_id: str | None = Query(default=None, max_length=160),
    capability_id: str | None = Query(default=None, max_length=160),
    context: dict[str, Any] = Depends(require_tenant_context),
) -> dict[str, Any]:
    """Return data for one capability declared by a host-owned component."""
    registry = get_control_plane_registry(context["tenant_id"])
    try:
        component = registry.component(component_id)
    except PlatformNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Control Plane component was not found.") from exc
    declared = tuple(getattr(component, "capability_ids", ()))
    selected = (capability_id if isinstance(capability_id, str) and capability_id else None) or (declared[0] if declared else "")
    if not selected or selected not in declared:
        raise HTTPException(status_code=404, detail="Capability is not declared by this component.")
    try:
        data = get_control_plane_data(
            str(context["tenant_id"]), selected, site_id=site_id, project_id=project_id,
        )
    except (PlatformOperationError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Control Plane capability data is invalid.",
        ) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="Control Plane capability data is unavailable.")
    return _with_etag(response, {
        "schema_version": "1",
        "component_id": component_id,
        "capability_id": selected,
        "site_id": site_id,
        "project_id": project_id,
        "data": data,
    })


@router.post("/actions/{action_token}")
def execute_action(
    action_token: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    context: dict[str, Any] = Depends(require_tenant_context),
) -> dict[str, Any]:
    try:
        return get_control_plane_dispatcher(context["tenant_id"]).dispatch(
            action_token, tenant_id=context["tenant_id"], actor_id=str(context.get("user_id") or "synthetic-admin"), payload=payload,
        )
    except (PlatformConflictError, PlatformNotFoundError, PlatformValidationError) as exc:
        status_code = 404 if isinstance(exc, PlatformNotFoundError) else 409 if isinstance(exc, PlatformConflictError) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except PlatformOperationError as exc:
        # The dispatcher has already collapsed an unexpected provider-handler
        # failure into a bounded, secret-free operation error. Letting it
        # escape here would turn that deliberate boundary into an unhandled
        # 500, so map it to a stable status instead of re-exposing the cause.
        raise HTTPException(status_code=502, detail=str(exc)) from exc


__all__ = ["router"]
