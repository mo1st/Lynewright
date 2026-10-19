"""Account-session consent and grant management for inbound MCP OAuth."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import get_settings
from app.database import resolve_session
from app.models.mcp_oauth import (
    MCPOAuthConnection,
    MCPOAuthDecision,
    MCPOAuthDecisionResult,
    MCPOAuthRequestDetails,
)
from app.security import _bearer_token
from app.services.application_logging import log_event
from app.services.mcp_oauth import (
    MCPOAuthError,
    MCPOAuthService,
    canonical_public_origin,
    mcp_oauth_enabled,
)
from app.services.platform_services import PlatformStore

router = APIRouter(prefix="/api/mcp-oauth", tags=["mcp-oauth"])


def require_real_platform_admin(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Require a persisted, active global admin session, never a synthetic admin."""
    if not mcp_oauth_enabled():
        raise HTTPException(status_code=404, detail="OAuth connections are unavailable.")
    token = _bearer_token(authorization)
    user = resolve_session(token) if token else None
    if user is None or bool(user.get("synthetic")):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    if int(user.get("id", 0)) <= 0 or not bool(user.get("is_active")) or str(user.get("role")) != "admin":
        raise HTTPException(status_code=403, detail="An active platform administrator is required.")
    return user


def _service() -> MCPOAuthService:
    issuer = canonical_public_origin(get_settings().public_url)
    return MCPOAuthService(
        PlatformStore(),
        issuer_url=f"{issuer}/",
        resource_url=f"{issuer}/mcp",
    )


def _raise_service_error(exc: MCPOAuthError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/requests/{request_id}", response_model=MCPOAuthRequestDetails)
def get_oauth_request(
    request_id: str,
    user: dict[str, Any] = Depends(require_real_platform_admin),
) -> dict[str, Any]:
    try:
        return _service().request_details(request_id, int(user["id"]))
    except MCPOAuthError as exc:
        _raise_service_error(exc)


@router.post(
    "/requests/{request_id}/decision",
    response_model=MCPOAuthDecisionResult,
)
def decide_oauth_request(
    request_id: str,
    payload: MCPOAuthDecision,
    user: dict[str, Any] = Depends(require_real_platform_admin),
) -> dict[str, str]:
    try:
        redirect_to = _service().decide(request_id, int(user["id"]), payload)
    except MCPOAuthError as exc:
        _raise_service_error(exc)
    log_event(
        level="info",
        category="authorization",
        source="mcp.oauth.consent",
        message="MCP OAuth authorization request decided.",
        details={
            "approved": payload.approved,
            "scopes": sorted(set(payload.scopes)) if payload.approved else [],
            "admin_user_id": int(user["id"]),
        },
        request_path="/api/mcp-oauth/requests/{id}/decision",
    )
    return {"redirect_to": redirect_to}


@router.get("/connections")
def list_oauth_connections(
    user: dict[str, Any] = Depends(require_real_platform_admin),
) -> dict[str, list[dict[str, Any]]]:
    try:
        return {"connections": _service().list_connections(int(user["id"]))}
    except MCPOAuthError as exc:
        _raise_service_error(exc)


@router.post("/connections/{grant_id}/revoke", response_model=MCPOAuthConnection)
def revoke_oauth_connection(
    grant_id: str,
    user: dict[str, Any] = Depends(require_real_platform_admin),
) -> dict[str, Any]:
    try:
        result = _service().revoke_connection(grant_id, int(user["id"]))
        connections = _service().list_connections(int(user["id"]))
    except MCPOAuthError as exc:
        _raise_service_error(exc)
    log_event(
        level="info",
        category="authorization",
        source="mcp.oauth.revoke",
        message="MCP OAuth connection revoked.",
        details={"application_name": result["application_name"], "admin_user_id": int(user["id"])},
        request_path="/api/mcp-oauth/connections/{id}/revoke",
    )
    item = next((entry for entry in connections if entry["id"] == grant_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="OAuth connection not found.")
    return item
