"""API models for the interaction/notification gateway."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

InteractionIntent = Literal["IMMEDIATE", "DIGEST", "SILENT"]
InteractionChannelType = Literal["telegram"]


class InteractionConnectionCreate(BaseModel):
    channel_type: InteractionChannelType = "telegram"
    project_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    bot_token: str = Field(default="", max_length=200)
    operator_profile_id: str = Field(default="", max_length=100)


class InteractionConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    bot_token: str | None = Field(default=None, max_length=200)
    # Empty clears the selection and restores automatic profile choice.
    operator_profile_id: str | None = Field(default=None, max_length=100)


class InteractionConnectionRead(BaseModel):
    id: str
    tenant_id: str
    project_id: str
    channel_type: str
    name: str
    external_identity: str = ""
    operator_profile_id: str = ""
    enabled: bool = True
    verified_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    delivery_mode: str = "polling"
    webhook_url: str = ""
    last_update_at: str | None = None
    last_error: str = ""


class InteractionConnectionCreated(InteractionConnectionRead):
    webhook_secret: str = ""
    bot_username: str = ""


class LinkTokenCreate(BaseModel):
    connection_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    ttl_seconds: int | None = Field(default=None, ge=30, le=86_400)


class LinkTokenRead(BaseModel):
    id: str
    token: str
    connection_id: str
    project_id: str
    expires_at_epoch: float
    ttl_seconds: int
    # https://t.me/<bot>?start=<token>: one tap links the chat.
    deep_link: str = ""


class LinkTokenStatus(BaseModel):
    id: str
    linked: bool
    expired: bool
    external_username: str = ""


class DeliveryModeUpdate(BaseModel):
    mode: str = Field(pattern="^(polling|webhook)$")


class TelegramSettingsRead(BaseModel):
    enabled: bool
    webhook_available: bool
    public_url: str


class InteractionLinkRead(BaseModel):
    id: str
    tenant_id: str
    project_id: str
    user_id: int
    connection_id: str
    channel_type: str
    external_user_id: str
    external_username: str = ""
    status: str
    created_at: str | None = None


class ConversationRead(BaseModel):
    id: str
    tenant_id: str
    project_id: str
    connection_id: str
    channel_type: str
    external_chat_id: str
    title: str = ""
    state: str = "active"
    active_run_id: str | None = None
    run_state: str | None = None
    last_inbound_at: str | None = None
    last_outbound_at: str | None = None
    created_at: str | None = None


class RunBindingUpdate(BaseModel):
    run_id: str | None = None
    run_state: str | None = None


class MessageRead(BaseModel):
    id: str
    conversation_id: str
    direction: str
    actor: str
    message_type: str
    text: str = ""
    media: list[dict[str, Any]] = Field(default_factory=list)
    action_id: str | None = None
    external_message_id: str = ""
    status: str = ""
    detail: str = ""
    created_at: str | None = None


class ActionRead(BaseModel):
    id: str
    tenant_id: str
    project_id: str
    conversation_id: str
    run_id: str | None = None
    action_type: str
    decision_options: list[str] = Field(default_factory=list)
    payload_ref: str = ""
    summary: str = ""
    status: str
    resolved_by: str = ""
    resolved_via: str = ""
    resolution_note: str = ""
    created_at: str | None = None
    resolved_at: str | None = None


class ActionDecisionRequest(BaseModel):
    decision: Literal["approve", "reject", "request_changes"]
    note: str = Field(default="", max_length=500)
    resolved_by: str = Field(default="dashboard", max_length=120)


class RequestRead(BaseModel):
    id: str
    conversation_id: str
    kind: str
    text: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str
    dispatched_to: str = ""
    response_message_id: str | None = None
    error: str = ""
    created_at: str | None = None


class ResponseCreate(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=8000)
    request_id: str | None = Field(default=None, max_length=100)
    actor: Literal["agent", "system", "operator"] = "system"
    media_refs: list[dict[str, Any]] = Field(default_factory=list)


class ResponseRead(BaseModel):
    id: str
    request_id: str | None = None
    conversation_id: str
    actor: str
    text: str = ""
    media: list[dict[str, Any]] = Field(default_factory=list)
    delivery_status: str
    detail: str = ""
    sent_external_message_id: str = ""
    created_at: str | None = None


class NotificationRouteChannel(BaseModel):
    channel: Literal["telegram", "email", "webhook"]
    target: str = Field(default="", max_length=2000)
    chat_ref: str = Field(default="", max_length=200)


class NotificationRouteUpsert(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    event_class: str = Field(min_length=1, max_length=100)
    intent: InteractionIntent
    channels: list[NotificationRouteChannel] = Field(min_length=1)
    enabled: bool = True


class NotificationRouteRead(BaseModel):
    id: str
    tenant_id: str
    project_id: str
    event_class: str
    intent: str
    channels: list[dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True
    updated_at: str | None = None


class NotificationTestRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    event_class: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(default="", max_length=4000)
    intent: InteractionIntent | None = None
    actor: Literal["agent", "system", "operator"] = "system"
    dedupe_key: str = Field(default="", max_length=200)


class WebhookSetupRead(BaseModel):
    url_hint: str
    secret_header: str
    enabled_channel: bool
