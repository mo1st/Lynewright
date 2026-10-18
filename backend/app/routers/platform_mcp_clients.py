"""Admin API for provisioning external clients for the Lynewright MCP server."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.security import require_admin_token
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService
from app.services.platform_models import ClientCreateInput, ClientUpdateInput
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformServiceError,
)


router = APIRouter(
    prefix="/api/platform/mcp-clients",
    tags=["platform-mcp-clients"],
    dependencies=[Depends(require_admin_token)],
)


def _manager() -> CapabilityActor:
    return CapabilityActor.platform_manager()


def _service_error(exc: PlatformServiceError) -> HTTPException:
    if isinstance(exc, PlatformNotFoundError):
        status_code = 404
    elif isinstance(exc, PlatformConflictError):
        status_code = 409
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=exc.message)


@router.get("")
def list_platform_mcp_clients() -> dict:
    """List client metadata only. Provisioned tokens are deliberately never returned."""
    try:
        return {"clients": ClientAuthorizationService().list_clients(actor=_manager())}
    except PlatformServiceError as exc:
        raise _service_error(exc) from exc


@router.post("", status_code=201)
def create_platform_mcp_client(payload: ClientCreateInput) -> dict:
    """Provision a client and expose its token exactly once in this response."""
    try:
        return ClientAuthorizationService().create_client(payload, actor=_manager())
    except PlatformServiceError as exc:
        raise _service_error(exc) from exc


@router.patch("/{client_id}")
def update_platform_mcp_client(client_id: str, payload: ClientUpdateInput) -> dict:
    """Update client policy or enablement. Disabling revokes future authentication."""
    try:
        return ClientAuthorizationService().update_client(client_id, payload, actor=_manager())
    except PlatformServiceError as exc:
        raise _service_error(exc) from exc
