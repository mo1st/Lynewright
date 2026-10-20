"""REST adapter for shared automation plans and disabled preview lines."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.models.automation_plans import (
    AutomationPlanApplyInput,
    AutomationPlanAuthorizationInput,
    AutomationPlanInput,
)
from app.security import get_current_user, require_tenant_context, require_user
from app.services.automation_plans import AutomationPlanService
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService, require_capability
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformServiceError,
    PlatformValidationError,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID, tenant_role_at_least
from app.services.tenancy import require_project_in_tenant


router = APIRouter(prefix="/api/automation", tags=["automation-plans"])


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PlatformNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PlatformConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, PlatformOperationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, PlatformValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, PlatformServiceError):
        return HTTPException(status_code=400, detail=exc.message)
    return HTTPException(status_code=500, detail="Automation plan request failed.")


def _bearer(request: Request) -> str:
    value = str(request.headers.get("authorization") or "")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def _identity(
    request: Request, *, project_id: str, capability: str = "",
    minimum_role: str = "viewer",
) -> tuple[str, str, str, CapabilityActor | None, dict[str, Any] | None]:
    """Resolve either a REST service credential or a dashboard user."""
    token = _bearer(request)
    if token.startswith("qxc_"):
        try:
            actor = ClientAuthorizationService().authenticate(token, audience="api")
        except PlatformServiceError as exc:
            raise HTTPException(status_code=401, detail="REST client authentication failed.") from exc
        if capability:
            try:
                require_capability(actor, capability)
            except PlatformServiceError as exc:
                raise _http_error(exc) from exc
        from app.services.rate_limiter import RateLimiterUnavailable, get_rate_limiter

        try:
            allowed = get_rate_limiter().consume(
                namespace="automation_rest", key=actor.client_id,
                maximum=60, window_seconds=60,
            )
        except RateLimiterUnavailable as exc:
            raise HTTPException(status_code=503, detail="Automation API is temporarily unavailable.") from exc
        if not allowed:
            raise HTTPException(status_code=429, detail="Automation API rate limit exceeded.")
        selected = str(project_id or request.headers.get("X-Lynewright-Project-ID") or "").strip()
        if not selected and len(actor.project_ids) == 1:
            selected = actor.project_ids[0]
        if not selected or selected not in set(actor.project_ids):
            raise HTTPException(status_code=404, detail="Project was not found in this assistant's authorized scope.")
        try:
            require_project_in_tenant(tenant_id=actor.tenant_id, project_id=selected)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Project was not found in this assistant's authorized scope.") from exc
        return actor.tenant_id, selected, f"rest_client:{actor.name} ({actor.client_id})", actor, None

    user = get_current_user(authorization=f"Bearer {token}" if token else None)
    if user is None:
        if token:
            raise HTTPException(status_code=401, detail="Authentication failed.")
        user = require_user(None)
    try:
        context = require_tenant_context(
            x_lynewright_tenant_id=request.headers.get("X-Lynewright-Tenant-ID"), user=user,
        )
    except HTTPException:
        raise
    if not tenant_role_at_least(str(context.get("role") or ""), minimum_role):
        raise HTTPException(status_code=403, detail=f"This action requires the tenant '{minimum_role}' role or higher.")
    selected = str(project_id or request.headers.get("X-Lynewright-Project-ID") or "").strip()
    if not selected:
        raise HTTPException(status_code=400, detail="Project is required.")
    try:
        require_project_in_tenant(tenant_id=str(context["tenant_id"]), project_id=selected)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    return (
        str(context["tenant_id"] or DEFAULT_TENANT_ID), selected,
        str(user.get("email") or user.get("id") or "dashboard-user"), None, context,
    )


def _operation_context(tenant_id: str, actor: CapabilityActor | None) -> tuple[Any, Any]:
    from app.services.integration_operations import build_tenant_operation_catalog

    operation_directory = build_tenant_operation_catalog(tenant_id)
    if actor is not None:
        return operation_directory, actor
    admin = CapabilityActor(
        client_id="dashboard-plan-authoring", name="Dashboard plan authoring",
        client_type="automation", permission_mode="full", granted_capabilities=frozenset(),
        tenant_id=tenant_id,
    )
    return operation_directory, admin


@router.get("/operations")
def search_operations(
    request: Request,
    project_id: str = Query(default="", max_length=160),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    try:
        tenant_id, selected, _created_by, actor, _context = _identity(
            request, project_id=project_id, capability="integrations:discover", minimum_role="editor",
        )
        from app.services.automation_plans import public_operation_cards

        operation_directory, operation_actor = _operation_context(tenant_id, actor)
        candidates = operation_directory.search(
            operation_actor, project_id=selected, query=q, limit=limit,
            compact=True, include_schema=True,
        )
        return {"items": public_operation_cards(candidates)}
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/plans")
def list_plans(
    request: Request,
    project_id: str = Query(default="", max_length=160),
) -> dict[str, Any]:
    try:
        tenant_id, selected, _created_by, _actor, _context = _identity(
            request, project_id=project_id, capability="automation:read", minimum_role="viewer",
        )
        return {"items": AutomationPlanService().list_plans(tenant_id=tenant_id, project_id=selected)}
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/plans", status_code=status.HTTP_201_CREATED)
def create_or_revise_plan(payload: AutomationPlanInput, request: Request) -> dict[str, Any]:
    try:
        tenant_id, selected, created_by, actor, _context = _identity(
            request, project_id=payload.project_id, capability="automation:plan", minimum_role="editor",
        )
        if actor is None and payload.plan_id:
            current = AutomationPlanService().get_plan(payload.plan_id, tenant_id=tenant_id, project_id=selected)
            if not current:
                raise PlatformNotFoundError("Automation plan was not found.")
        operation_directory, operation_actor = _operation_context(tenant_id, actor)
        value = payload.model_dump()
        value["project_id"] = selected
        result = AutomationPlanService().create_or_revise(
            value, tenant_id=tenant_id, created_by=created_by,
            originating_client_id=actor.client_id if actor else "dashboard",
            operation_directory=operation_directory, operation_actor=operation_actor,
        )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/plans/{plan_id}")
def get_plan(
    plan_id: str, request: Request,
    project_id: str = Query(default="", max_length=160),
) -> dict[str, Any]:
    try:
        tenant_id, selected, _created_by, _actor, _context = _identity(
            request, project_id=project_id, capability="automation:read", minimum_role="viewer",
        )
        return AutomationPlanService().get_plan(plan_id, tenant_id=tenant_id, project_id=selected)
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/plans/{plan_id}/authorize")
def authorize_plan(
    plan_id: str, payload: AutomationPlanAuthorizationInput, request: Request,
) -> dict[str, Any]:
    try:
        tenant_id, selected, approved_by, actor, context = _identity(
            request, project_id=payload.project_id, minimum_role="admin",
        )
        if actor is not None or context is None:
            raise HTTPException(status_code=403, detail="Only an authenticated tenant administrator can approve a plan.")
        return AutomationPlanService().authorize(
            plan_id, tenant_id=tenant_id, project_id=selected, revision=payload.revision,
            plan_hash=payload.plan_hash, approved_by=approved_by,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/plans/{plan_id}/apply")
def apply_plan(plan_id: str, payload: AutomationPlanApplyInput, request: Request) -> dict[str, Any]:
    try:
        tenant_id, selected, created_by, actor, _context = _identity(
            request, project_id=payload.project_id, capability="automation:provision", minimum_role="editor",
        )
        operation_directory, operation_actor = _operation_context(tenant_id, actor)
        return AutomationPlanService().apply_preview(
            plan_id, tenant_id=tenant_id, project_id=selected, revision=payload.revision,
            plan_hash=payload.plan_hash, created_by=created_by,
            operation_directory=operation_directory, operation_actor=operation_actor,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc) from exc
