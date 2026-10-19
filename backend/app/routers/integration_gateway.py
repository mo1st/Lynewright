"""Public inbound routes that dispatch only to registered integration handlers."""
from __future__ import annotations

from collections.abc import Callable, Iterable

from fastapi import APIRouter, HTTPException, Request

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services import integration_host
from app.services.integration_lifecycle import IntegrationLifecycleRuntime, IntegrationRuntimeDefinition
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformNotFoundError, PlatformOperationError, PlatformServiceError, PlatformStore, PlatformValidationError


router = APIRouter(prefix="/api/integrations", tags=["integrations"])
# Composed in the service layer. The router is one way into the system, not
# the place the system is assembled; these names are re-exported so existing
# importers keep working.
_store = integration_host.store
_integrations = integration_host.integrations
_connections = integration_host.connections
_contributions = integration_host.contributions
runtime = integration_host.runtime
gateway = integration_host.gateway


def register_runtime_definition(definition: IntegrationRuntimeDefinition) -> None:
    """Adapter bootstrap entrypoint; definitions remain executable code, not DB rows."""
    runtime.register_definition(definition)


def bootstrap_runtime_definitions(
    factories: Iterable[Callable[[], IntegrationRuntimeDefinition]] = (),
) -> dict[str, list[str]]:
    """Build and register injected definitions before persisted recovery.

    Composition roots pass factories after constructing their approved
    transports. One malformed or unavailable factory is isolated so another
    integration can still recover; provider code never gets a database or
    secret boundary from this hook.
    """
    registered: list[str] = []
    failed: list[str] = []
    ordered = sorted(
        tuple(factories),
        key=lambda item: f"{getattr(item, '__module__', '')}:{getattr(item, '__qualname__', repr(item))}",
    )
    for factory in ordered:
        if not callable(factory):
            failed.append("<non-callable>")
            continue
        label = f"{getattr(factory, '__module__', '')}:{getattr(factory, '__qualname__', repr(factory))}"
        try:
            definition = factory()
            if not isinstance(definition, IntegrationRuntimeDefinition):
                raise PlatformValidationError("Runtime definition factory returned an incompatible value.")
            register_runtime_definition(definition)
            registered.append(definition.integration_id)
        except Exception:
            failed.append(label)
    return {"registered": registered, "failed": failed}


def recover_enabled_integrations() -> dict[str, list[str]]:
    """Restore persisted enabled integrations after all available adapters register."""
    return runtime.recover_enabled()


def _error(exc: PlatformServiceError) -> HTTPException:
    if isinstance(exc, PlatformNotFoundError):
        return HTTPException(status_code=404, detail="Integration handler not found.")
    if isinstance(exc, PlatformOperationError) and "signature" in exc.message.lower():
        return HTTPException(status_code=401, detail="Inbound integration verification failed.")
    return HTTPException(status_code=400, detail="Inbound integration request was rejected.")


async def _receive(request: Request, kind: str, integration_id: str, connection_id: str, handler_id: str) -> dict:
    try:
        content_length = int(request.headers.get("content-length") or "0")
    except ValueError:
        raise HTTPException(status_code=400, detail="Inbound integration request was rejected.")
    if content_length > 1_000_000:
        raise HTTPException(status_code=413, detail="Inbound integration payload is too large.")
    try:
        return gateway.receive(
            kind, integration_id=integration_id, connection_id=connection_id, handler_id=handler_id,
            body=await request.body(), headers=dict(request.headers),
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/{connection_id}/webhooks/{handler_id}", status_code=202)
async def receive_webhook(request: Request, integration_id: str, connection_id: str, handler_id: str) -> dict:
    return await _receive(request, "webhook", integration_id, connection_id, handler_id)


@router.post("/{integration_id}/{connection_id}/callbacks/{handler_id}", status_code=202)
async def receive_callback(request: Request, integration_id: str, connection_id: str, handler_id: str) -> dict:
    return await _receive(request, "callback", integration_id, connection_id, handler_id)
