from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


AI_PROVIDERS = [
    "codex",
    "gemini_cli",
    "gemini",
    "vertex",
    "openai",
    "openai_compatible",
    "anthropic",
    "grok",
    "mistral",
    "deepseek",
]

AiProviderName = Literal[
    "mock",
    "codex",
    "gemini_cli",
    "gemini",
    "vertex",
    "openai",
    "openai_compatible",
    "anthropic",
    "grok",
    "mistral",
    "deepseek",
]


def mask_key(key: str) -> str:
    if not key or not isinstance(key, str):
        return ""
    key = key.strip()
    length = len(key)
    if length == 0:
        return ""
    if length <= 8:
        return "*" * max(4, length)
    mask_len = min(12, max(6, length - 8))
    return key[:4] + "*" * mask_len + key[-4:]


def sanitize_secret_input(submitted: str | None, existing: str) -> str:
    if not submitted or not submitted.strip():
        return existing
    candidate = submitted.strip()
    if existing and candidate == mask_key(existing):
        return existing
    return candidate


class AiSettingsPayload(BaseModel):
    provider: AiProviderName = "mock"
    api_keys: dict[str, str] = Field(
        default_factory=lambda: {p: "" for p in AI_PROVIDERS}
    )
    model: str = "mock-brief-v1"
    base_url: str = "https://api.openai.com/v1"

    def get_api_key(self) -> str:
        return (self.api_keys.get(self.provider) or "").strip()


class AiSettingsRead(BaseModel):
    provider: AiProviderName
    model: str
    base_url: str
    api_keys: dict[str, str]
    api_key_set: bool
    updated_at: str = ""
    storage_warning: str
    providers: list["AiProviderProfileRead"] = Field(default_factory=list)


class AiProviderProfilePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: AiProviderName
    protocol: Literal[
        "openai_chat", "openai_responses", "anthropic_messages",
        "gemini_generate", "vertex_generate", "codex", "gemini_cli",
    ] = "openai_chat"
    model: str = Field(min_length=1, max_length=160)
    base_url: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)
    credential_ref: str = Field(default="", max_length=256, pattern=r"^$|^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    priority: int = Field(default=100, ge=1, le=9999)
    enabled: bool = True
    reasoning_effort: str = Field(default="", max_length=20, pattern=r"^$|^(minimal|low|medium|high|xhigh|max|ultra)$")


class AiProviderProfileRead(BaseModel):
    id: int
    name: str
    provider: AiProviderName
    protocol: str
    model: str
    base_url: str
    priority: int
    enabled: bool
    api_key_set: bool
    api_key_masked: str = ""
    credential_ref: str = ""
    reasoning_effort: str = ""
    updated_at: str = ""


class AiProviderTestResponse(BaseModel):
    ok: bool
    provider: AiProviderName
    model: str
    base_url: str
    fallback_available: bool
    error: str | None = None


class AiModelOption(BaseModel):
    id: str
    name: str


class AiModelsResponse(BaseModel):
    ok: bool
    provider: AiProviderName
    models: list[AiModelOption] = Field(default_factory=list)
    error: str | None = None


class CredentialMetadata(BaseModel):
    """Metadata safe to return to an operator; the stored secret never leaves the service."""

    name: str
    kind: str | None = None
    configured: bool = False
    created_at: str | None = None
    updated_at: str | None = None


class CredentialDeletePayload(BaseModel):
    confirm_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


class CredentialDeleteResponse(BaseModel):
    name: str
    deleted: bool


class BackupSettingsPayload(BaseModel):
    remote_enabled: bool = False
    endpoint: str = Field(default="", max_length=500)
    region: str = Field(default="us-east-1", max_length=64)
    bucket: str = Field(default="", max_length=63)
    prefix: str = Field(default="lynewright", max_length=180)
    credential_ref: str = Field(default="backup.s3", max_length=256, pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
    retention_days: int = Field(default=30, ge=1, le=3650)


class BackupSettingsRead(BackupSettingsPayload):
    local_backups: list[dict] = Field(default_factory=list)
    last_local_backup: dict | None = None
    last_remote_sync: dict | None = None
    last_error: str = ""
