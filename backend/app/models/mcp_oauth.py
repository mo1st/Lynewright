"""HTTP request and response models for first-party MCP OAuth."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MCPOAuthDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    scopes: list[str] = Field(default_factory=list, max_length=128)
    confirm_writes: bool = False

    @field_validator("scopes")
    @classmethod
    def unique_scopes(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Duplicate scopes are not allowed.")
        return value


class MCPOAuthRequestDetails(BaseModel):
    request_id: str
    application_name: str
    redirect_origin: str
    requested_scopes: list[str]
    default_scopes: list[str]
    account_email: str
    expires_at: float


class MCPOAuthDecisionResult(BaseModel):
    redirect_to: str


class MCPOAuthConnection(BaseModel):
    id: str
    application_name: str
    account_email: str
    approved_scopes: list[str]
    created_at: str
    revoked_at: str | None = None
