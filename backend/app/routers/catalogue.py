"""Signed catalogue lookup and administrator-scoped artifact installation."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.security import require_tenant_role
from app.services.catalogue import CatalogueError, CatalogueService
from app.services.platform_services import (
    PlatformConflictError, PlatformNotFoundError, PlatformOperationError,
    PlatformServiceError, PlatformValidationError,
)


router = APIRouter(prefix="/api/catalogue", tags=["catalogue"])
_ADMIN = require_tenant_role("admin")


class CatalogueInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional deployment-owned reference for private example services. The
    # host must already map it through LYNEWRIGHT_INTERNAL_ENDPOINT__<REF>.
    internal_endpoint_ref: str = Field(default="", max_length=64)


def _entry_status(service: CatalogueService, tenant_id: str) -> list[dict[str, Any]]:
    from app.services.extension_registry import ExtensionRegistry
    from app.services.integration_provenance import get_provenance
    from app.services.catalogue_installation import catalogue_installation_incomplete
    from app.services.integration_host import store

    registry = ExtensionRegistry(store, tenant_id=tenant_id)
    installed = {str(item.get("id")): item for item in registry.list()}
    result = []
    for entry in service.list_installable():
        current = installed.get(str(entry["id"]))
        value = dict(entry)
        value["installed"] = bool(current)
        provenance = get_provenance(store, tenant_id, str(entry["id"])) if current else None
        value["provenance_kind"] = provenance.get("kind") if provenance else None
        value["provenance_status"] = provenance.get("status") if provenance else None
        value["installation_incomplete"] = bool(
            current and catalogue_installation_incomplete(current, provenance, entry)
        )
        result.append(value)
    return result


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PlatformNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message)
    if isinstance(exc, PlatformConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message)
    if isinstance(exc, PlatformOperationError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message)
    if isinstance(exc, (PlatformValidationError, CatalogueError)):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, PlatformServiceError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Catalogue installation was refused.")


@router.get("")
def read_catalogue(context: dict = Depends(_ADMIN)) -> dict:
    service = CatalogueService()
    return {
        "status": service.status(),
        "entries": _entry_status(service, str(context["tenant_id"])),
    }


@router.post("/refresh")
def refresh_catalogue(context: dict = Depends(_ADMIN)) -> dict:
    service = CatalogueService()
    return {
        "status": service.refresh(),
        "entries": _entry_status(service, str(context["tenant_id"])),
    }


@router.get("/entries/{integration_id}/{version}")
def read_catalogue_entry(
    integration_id: str,
    version: str,
    _context: dict = Depends(_ADMIN),
) -> dict:
    entry = CatalogueService().get(integration_id, version)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalogue entry not found.")
    return {"entry": entry}


@router.post("/entries/{integration_id}/{version}/install", status_code=status.HTTP_201_CREATED)
def install_catalogue_entry(
    integration_id: str,
    version: str,
    payload: CatalogueInstallRequest | None = None,
    context: dict = Depends(_ADMIN),
) -> dict:
    from app.routers.integration_management import _service
    from app.services.catalogue_installation import CatalogueInstaller

    service = CatalogueService()
    try:
        installed = CatalogueInstaller(service, _service(context)).install(
            integration_id, version,
            internal_endpoint_ref=payload.internal_endpoint_ref if payload else "",
        )
        return {"integration": installed, "installed": True}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/entries/{integration_id}/{version}/repair")
def repair_catalogue_entry(
    integration_id: str,
    version: str,
    context: dict = Depends(_ADMIN),
) -> dict:
    from app.routers.integration_management import _service
    from app.services.catalogue_installation import CatalogueInstaller

    try:
        repaired = CatalogueInstaller(CatalogueService(), _service(context)).repair(integration_id, version)
        return {"integration": repaired, "repaired": True}
    except Exception as exc:
        raise _http_error(exc) from exc
