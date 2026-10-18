"""Bearer-authenticated HTTP protocol for isolated integration runners."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.services import integration_host
from app.services.integration_runner import IntegrationRunnerService
from app.services.integration_worker_invocations import IntegrationWorkerInvocationStore
from app.services.platform_services import (
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformServiceError,
    PlatformStore,
    PlatformValidationError,
)


router = APIRouter(prefix="/api/runner/v1", tags=["integration-runners"])


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimRequest(_Request):
    wait_seconds: float = Field(default=15, ge=0, le=30)


class CompleteRequest(_Request):
    claim_token: str = Field(min_length=20, max_length=200)
    result: Any


class FailRequest(_Request):
    claim_token: str = Field(min_length=20, max_length=200)
    error_code: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


def _service(identity: dict[str, str] | None = None) -> IntegrationRunnerService:
    return IntegrationRunnerService(
        integration_host.store,
        tenant_id=str((identity or {}).get("tenant_id") or "ten_default"),
    )


def _identity(authorization: str | None = Header(default=None)) -> dict[str, str]:
    scheme, _, value = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(status_code=401, detail="Runner bearer token required.")
    identity = _service().authenticate(value.strip())
    if identity is None:
        raise HTTPException(status_code=401, detail="Runner token is invalid or revoked.")
    return identity


def _raise(exc: Exception) -> None:
    if isinstance(exc, PlatformNotFoundError):
        raise HTTPException(status_code=404, detail="Runner resource was not found.") from exc
    if isinstance(exc, PlatformValidationError):
        raise HTTPException(status_code=422, detail=exc.message) from exc
    if isinstance(exc, PlatformOperationError):
        message = exc.message
        status = 403 if "scope" in message.lower() or "claimed by this token" in message.lower() else 409
        raise HTTPException(status_code=status, detail=message) from exc
    if isinstance(exc, PlatformServiceError):
        raise HTTPException(status_code=409, detail=exc.message) from exc
    raise exc


@router.post("/claim")
def claim(payload: ClaimRequest, identity: dict[str, str] = Depends(_identity)) -> dict[str, Any]:
    try:
        invocation = _service(identity).claim(identity, wait_seconds=payload.wait_seconds)
        return {"invocation": invocation}
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.post("/{request_id}/complete")
def complete(
    request_id: str, payload: CompleteRequest, identity: dict[str, str] = Depends(_identity),
) -> dict[str, Any]:
    try:
        queue = IntegrationWorkerInvocationStore(
            integration_host.store, tenant_id=identity["tenant_id"],
        )
        result = queue.complete_for_runner(
            request_id, integration_id=identity["integration_id"],
            runner_id=identity["runner_id"], claim_token=payload.claim_token,
            result=payload.result,
        )
        if result["status"] != "succeeded":
            raise HTTPException(status_code=403, detail="Runner claim token is invalid or expired.")
        return {"request_id": request_id, "status": result["status"]}
    except HTTPException:
        raise
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.post("/{request_id}/fail")
def fail(
    request_id: str, payload: FailRequest, identity: dict[str, str] = Depends(_identity),
) -> dict[str, Any]:
    try:
        queue = IntegrationWorkerInvocationStore(
            integration_host.store, tenant_id=identity["tenant_id"],
        )
        result = queue.fail_for_runner(
            request_id, integration_id=identity["integration_id"],
            runner_id=identity["runner_id"], claim_token=payload.claim_token,
            error=f"Runner operation failed ({payload.error_code}).",
        )
        if result["status"] != "failed":
            raise HTTPException(status_code=403, detail="Runner claim token is invalid or expired.")
        return {"request_id": request_id, "status": result["status"]}
    except HTTPException:
        raise
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.post("/heartbeat")
def heartbeat(identity: dict[str, str] = Depends(_identity)) -> dict[str, Any]:
    try:
        return _service(identity).heartbeat(identity)
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.get("/invocations/{request_id}/media/{media_id}/link")
def get_media_link(
    request_id: str, media_id: str, ttl_seconds: int = 900,
    identity: dict[str, str] = Depends(_identity),
) -> dict[str, Any]:
    try:
        return _service(identity).media_link(
            identity, request_id=request_id, media_id=media_id, ttl_seconds=ttl_seconds,
        )
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


@router.post("/invocations/{request_id}/artifacts")
async def upload_artifact(
    request_id: str, request: Request, identity: dict[str, str] = Depends(_identity),
) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 250_000_000:
        raise HTTPException(status_code=413, detail="Runner artifact exceeds the 250 MB limit.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 250_000_000:
            raise HTTPException(status_code=413, detail="Runner artifact exceeds the 250 MB limit.")
        body.extend(chunk)
    name = request.headers.get("x-filename", "artifact")[:255]
    if not re.fullmatch(r"[A-Za-z0-9._ -]{1,255}", name):
        raise HTTPException(status_code=422, detail="Artifact filename contains unsupported characters.")
    content_type = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0][:120]
    try:
        return _service(identity).upload_artifact(
            identity, request_id=request_id, body=bytes(body), name=name, content_type=content_type,
        )
    except Exception as exc:
        _raise(exc)
    raise AssertionError("unreachable")


__all__ = ["router"]
