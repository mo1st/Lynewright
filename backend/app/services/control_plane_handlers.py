"""Built-in, scope-aware handlers exposed through Control Plane action tokens."""
from __future__ import annotations

from typing import Any, Callable

from app.services.control_plane_actions import ControlPlaneActionDispatcher
from app.services.integration_service_contracts import IntegrationServiceContractRegistry
from app.services.platform_services import PlatformConflictError, PlatformValidationError
from app.services.resource_bindings import ResourceBindingService
from app.services.resource_lifecycle_host import get_resource_lifecycle_coordinator
from app.services.social_publisher_contract import SOCIAL_PUBLISHER_CONTRACT


RESOURCE_RECONCILE_CAPABILITY = "resource.lifecycle.reconcile"
_DEFAULT_HANDLER_REGISTRARS: list[Callable[[str, ControlPlaneActionDispatcher], None]] = []
ANALYTICS_REFRESH_CAPABILITY = "analytics.web.refresh"
SOCIAL_PUBLISH_CREATE_CAPABILITY = "social.publish.create"
SOCIAL_PUBLISH_SCHEDULE_CAPABILITY = "social.publish.schedule"
MEDIA_VIDEO_RENDER_CAPABILITY = "media.video.render"
MEDIA_VIDEO_STATUS_CAPABILITY = "media.video.status"


def register_video_render_handlers(
    *,
    dispatcher: ControlPlaneActionDispatcher,
    provider: Any,
    integration_id: str,
) -> None:
    """Expose a provider implementing the host video-render interface."""
    if not callable(getattr(provider, "create_informational_short", None)) or not callable(getattr(provider, "get_generation_status", None)):
        raise PlatformValidationError("Video render handlers require a compatible provider.")
    existing = getattr(dispatcher, "_video_render_provider", None)
    if existing is not None:
        if existing is not provider:
            raise PlatformConflictError("Video render handlers are already bound to another provider.")
        return

    def assert_scope(claims: dict[str, Any]) -> None:
        if str(claims.get("integration_id") or "") != integration_id:
            raise PlatformConflictError("Video actions require the explicitly selected provider.")
        if not str(claims.get("resource_id") or ""):
            raise PlatformValidationError("Video actions require a resource scope.")

    def render(claims: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        assert_scope(claims)
        allowed = {key: payload[key] for key in ("subject", "script", "terms", "voice_name", "material", "subtitle_enabled", "clip_duration", "video_aspect", "video_source") if key in payload}
        return dict(provider.create_informational_short(**allowed))

    def status(claims: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        assert_scope(claims)
        generation_id = str(payload.get("generation_id") or "")
        if not generation_id:
            raise PlatformValidationError("Video status requires a generation id.")
        return dict(provider.get_generation_status(generation_id))

    dispatcher.register(capability_id=MEDIA_VIDEO_RENDER_CAPABILITY, action="render", handler=render)
    dispatcher.register(capability_id=MEDIA_VIDEO_STATUS_CAPABILITY, action="status", handler=status)
    dispatcher._video_render_provider = provider
    dispatcher._video_render_handlers = {"render": render, "status": status}
    dispatcher._video_render_integration_id = integration_id


def unregister_video_render_handlers(
    *, dispatcher: ControlPlaneActionDispatcher, provider: Any, integration_id: str,
) -> bool:
    """Dispose one provider-owned video bridge without touching Core handlers."""
    if (
        getattr(dispatcher, "_video_render_provider", None) is not provider
        or str(getattr(dispatcher, "_video_render_integration_id", "")) != integration_id
    ):
        return False
    handlers = getattr(dispatcher, "_video_render_handlers", {})
    dispatcher.unregister(capability_id=MEDIA_VIDEO_RENDER_CAPABILITY, action="render", handler=handlers["render"])
    dispatcher.unregister(capability_id=MEDIA_VIDEO_STATUS_CAPABILITY, action="status", handler=handlers["status"])
    dispatcher._video_render_provider = None
    dispatcher._video_render_handlers = {}
    dispatcher._video_render_integration_id = None
    return True


def register_social_publishing_handlers(
    *,
    dispatcher: ControlPlaneActionDispatcher,
    services: IntegrationServiceContractRegistry,
    require_verified: bool = True,
) -> None:
    """Bridge provider-neutral publish actions to the selected service contract.

    Provider adapters register a single ``lynewright.social.publisher`` service
    contribution; Core owns the action-token boundary.  The token's provider
    claim is checked against the durable provider selection before invocation,
    so a missing or unavailable selection cannot silently fall back to another
    publisher.  Registration is idempotent for one service-registry instance,
    which keeps adapter reloads from creating duplicate handlers.
    """
    if not isinstance(services, IntegrationServiceContractRegistry):
        raise PlatformValidationError("Social publishing handlers require a service contract registry.")
    existing = getattr(dispatcher, "_social_publishing_services", None)
    if existing is not None:
        if existing.contributions is not services.contributions:
            raise PlatformConflictError("Social publishing handlers are already bound to another service registry.")
        return
    def invoke_selected(claims: dict[str, Any], payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
        claimed_provider = str(claims.get("integration_id") or "")
        if not claimed_provider:
            raise PlatformValidationError("Publishing actions require an explicit provider scope.")
        project_id = str(claims.get("project_id") or "") or None
        provider = services.resolve(SOCIAL_PUBLISHER_CONTRACT, project_id=project_id)
        if provider.integration_id != claimed_provider:
            raise PlatformConflictError("Publishing requires the explicitly selected provider.")
        claimed_connection = str(claims.get("connection_id") or "") or None
        if provider.connection_id and provider.connection_id != claimed_connection:
            raise PlatformConflictError("Publishing action is outside the selected provider connection.")
        return services.invoke(
            SOCIAL_PUBLISHER_CONTRACT,
            operation,
            payload,
            project_id=project_id,
            require_verified=require_verified,
        )

    create = lambda claims, payload: invoke_selected(claims, payload, operation="posts.create")
    schedule = lambda claims, payload: invoke_selected(claims, payload, operation="posts.schedule")
    dispatcher.register(
        capability_id=SOCIAL_PUBLISH_CREATE_CAPABILITY,
        action="create",
        handler=create,
    )
    dispatcher.register(
        capability_id=SOCIAL_PUBLISH_SCHEDULE_CAPABILITY,
        action="schedule",
        handler=schedule,
    )
    dispatcher._social_publishing_services = services
    dispatcher._social_publishing_handlers = {"create": create, "schedule": schedule}


def unregister_social_publishing_handlers(
    *, dispatcher: ControlPlaneActionDispatcher, services: IntegrationServiceContractRegistry,
) -> bool:
    """Dispose one provider-neutral social bridge without touching Core handlers."""
    existing = getattr(dispatcher, "_social_publishing_services", None)
    if existing is None or existing.contributions is not services.contributions:
        return False
    handlers = getattr(dispatcher, "_social_publishing_handlers", {})
    dispatcher.unregister(capability_id=SOCIAL_PUBLISH_CREATE_CAPABILITY, action="create", handler=handlers["create"])
    dispatcher.unregister(capability_id=SOCIAL_PUBLISH_SCHEDULE_CAPABILITY, action="schedule", handler=handlers["schedule"])
    dispatcher._social_publishing_services = None
    dispatcher._social_publishing_handlers = {}
    return True


def register_default_control_plane_handler(
    registrar: Callable[[str, ControlPlaneActionDispatcher], None],
) -> Callable[[], bool]:
    """Contribute an optional handler set; no product handlers are built in."""
    if not callable(registrar):
        raise PlatformValidationError("Control Plane handler registrar must be callable.")
    _DEFAULT_HANDLER_REGISTRARS.append(registrar)

    def unregister() -> bool:
        if registrar not in _DEFAULT_HANDLER_REGISTRARS:
            return False
        _DEFAULT_HANDLER_REGISTRARS.remove(registrar)
        return True

    return unregister


def register_default_control_plane_handlers(*, tenant_id: str, dispatcher: ControlPlaneActionDispatcher) -> None:
    if not tenant_id:
        raise PlatformValidationError("Control Plane handlers require a tenant id.")
    from app.services.resource_lifecycle_host import run_resource_lifecycle_tick

    def reconcile(_claims: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        raw_limit = payload.get("max_items", 10)
        try:
            max_items = max(1, min(int(raw_limit), 100))
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("Resource reconciliation max_items must be an integer.") from exc
        raw_tick = payload.get("tick")
        try:
            tick = int(raw_tick) if raw_tick is not None else None
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("Resource reconciliation tick must be an integer.") from exc
        return run_resource_lifecycle_tick(tenant_id, max_items=max_items, tick=tick)

    dispatcher.register(
        capability_id=RESOURCE_RECONCILE_CAPABILITY,
        action="reconcile",
        handler=reconcile,
    )
    for registrar in tuple(_DEFAULT_HANDLER_REGISTRARS):
        registrar(tenant_id, dispatcher)


__all__ = [
    "ANALYTICS_REFRESH_CAPABILITY",
    "RESOURCE_RECONCILE_CAPABILITY",
    "SOCIAL_PUBLISH_CREATE_CAPABILITY",
    "SOCIAL_PUBLISH_SCHEDULE_CAPABILITY",
    "MEDIA_VIDEO_RENDER_CAPABILITY",
    "MEDIA_VIDEO_STATUS_CAPABILITY",
    "register_default_control_plane_handler",
    "register_default_control_plane_handlers",
    "register_video_render_handlers",
    "unregister_video_render_handlers",
    "register_social_publishing_handlers",
    "unregister_social_publishing_handlers",
]
