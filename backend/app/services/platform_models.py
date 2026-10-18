"""Validated, adapter-neutral inputs for the Lynewright platform services."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _PlatformInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectCreateInput(_PlatformInput):
    name: str = Field(min_length=1, max_length=120)
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


class ProjectUpdateInput(_PlatformInput):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    status: str | None = Field(default=None, pattern=r"^(active|archived)$")

    @model_validator(mode="after")
    def requires_change(self) -> "ProjectUpdateInput":
        if self.name is None and self.status is None:
            raise ValueError("At least one project field must be supplied.")
        return self


class JobCreateInput(_PlatformInput):
    type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,99}$")
    payload: dict[str, Any] = Field(default_factory=dict)


class MediaCreateInput(_PlatformInput):
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=2_048)
    content_type: str = Field(default="application/octet-stream", min_length=3, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CredentialSetInput(_PlatformInput):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    secret: str = Field(min_length=1, max_length=16_384)
    kind: str = Field(default="token", pattern=r"^[a-z][a-z0-9_.-]{0,63}$")


class SettingSetInput(_PlatformInput):
    key: str = Field(pattern=r"^platform\.[a-z][a-z0-9_.-]{0,127}$")
    value: bool | int | float | str | list[Any] | dict[str, Any] | None


class EventCreateInput(_PlatformInput):
    type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,99}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


class ClientCreateInput(_PlatformInput):
    name: str = Field(min_length=1, max_length=120)
    client_type: Literal["mcp", "cli", "automation", "extension"] = "mcp"
    permission_mode: Literal["restricted", "custom", "full"] = "restricted"
    granted_capabilities: list[str] = Field(default_factory=list, max_length=100)


class ClientUpdateInput(_PlatformInput):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    permission_mode: Literal["restricted", "custom", "full"] | None = None
    granted_capabilities: list[str] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def requires_change(self) -> "ClientUpdateInput":
        if self.name is None and self.enabled is None and self.permission_mode is None and self.granted_capabilities is None:
            raise ValueError("At least one client field must be supplied.")
        if self.permission_mode is None and self.granted_capabilities is not None:
            raise ValueError("Updating capabilities requires an explicit permission_mode.")
        return self
