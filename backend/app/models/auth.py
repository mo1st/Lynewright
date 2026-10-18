from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Minimal, dependency-free email shape check (no external email-validator).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(value: str) -> str:
    normalized = value.strip().lower()
    if not _EMAIL_RE.match(normalized):
        raise ValueError("A valid email address is required.")
    return normalized


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _validate_email(value)


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    is_active: bool = True
    created_at: str = ""
    updated_at: str = ""


class LoginResponse(BaseModel):
    token: str
    user: UserOut


class CurrentUserTenant(BaseModel):
    id: str
    slug: str
    name: str
    role: str
    default_project_id: str | None = None


class CurrentUser(BaseModel):
    id: int
    email: str
    role: str
    is_active: bool = True
    synthetic: bool = False
    active_tenant_id: str | None = None
    tenants: list[CurrentUserTenant] = Field(default_factory=list)


class CreateUserRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    role: str = "viewer"

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _validate_email(value)


class UpdateUserRequest(BaseModel):
    email: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str | None) -> str | None:
        return _validate_email(value) if value is not None else None


class ChangeMyPasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class ChangeMyEmailRequest(BaseModel):
    new_email: str
    current_password: str = Field(min_length=1)

    @field_validator("new_email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _validate_email(value)
