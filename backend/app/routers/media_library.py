"""Admin media library with explicit removal for managed integration artifacts."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.security import require_tenant_role
from app.services.platform_services import MediaService, PlatformNotFoundError, PlatformStore

router = APIRouter(prefix="/api/media", tags=["media"])
_ADMIN = require_tenant_role("admin")


class MediaDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_media_id: str = Field(min_length=1, max_length=160)


def _service(context: dict[str, Any]) -> MediaService:
    return MediaService(PlatformStore(), tenant_id=str(context["tenant_id"]))


@router.get("")
def list_media(context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    items = _service(context).list()
    safe = []
    for item in items:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        safe.append({
            "id": str(item["id"]), "name": str(item.get("name") or ""),
            "content_type": str(item.get("content_type") or ""),
            "project_id": str(item.get("project_id") or ""),
            "created_at": str(item.get("created_at") or ""),
            "bytes": int(metadata.get("size_bytes") or metadata.get("bytes") or 0),
            "integration_id": str(metadata.get("integration_id") or ""),
        })
    return {"items": safe}


@router.delete("/{media_id}")
def delete_media(
    media_id: str,
    payload: MediaDeleteRequest,
    context: dict[str, Any] = Depends(_ADMIN),
) -> dict[str, Any]:
    service = _service(context)
    if payload.confirm_media_id != media_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Type the media id to confirm removal.")
    try:
        record = service.get(media_id)
        result = service.delete(media_id, confirm_media_id=media_id)
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        stored_as = str(metadata.get("stored_as") or "")
        if stored_as:
            from app.services.integration_media_transfer import IntegrationMediaTransferService, managed_media_root
            IntegrationMediaTransferService(temp_dir=managed_media_root()).delete_managed_copy(stored_as)
        return result
    except PlatformNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


__all__ = ["router"]
