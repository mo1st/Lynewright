"""Auth dependencies: session/token resolution and role guards.

Model: users log in with email+password and carry an opaque session token as
``Authorization: Bearer <token>`` (same header shape the single-token deployment
already used). The legacy ``LYNEWRIGHT_ADMIN_TOKEN`` still works as a break-glass
admin so existing installs keep running.

Back-compat: in development, when nobody has been provisioned and no admin token
is set, auth is a no-op and requests act as a synthetic dev admin — so local dev
and the test suite need no credentials. Enforcement turns on automatically once
any user exists, an admin token is configured, or the app runs in production.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import get_settings
from app.database import resolve_session, role_at_least, users_exist
from app.services.tenant_schema import DEFAULT_TENANT_ID, tenant_role_at_least
from app.services.tenancy import (
    get_membership,
    require_project_in_tenant,
    tenant_exists,
)

# Identity used when auth is not enforced (dev no-op) or the break-glass token is
# presented. id 0 never collides with a real AUTOINCREMENT user id.
_SYNTHETIC_ADMIN: dict[str, Any] = {
    "id": 0, "email": "system", "role": "admin", "is_active": True, "synthetic": True,
}


def _secret_matches(candidate: str | None, secret: str) -> bool:
    """Constant-time equality for the app's static shared secrets.

    The configured administrator token is compared against an attacker-supplied
    header on every request, so it uses the same constant-time comparison as passwords.
    """
    if not candidate or not secret:
        return False
    return hmac.compare_digest(candidate, secret)


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    prefix = "Bearer "
    return authorization[len(prefix):].strip() if authorization.startswith(prefix) else ""


def _auth_enforced() -> bool:
    settings = get_settings()
    return settings.is_production or bool(settings.admin_token) or users_exist()


def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any] | None:
    """Resolve the caller to a user dict, or None when unauthenticated."""
    token = _bearer_token(authorization)
    if not token:
        return None
    settings = get_settings()
    if _secret_matches(token, settings.admin_token):
        return dict(_SYNTHETIC_ADMIN)
    return resolve_session(token)


def require_user(user: dict[str, Any] | None = Depends(get_current_user)) -> dict[str, Any]:
    """Require an authenticated user; falls back to a dev admin when auth is off."""
    if user is not None:
        return user
    if not _auth_enforced():
        return dict(_SYNTHETIC_ADMIN)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def require_role(minimum: str):
    """Dependency factory: require an authenticated user with at least `minimum` role."""

    def _guard(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        if not role_at_least(str(user.get("role", "")), minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the '{minimum}' role or higher.",
            )
        return user

    return _guard


# Named guards for router use.
require_editor = require_role("editor")
require_admin = require_role("admin")

def require_tenant_context(
    x_lynewright_tenant_id: str | None = Header(default=None, alias="X-Lynewright-Tenant-ID"),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Resolve and authorize the active tenant for dashboard/API requests.

    The header selects a tenant, but never grants access to it. Real users must
    have an active membership. Development/break-glass synthetic admins may
    operate across tenants, while still receiving an explicit tenant boundary.
    """
    requested = (x_lynewright_tenant_id or str(user.get("active_tenant_id") or "")).strip()
    tenant_id = requested or DEFAULT_TENANT_ID
    if user.get("synthetic"):
        if not tenant_exists(tenant_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
        return {
            "tenant_id": tenant_id,
            "user_id": 0,
            "role": "owner",
            "synthetic": True,
        }

    membership = get_membership(tenant_id=tenant_id, user_id=int(user["id"]))
    if membership is None:
        # Return 404 instead of 403 so callers cannot enumerate other tenants.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    return {
        "tenant_id": tenant_id,
        "user_id": int(user["id"]),
        "role": str(membership["role"]),
        "tenant_name": str(membership.get("tenant_name") or ""),
        "tenant_slug": str(membership.get("tenant_slug") or ""),
        "synthetic": False,
    }


def require_tenant_role(minimum: str):
    def _guard(context: dict[str, Any] = Depends(require_tenant_context)) -> dict[str, Any]:
        if not tenant_role_at_least(str(context.get("role", "")), minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This tenant action requires the '{minimum}' role or higher.",
            )
        return context

    return _guard


def require_project_context(
    request: Request,
    x_lynewright_project_id: str | None = Header(default=None, alias="X-Lynewright-Project-ID"),
    context: dict[str, Any] = Depends(require_tenant_context),
) -> dict[str, Any]:
    """Resolve an active project inside the already-authorized tenant."""
    requested = (
        x_lynewright_project_id
        or request.query_params.get("project_id")
        or ""
    ).strip()
    if not requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is required.",
        )
    project_id = requested
    try:
        project = require_project_in_tenant(
            tenant_id=str(context["tenant_id"]), project_id=project_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.") from exc
    return {**context, "project_id": project_id, "project": project}


def require_project_role(minimum: str):
    """Require a tenant role and return tenant+project authorization context."""
    def _guard(context: dict[str, Any] = Depends(require_project_context)) -> dict[str, Any]:
        if not tenant_role_at_least(str(context.get("role", "")), minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This project action requires the '{minimum}' role or higher.",
            )
        return context

    return _guard


def require_admin_token(
    user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Require the dashboard's administrator session or configured API token.

    Tenant and project boundaries are enforced by the scoped router dependencies.
    """
    return user
