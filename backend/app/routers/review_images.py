"""Authenticated issuer for short-lived review links to managed media."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.security import require_tenant_role
from app.services.platform_services import PlatformNotFoundError, PlatformStore
from app.services.review_media import review_media_descriptors
from app.services.tenancy import require_project_in_tenant


router = APIRouter(prefix="/api/review-media", tags=["review-media"])
_EDITOR = require_tenant_role("editor")


@router.get("/link/{media_id}")
def review_media_link(
    media_id: str,
    project_id: str = Query(default="", max_length=160),
    context: dict[str, Any] = Depends(_EDITOR),
) -> dict[str, Any]:
    tenant_id = str(context.get("tenant_id") or "")
    project = str(project_id or "")
    if project:
        try:
            require_project_in_tenant(tenant_id=tenant_id, project_id=project)
        except ValueError:
            raise HTTPException(status_code=404, detail="Media not found.") from None
    try:
        values = review_media_descriptors(
            {"media_id": media_id}, tenant_id=tenant_id, project_id=project,
            actor=context, store=PlatformStore(),
        )
    except (PlatformNotFoundError, ValueError):
        values = []
    if not values:
        raise HTTPException(status_code=404, detail="Media not found.")
    return {"media": values[0]}


__all__ = ["review_media_link", "router"]
