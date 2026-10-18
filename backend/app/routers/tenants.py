from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.models.tenant import TenantCreate, TenantProjectCreate, TenantProjectRead, TenantRead
from app.security import require_tenant_context, require_tenant_role, require_user
from app.services.tenancy import create_tenant, create_tenant_project, list_tenant_projects, list_user_tenants

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


@router.get("", response_model=list[TenantRead])
def read_tenants(user: dict = Depends(require_user)) -> list[dict]:
    return list_user_tenants(int(user.get("id", 0)), synthetic=bool(user.get("synthetic")))


@router.post("", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
def add_tenant(payload: TenantCreate, user: dict = Depends(require_user)) -> dict:
    try:
        return create_tenant(
            name=payload.name,
            slug=payload.slug,
            creator_user_id=None if user.get("synthetic") else int(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{tenant_id}/projects", response_model=list[TenantProjectRead])
def read_projects(
    tenant_id: str,
    context: dict = Depends(require_tenant_context),
) -> list[dict]:
    if context["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return list_tenant_projects(tenant_id)


@router.post(
    "/{tenant_id}/projects",
    response_model=TenantProjectRead,
    status_code=status.HTTP_201_CREATED,
)
def add_project(
    tenant_id: str,
    payload: TenantProjectCreate,
    context: dict = Depends(require_tenant_role("admin")),
) -> dict:
    if context["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    try:
        return create_tenant_project(
            tenant_id=tenant_id,
            name=payload.name,
            project_id=payload.project_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
