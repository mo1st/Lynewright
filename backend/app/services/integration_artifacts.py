"""Provider-neutral artifact materialisation for integration results.

Integrations may return an artifact URL after an asynchronous operation.  The
host turns that explicit artifact into durable managed media before a channel
ever tries to deliver it.  The core does not know whether the artifact is a
video, image, or document; the integration supplies only the URL and media
metadata while the host owns storage, bounds, and tenant/project scope.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import Any, Callable

from app.services.platform_services import MediaService, PlatformStore


MAX_ARTIFACT_BYTES = 250_000_000


def _managed_media_root() -> Path:
    return Path(os.getenv("LYNEWRIGHT_MANAGED_MEDIA_DIR", "/app/backend/data/media")).resolve()


def materialize_artifact_bytes(
    *,
    tenant_id: str,
    project_id: str | None,
    origin_url: str,
    body: bytes,
    name: str,
    content_type: str,
    integration_id: str = "",
    max_bytes: int = MAX_ARTIFACT_BYTES,
    store: PlatformStore | None = None,
) -> dict[str, Any]:
    """Persist explicitly returned artifact bytes as a managed media row.

    Repeated status polls are idempotent by origin URL within the same tenant
    and project.  The bytes are written atomically under the configured media
    root; an incomplete transfer can never become a platform media record.
    """

    if not origin_url or not isinstance(body, (bytes, bytearray)):
        raise ValueError("An artifact origin and byte body are required.")
    if not 1 <= int(max_bytes) <= MAX_ARTIFACT_BYTES:
        raise ValueError("Artifact size bound is invalid.")
    raw = bytes(body)
    if len(raw) > int(max_bytes):
        raise ValueError("Artifact exceeds the configured size bound.")
    media = MediaService(store or PlatformStore(), tenant_id=tenant_id)
    for existing in media.list(project_id=project_id):
        metadata = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
        if str(metadata.get("origin_url") or "") == origin_url:
            return existing

    root = _managed_media_root()
    root.mkdir(parents=True, exist_ok=True)
    stored_as = f"media-{secrets.token_hex(12)}.stored"
    staging = root / f".{stored_as}.part"
    final = root / stored_as
    digest = hashlib.sha256(raw).hexdigest()
    try:
        with staging.open("wb") as handle:
            handle.write(raw)
        staging.replace(final)
        metadata = {
            "transfer": "integration_artifact",
            "mode": "bounded_memory",
            "bytes": len(raw),
            "size_bytes": len(raw),
            "sha256": digest,
            "stored_as": stored_as,
            "storage_kind": "managed_file",
            "origin_url": origin_url,
            "integration_id": str(integration_id or ""),
        }
        try:
            return media.create_managed(
                name=str(name or "artifact")[:255],
                content_type=str(content_type or "application/octet-stream")[:120],
                metadata=metadata,
                project_id=project_id,
            )
        except Exception:
            final.unlink(missing_ok=True)
            raise
    except Exception:
        staging.unlink(missing_ok=True)
        raise


def extract_media_refs(value: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    """Extract only explicit media/artifact references from a result document.

    This is deliberately schema-light: future integrations can return
    ``media_refs``, ``artifacts`` or ``artifact_urls`` without a new channel
    adapter.  Arbitrary URLs are ignored so a normal API response cannot turn
    into an unsolicited Telegram attachment.
    """

    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    explicit_keys = {"media_refs", "media", "artifacts", "artifact_urls", "artifact_url", "media_url", "artifact_media_url"}

    def add(candidate: Any, inherited: dict[str, Any] | None = None) -> None:
        if len(refs) >= limit:
            return
        base = dict(inherited or {})
        if isinstance(candidate, str):
            base.setdefault("url", candidate)
        elif isinstance(candidate, dict):
            base.update(candidate)
        else:
            return
        media_id = str(base.get("media_id") or base.get("artifact_media_id") or "").strip()
        url = str(base.get("url") or base.get("media_url") or base.get("artifact_media_url") or "").strip()
        if not media_id and url.startswith("managed://"):
            media_id = url.removeprefix("managed://").split("?", 1)[0].strip()
        if not media_id and not url:
            return
        if url and not (url.startswith("managed://") or url.startswith("https://")) and not media_id:
            return
        key = (media_id, url)
        if key in seen:
            return
        seen.add(key)
        ref: dict[str, Any] = {}
        if media_id:
            ref["media_id"] = media_id
        if url:
            ref["url"] = url
        for key in ("content_type", "name", "caption"):
            if base.get(key):
                ref[key] = str(base[key])
        refs.append(ref)

    def walk(node: Any, inherited: dict[str, Any] | None = None, depth: int = 0) -> None:
        if len(refs) >= limit or depth > 5:
            return
        if isinstance(node, dict):
            for key in explicit_keys:
                if key not in node:
                    continue
                item = node[key]
                if isinstance(item, list):
                    for value_item in item:
                        add(value_item, {k: node.get(k) for k in ("content_type", "name", "caption") if node.get(k)})
                else:
                    add(item, {k: node.get(k) for k in ("content_type", "name", "caption") if node.get(k)})
            if node.get("media_id") or node.get("artifact_media_id"):
                add(node)
            for key, child in node.items():
                if key in explicit_keys or key in {"media_id", "artifact_media_id"}:
                    continue
                if isinstance(child, (dict, list)):
                    walk(child, inherited, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, inherited, depth + 1)

    walk(value)
    return refs


__all__ = ["MAX_ARTIFACT_BYTES", "extract_media_refs", "materialize_artifact_bytes"]
