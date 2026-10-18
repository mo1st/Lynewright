"""Dashboard/API routes for the interaction & notification gateway."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.models.interaction import (
    ActionDecisionRequest,
    ActionRead,
    ConversationRead,
    InteractionConnectionCreated,
    InteractionConnectionCreate,
    InteractionConnectionRead,
    InteractionConnectionUpdate,
    InteractionLinkRead,
    LinkTokenCreate,
    LinkTokenRead,
    MessageRead,
    NotificationRouteRead,
    NotificationRouteUpsert,
    NotificationTestRequest,
    RequestRead,
    ResponseCreate,
    ResponseRead,
    RunBindingUpdate,
)
from app.security import require_tenant_context, require_tenant_role
from app.services import interaction_actions as actions_service
from app.services import interaction_conversations as conversations_service
from app.services import interaction_linking as linking_service
from app.services import interaction_requests as requests_service
from app.services import notification_gateway
from app.services.interaction_channels import default_registry

router = APIRouter(prefix="/api/interactions", tags=["interactions"])


def _map_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, actions_service.ActionAlreadyResolvedError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, actions_service.ActionError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------- connections

@router.get("/connections", response_model=list[InteractionConnectionRead])
def list_connections(
    project_id: str | None = Query(default=None),
    context: dict = Depends(require_tenant_context),
) -> list[dict]:
    return linking_service.list_connections(tenant_id=context["tenant_id"], project_id=project_id)


@router.post(
    "/connections",
    response_model=InteractionConnectionCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    payload: InteractionConnectionCreate,
    context: dict = Depends(require_tenant_role("editor")),
) -> dict:
    try:
        created = linking_service.create_connection(
            tenant_id=context["tenant_id"],
            project_id=payload.project_id,
            channel_type=payload.channel_type,
            name=payload.name,
            credentials={"bot_token": payload.bot_token} if payload.bot_token else None,
            operator_profile_id=payload.operator_profile_id,
        )
    except ValueError as exc:
        raise _map_errors(exc) from exc
    raw_webhook_secret = str(created.get("webhook_secret") or "")
    bot_username = ""
    if payload.bot_token:
        # Best-effort identity verification; retryable via /verify.
        row = linking_service.get_connection_row(
            tenant_id=context["tenant_id"], connection_id=created["id"],
        )
        if row is not None:
            registry = default_registry()
            adapter = registry.get(payload.channel_type)
            try:
                identity = await adapter.describe_bot(connection=row)  # type: ignore[attr-defined]
                bot_username = str(identity.get("username") or "")
                created = linking_service.verify_connection_identity(
                    tenant_id=context["tenant_id"],
                    connection_id=created["id"],
                    external_identity=f"@{bot_username}" if bot_username else "",
                )
            except Exception:
                bot_username = ""
    return {
        **created,
        "webhook_secret": raw_webhook_secret,
        "webhook_path": f"/api/interactions/telegram/{created['id']}/webhook",
        "bot_username": bot_username,
    }


@router.patch("/connections/{connection_id}", response_model=InteractionConnectionRead)
def update_connection(
    connection_id: str,
    payload: InteractionConnectionUpdate,
    context: dict = Depends(require_tenant_role("editor")),
) -> dict:
    try:
        return linking_service.update_connection(
            tenant_id=context["tenant_id"],
            connection_id=connection_id,
            name=payload.name,
            enabled=payload.enabled,
            bot_token=payload.bot_token,
            operator_profile_id=payload.operator_profile_id,
        )
    except (LookupError, ValueError) as exc:
        raise _map_errors(exc) from exc


@router.post("/connections/{connection_id}/rotate-webhook-secret")
def rotate_webhook_secret(
    connection_id: str,
    context: dict = Depends(require_tenant_role("editor")),
) -> dict:
    try:
        result = linking_service.rotate_webhook_secret(
            tenant_id=context["tenant_id"], connection_id=connection_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": result["id"], "webhook_secret": result["webhook_secret"]}


@router.post("/connections/{connection_id}/verify", response_model=InteractionConnectionRead)
async def verify_connection(
    connection_id: str,
    context: dict = Depends(require_tenant_role("editor")),
) -> dict:
    connection = linking_service.require_tenant_connection(
        tenant_id=context["tenant_id"], connection_id=connection_id,
    )
    registry = default_registry()
    adapter = registry.get(connection["channel_type"])
    try:
        identity = await adapter.describe_bot(connection=connection)  # type: ignore[attr-defined]
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Telegram could not verify this bot.") from exc
    username = identity.get("username", "")
    return linking_service.verify_connection_identity(
        tenant_id=context["tenant_id"],
        connection_id=connection_id,
        external_identity=f"@{username}" if username else "",
    )


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: str,
    context: dict = Depends(require_tenant_role("admin")),
) -> None:
    try:
        linking_service.delete_connection(
            tenant_id=context["tenant_id"], connection_id=connection_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------- links

@router.post("/links/token", response_model=LinkTokenRead, status_code=status.HTTP_201_CREATED)
def create_link_token(
    payload: LinkTokenCreate,
    context: dict = Depends(require_tenant_role("editor")),
) -> dict:
    """Issue a one-time link code for the authenticated user to send to the bot."""
    try:
        return linking_service.create_link_token(
            tenant_id=context["tenant_id"],
            project_id=payload.project_id,
            user_id=int(context["user_id"]),
            connection_id=payload.connection_id,
            ttl_seconds=payload.ttl_seconds,
        )
    except ValueError as exc:
        raise _map_errors(exc) from exc


@router.get("/links", response_model=list[InteractionLinkRead])
def list_links(
    connection_id: str | None = Query(default=None),
    context: dict = Depends(require_tenant_context),
) -> list[dict]:
    return linking_service.list_links(tenant_id=context["tenant_id"], connection_id=connection_id)


@router.delete("/links/{link_id}", response_model=InteractionLinkRead)
def revoke_link(link_id: str, context: dict = Depends(require_tenant_role("editor"))) -> dict:
    try:
        return linking_service.revoke_link(tenant_id=context["tenant_id"], link_id=link_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# -------------------------------------------------------------- conversations

@router.get("/conversations", response_model=list[ConversationRead])
def list_conversations(
    project_id: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    context: dict = Depends(require_tenant_context),
) -> list[dict]:
    return conversations_service.list_conversations(
        tenant_id=context["tenant_id"], project_id=project_id, include_archived=include_archived,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationRead)
def get_conversation(conversation_id: str, context: dict = Depends(require_tenant_context)) -> dict:
    conversation = conversations_service.get_conversation(
        tenant_id=context["tenant_id"], conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Interaction conversation not found.")
    return conversation


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
def list_messages(
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    context: dict = Depends(require_tenant_context),
) -> list[dict]:
    try:
        return conversations_service.list_messages(
            tenant_id=context["tenant_id"], conversation_id=conversation_id, limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/conversations/{conversation_id}/run-binding", response_model=ConversationRead)
def set_run_binding(
    conversation_id: str,
    payload: RunBindingUpdate,
    context: dict = Depends(require_tenant_role("editor")),
) -> dict:
    """Future Operator integration point: bind/replace a run on this thread."""
    try:
        return conversations_service.set_run_binding(
            tenant_id=context["tenant_id"], conversation_id=conversation_id,
            run_id=payload.run_id, run_state=payload.run_state,
        )
    except (LookupError, ValueError) as exc:
        raise _map_errors(exc) from exc


# ------------------------------------------------------------------- requests

@router.get("/requests", response_model=list[RequestRead])
def list_requests(
    request_status: str | None = Query(default=None, alias="status"),
    conversation_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    context: dict = Depends(require_tenant_context),
) -> list[dict]:
    try:
        return requests_service.list_requests(
            tenant_id=context["tenant_id"], status=request_status,
            conversation_id=conversation_id, limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ------------------------------------------------------------------ responses

@router.get("/responses", response_model=list[ResponseRead])
def list_responses(
    conversation_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    context: dict = Depends(require_tenant_context),
) -> list[dict]:
    try:
        return requests_service.list_responses(
            tenant_id=context["tenant_id"], conversation_id=conversation_id, limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/responses", response_model=ResponseRead, status_code=status.HTTP_202_ACCEPTED)
async def submit_response(
    payload: ResponseCreate,
    context: dict = Depends(require_tenant_role("editor")),
) -> dict:
    """Generic outbound response boundary (deterministic system today, agent later)."""
    try:
        conversation = conversations_service.require_tenant_conversation(
            tenant_id=context["tenant_id"], conversation_id=payload.conversation_id,
        )
        return await requests_service.submit_response(
            tenant_id=context["tenant_id"],
            project_id=str(conversation["project_id"]),
            conversation_id=payload.conversation_id,
            text=payload.text,
            actor=payload.actor,
            media_refs=payload.media_refs,
            request_id=payload.request_id,
        )
    except (LookupError, ValueError) as exc:
        raise _map_errors(exc) from exc


# -------------------------------------------------------------------- actions

@router.get("/actions", response_model=list[ActionRead])
def list_actions(
    conversation_id: str | None = Query(default=None),
    action_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    context: dict = Depends(require_tenant_context),
) -> list[dict]:
    try:
        return actions_service.list_actions(
            tenant_id=context["tenant_id"], conversation_id=conversation_id,
            status=action_status, limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/actions/{action_id}/resolve", response_model=ActionRead)
def resolve_action(
    action_id: str,
    payload: ActionDecisionRequest,
    context: dict = Depends(require_tenant_role("editor")),
) -> dict:
    """Business-system-side resolution path (the business system stays authoritative)."""
    try:
        return actions_service.resolve_action_via_api(
            tenant_id=context["tenant_id"], action_id=action_id, decision=payload.decision,
            resolved_by=payload.resolved_by or str(context.get("user_id") or "dashboard"),
            note=payload.note,
        )
    except (LookupError, actions_service.ActionError) as exc:
        raise _map_errors(exc) from exc


# ---------------------------------------------------------- notification routes

@router.get("/notification-routes", response_model=list[NotificationRouteRead])
def list_notification_routes(
    project_id: str | None = Query(default=None),
    context: dict = Depends(require_tenant_context),
) -> list[dict]:
    return [
        route | {"enabled": bool(route.get("enabled"))}
        for route in notification_gateway.list_routes(
            tenant_id=context["tenant_id"], project_id=project_id,
        )
    ]


@router.put("/notification-routes", response_model=NotificationRouteRead)
def upsert_notification_route(
    payload: NotificationRouteUpsert,
    context: dict = Depends(require_tenant_role("editor")),
) -> dict:
    try:
        route = notification_gateway.set_route(
            tenant_id=context["tenant_id"],
            project_id=payload.project_id,
            event_class=payload.event_class,
            intent=payload.intent,
            channels=[entry.model_dump() for entry in payload.channels],
            enabled=payload.enabled,
        )
    except notification_gateway.NotificationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return route | {"enabled": bool(route.get("enabled"))}


@router.delete("/notification-routes/{event_class}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification_route(
    event_class: str,
    project_id: str = Query(min_length=1, max_length=100),
    context: dict = Depends(require_tenant_role("editor")),
) -> None:
    notification_gateway.delete_route(
        tenant_id=context["tenant_id"], project_id=project_id, event_class=event_class,
    )


@router.post("/notifications/send")
async def send_notification(
    payload: NotificationTestRequest,
    context: dict = Depends(require_tenant_role("editor")),
) -> dict:
    """Deterministic Lynewright notifications — no AI is invoked on this path."""
    try:
        return await notification_gateway.send_notification(
            tenant_id=context["tenant_id"],
            project_id=payload.project_id,
            event_class=payload.event_class,
            title=payload.title,
            body=payload.body,
            intent=payload.intent,
            dedupe_key=payload.dedupe_key,
            actor=payload.actor,
        )
    except notification_gateway.NotificationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
