"""Admin API for provisioning external clients for the Lynewright MCP server."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from app.config import get_settings
from app.security import require_admin_token, require_project_context, require_tenant_context, require_user
from app.services.mcp_oauth import canonical_public_origin
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
def list_platform_mcp_clients(context: dict = Depends(require_tenant_context)) -> dict:
    """List client metadata only. Provisioned tokens are deliberately never returned."""
    try:
        public_url = canonical_public_origin(get_settings().public_url or "http://127.0.0.1:8000")
        return {
            "clients": ClientAuthorizationService().list_clients(actor=_manager(), tenant_id=str(context["tenant_id"])),
            "mcp_url": f"{public_url}/mcp",
        }
    except PlatformServiceError as exc:
        raise _service_error(exc) from exc


@router.post("", status_code=201)
def create_platform_mcp_client(
    payload: ClientCreateInput,
    context: dict = Depends(require_project_context),
    user: dict = Depends(require_user),
) -> dict:
    """Provision a client and expose its token exactly once in this response."""
    try:
        return ClientAuthorizationService().create_client(
            payload, actor=_manager(), tenant_id=str(context["tenant_id"]),
            project_ids=[str(context["project_id"])], on_behalf_of=str(user.get("email") or "system administrator"),
        )
    except PlatformServiceError as exc:
        raise _service_error(exc) from exc


@router.patch("/{client_id}")
def update_platform_mcp_client(client_id: str, payload: ClientUpdateInput) -> dict:
    """Update client policy or enablement. Disabling revokes future authentication."""
    try:
        return ClientAuthorizationService().update_client(client_id, payload, actor=_manager())
    except PlatformServiceError as exc:
        raise _service_error(exc) from exc


@router.delete("/{client_id}", status_code=204)
def delete_platform_mcp_client(client_id: str, context: dict = Depends(require_tenant_context)) -> Response:
    try:
        ClientAuthorizationService().delete_client(client_id, actor=_manager(), tenant_id=str(context["tenant_id"]))
        return Response(status_code=204)
    except PlatformServiceError as exc:
        raise _service_error(exc) from exc
