from __future__ import annotations

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    slug: str = Field(default="", max_length=63)


class TenantRead(BaseModel):
    id: str
    slug: str
    name: str
    status: str = "active"
    role: str = "viewer"
    default_project_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


class TenantProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    project_id: str | None = Field(default=None, min_length=3, max_length=100)


class TenantProjectRead(BaseModel):
    project_id: str
    tenant_id: str
    name: str
    status: str = "active"
    timezone: str = "UTC"
    reporting_currency: str = "USD"
    locale: str = "en"
    created_at: str = ""
    updated_at: str = ""
