from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


LogLevel = Literal["debug", "info", "warning", "error", "critical"]


class ApplicationLog(BaseModel):
    id: int
    level: LogLevel
    category: str
    source: str
    message: str
    details: dict[str, Any]
    correlation_id: str
    request_path: str
    project_id: str | None = None
    created_at: str


class ApplicationLogList(BaseModel):
    items: list[ApplicationLog]
    total: int
    counts: dict[str, int]


class FrontendLogRequest(BaseModel):
    level: Literal["warning", "error"] = "error"
    category: str = Field(default="frontend", min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)
    request_path: str = Field(default="", max_length=500)
    # The correlation id of the backend request that failed, so a browser-side
    # report joins to the server-side entry instead of sitting next to it.
    correlation_id: str = Field(default="", max_length=120)

