from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


AITaskType = Literal[
    "diagnosis", "tracking_builder", "ad_text", "ad_image", "code_builder",
    "experience_builder",
    "visitor_chat",
    "research_mining", "offer_builder", "results_copy",
    "email_copy", "product_recommendation", "site_product_mapping",
    # Internal Operator Agent orchestration route. Resolves through the same
    # per-task BYOK provider configuration as every other task; the Operator
    # runtime never builds a second provider framework.
    "operator_agent", "operator_judge",
]
CredentialMode = Literal["credential_ref", "env_var", "raw_key"]
AIProviderProtocol = Literal[
    "openai_chat", "openai_responses", "anthropic_messages", "gemini_generate",
    "vertex_generate", "codex", "gemini_cli",
]


class AIProviderTaskSettings(BaseModel):
    task_type: AITaskType
    provider: str = Field(min_length=1, max_length=80)
    protocol: AIProviderProtocol = "openai_chat"
    model: str = Field(default="", max_length=160)
    credential_mode: CredentialMode = "env_var"
    api_key_env_name: str = Field(default="", max_length=160)
    credential_ref: str = Field(default="", max_length=256)
    api_key_value_masked: str = ""
    base_url: str = Field(default="", max_length=500)
    fallback_provider: str = Field(default="", max_length=80)
    fallback_model: str = Field(default="", max_length=160)
    reasoning_effort: str = Field(default="", max_length=20)
    enabled: bool = True


class AISavedProvider(BaseModel):
    """A provider saved in AI & Providers that a model slot may point at. No key fields."""

    id: int
    name: str
    provider: str
    model: str = ""
    enabled: bool = True


class AIModelSlotTestDraft(BaseModel):
    """What the settings screen has selected for a slot, tested before saving."""

    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(default="", max_length=160)


class AIModelSlotSettings(BaseModel):
    """One model an active integration declared it needs, with its stored route."""

    slot_key: str
    integration_id: str
    integration_name: str
    slot_id: str
    kind: Literal["text", "image", "vision"]
    label: str
    description: str = ""
    # The saved provider's name when ``provider`` is ``profile:<id>`` and it exists.
    saved_provider_name: str = ""
    provider: str = Field(min_length=1, max_length=80)
    protocol: AIProviderProtocol = "openai_chat"
    model: str = Field(default="", max_length=160)
    credential_mode: CredentialMode = "env_var"
    api_key_env_name: str = Field(default="", max_length=160)
    credential_ref: str = Field(default="", max_length=256)
    api_key_value_masked: str = ""
    base_url: str = Field(default="", max_length=500)
    fallback_provider: str = Field(default="", max_length=80)
    fallback_model: str = Field(default="", max_length=160)
    reasoning_effort: str = Field(default="", max_length=20)
    enabled: bool = True


class AIProviderTaskUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    protocol: AIProviderProtocol = "openai_chat"
    model: str = Field(default="", max_length=160)
    credential_mode: CredentialMode = "env_var"
    api_key_env_name: str = Field(default="", max_length=160)
    credential_ref: str = Field(default="", max_length=256, pattern=r"^$|^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    api_key_value: str | None = Field(default=None, max_length=1000)
    base_url: str = Field(default="", max_length=500)
    enabled: bool = True
    fallback_provider: str = Field(default="", max_length=80)
    fallback_model: str = Field(default="", max_length=160)
    reasoning_effort: str = Field(default="", max_length=20, pattern=r"^$|^[a-z]{1,20}$")


class AIProviderSettingsResponse(BaseModel):
    providers: list[AIProviderTaskSettings]
    slots: list[AIModelSlotSettings] = Field(default_factory=list)
    saved_providers: list[AISavedProvider] = Field(default_factory=list)


class AIProviderTaskTestResponse(BaseModel):
    ok: bool
    provider: str
    protocol: AIProviderProtocol
    model: str
    models: list[dict] = Field(default_factory=list)
    error: str | None = None
    authenticated: bool | None = None
    plan: str | None = None
    quota_state: str | None = None
    quota_until: str | None = None
