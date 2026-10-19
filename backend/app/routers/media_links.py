"""Public bearer endpoint for revocable managed-media links."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from starlette.responses import StreamingResponse

from app.services.media_links import MediaLinksService
from app.services.platform_services import PlatformNotFoundError, PlatformValidationError, PlatformStore
from app.services.client_address import client_address


router = APIRouter(tags=["media-links"])
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Media link not found.")


def _rate_limit(token_hash: str, client_ip: str) -> bool:
    from app.services.rate_limiter import RateLimiterUnavailable, get_rate_limiter

    try:
        limiter = get_rate_limiter()
        token_ok = limiter.consume(namespace="media_link_token", key=token_hash, maximum=120, window_seconds=60)
        ip_ok = limiter.consume(namespace="media_link_ip", key=client_ip, maximum=600, window_seconds=60)
    except RateLimiterUnavailable as exc:
        raise HTTPException(status_code=503, detail="Media link service is temporarily unavailable.") from exc
    return token_ok and ip_ok


def _range_bounds(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    match = _RANGE.fullmatch(value.strip())
    if match is None or "," in value:
        raise ValueError("unsupported range")
    first, last = match.groups()
    if not first and not last:
        raise ValueError("empty range")
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        start, end = max(0, size - suffix), size - 1
    if start >= size or end < start:
        raise ValueError("unsatisfiable range")
    return start, end


def _content_disposition(name: str, content_type: str) -> str:
    safe_name = Path(str(name or "download")).name.replace("\r", "").replace("\n", "")[:200] or "download"
    inline = content_type.startswith(("image/", "audio/", "video/")) or content_type == "application/pdf"
    return f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(safe_name, safe='')}"


def _serve(token: str, request: Request, *, head: bool, tenant_id: str) -> Response:
    from app.services.integration_media_transfer import normalize_media_content_type

    service = MediaLinksService(PlatformStore(), tenant_id=tenant_id)
    digest = service.token_hash(token)
    client_ip = client_address(request)
    try:
        allowed = _rate_limit(digest, client_ip)
    except HTTPException:
        service._access_record(digest, None, "rate_limiter_unavailable")
        raise
    if not allowed:
        service._access_record(digest, None, "rate_limited")
        raise HTTPException(status_code=429, detail="Media link rate limit exceeded.")
    try:
        link = service.lookup(token)
    except PlatformNotFoundError:
        raise _not_found() from None
    try:
        requested = _range_bounds(request.headers.get("range"), int(link["size"]))
    except ValueError:
        headers = {
            "Content-Range": f"bytes */{int(link['size'])}",
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        raise HTTPException(status_code=416, detail="Range not satisfiable.", headers=headers) from None
    size = int(link["size"])
    status = 206 if requested else 200
    start, end = requested if requested else (0, size - 1)
    length = max(0, end - start + 1)
    media_type = normalize_media_content_type(link.get("content_type"))
    headers = {
        "Content-Type": media_type,
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": _content_disposition(str(link.get("name") or "download"), media_type),
    }
    if requested:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if head:
        return Response(status_code=status, headers=headers)

    def chunks():
        with open(link["path"], "rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(chunks(), status_code=status, headers=headers)


@router.get("/api/media/l/{token}")
def get_media_link(
    token: str, request: Request, tenant_id: str = Query(default="", max_length=256),
) -> Response:
    if not tenant_id:
        raise _not_found()
    try:
        return _serve(token, request, head=False, tenant_id=tenant_id)
    except PlatformValidationError:
        raise _not_found() from None


@router.head("/api/media/l/{token}")
def head_media_link(
    token: str, request: Request, tenant_id: str = Query(default="", max_length=256),
) -> Response:
    if not tenant_id:
        raise _not_found()
    try:
        return _serve(token, request, head=True, tenant_id=tenant_id)
    except PlatformValidationError:
        raise _not_found() from None


__all__ = ["get_media_link", "head_media_link", "router"]
