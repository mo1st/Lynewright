"""Turn explicit managed-media refs into short-lived review links."""
from __future__ import annotations

from typing import Any

from app.services.integration_artifacts import extract_media_refs
from app.services.media_links import DEFAULT_TTL_SECONDS, MediaLinksService
from app.services.platform_services import MediaService, PlatformNotFoundError, PlatformStore


_PREVIEW_TYPES = ("image/", "audio/", "video/")


def review_media_descriptors(
    value: Any,
    *,
    tenant_id: str,
    project_id: str,
    actor: Any = None,
    run_id: str = "",
    store: PlatformStore | None = None,
) -> list[dict[str, Any]]:
    """Issue a 15-minute token only for media owned by this tenant/project."""
    selected_store = store or PlatformStore()
    media_service = MediaService(selected_store, tenant_id=tenant_id)
    links = MediaLinksService(selected_store, tenant_id=tenant_id)
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in extract_media_refs(value):
        media_id = str(ref.get("media_id") or "").strip()
        if not media_id or media_id in seen:
            continue
        seen.add(media_id)
        try:
            media = media_service.get(media_id)
        except PlatformNotFoundError:
            continue
        media_project = str(media.get("project_id") or "")
        if media_project != str(project_id or ""):
            continue
        content_type = str(ref.get("content_type") or media.get("content_type") or "application/octet-stream").split(";", 1)[0].lower()
        preview = content_type.startswith(_PREVIEW_TYPES) or content_type == "application/pdf"
        token = links.issue(
            media_id, ttl_seconds=DEFAULT_TTL_SECONDS, purpose="review-preview" if preview else "review-download",
            actor=actor, project_id=project_id, run_id=run_id,
        )
        output.append({
            "media_id": media_id,
            "name": str(ref.get("name") or media.get("name") or "download"),
            "content_type": content_type,
            "kind": "preview" if preview else "download",
            "url": links.public_url(token, tenant_id=tenant_id),
        })
    return output


def attach_review_media(
    items: list[Any],
    *,
    tenant_id: str,
    project_id: str,
    actor: Any = None,
    run_id: str = "",
    store: PlatformStore | None = None,
) -> list[Any]:
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = dict(item)
        value["media_previews"] = review_media_descriptors(
            item, tenant_id=tenant_id, project_id=project_id, actor=actor,
            run_id=run_id, store=store,
        )
        result.append(value)
    return result


__all__ = ["attach_review_media", "review_media_descriptors"]
