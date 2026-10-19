"""Provider-neutral, redacted errors for integration boundaries.

Adapters may keep provider-specific context locally, but callers and generic
runtime code only receive a small stable category plus safe diagnostics.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

import httpx

from app.services.integration_execution import IntegrationExecutionTimeout
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformValidationError,
)


class IntegrationErrorCategory(StrEnum):
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    TIMEOUT = "TIMEOUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CANCELLED = "CANCELLED"
    UNKNOWN_EXTERNAL_ERROR = "UNKNOWN_EXTERNAL_ERROR"


_SENSITIVE_KEY = re.compile(r"(authorization|cookie|credential|secret|token|password|api[_-]?key)", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|credential|secret|token|password|api[_-]?key)=([^&\s,]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\b(bearer)\s+[^\s,]+")


@dataclass(frozen=True, slots=True)
class IntegrationErrorDetails:
    category: IntegrationErrorCategory
    message: str
    diagnostic: dict[str, Any]


class IntegrationExternalError(PlatformOperationError):
    """A safe, stable representation of an external provider failure."""

    code = "integration_external_error"

    def __init__(self, details: IntegrationErrorDetails) -> None:
        super().__init__(details.message)
        self.category = details.category
        self.diagnostic = details.diagnostic

    def as_dict(self) -> dict[str, Any]:
        return {"category": self.category.value, "message": self.message, "diagnostic": self.diagnostic}


def normalize_http_error(
    status_code: int,
    *,
    headers: dict[str, Any] | None = None,
    provider: str = "",
) -> IntegrationExternalError:
    """Normalize a non-success HTTP status without retaining response bodies."""
    category = {
        400: IntegrationErrorCategory.INVALID_REQUEST,
        401: IntegrationErrorCategory.AUTHENTICATION_FAILED,
        403: IntegrationErrorCategory.AUTHORIZATION_FAILED,
        404: IntegrationErrorCategory.NOT_FOUND,
        409: IntegrationErrorCategory.CONFLICT,
        429: IntegrationErrorCategory.RATE_LIMITED,
    }.get(status_code, IntegrationErrorCategory.PROVIDER_UNAVAILABLE if status_code >= 500 else IntegrationErrorCategory.UNKNOWN_EXTERNAL_ERROR)
    diagnostic: dict[str, Any] = {"status_code": int(status_code)}
    if provider:
        diagnostic["provider"] = provider
    safe_headers = redact_diagnostic(headers or {})
    if safe_headers:
        diagnostic["headers"] = safe_headers
    return _error(category, diagnostic=diagnostic)


def normalize_external_error(
    error: BaseException,
    *,
    provider: str = "",
    diagnostic: dict[str, Any] | None = None,
    secrets: Iterable[str] = (),
) -> IntegrationExternalError:
    """Map transport/adapter failures to the shared taxonomy.

    This deliberately does not return provider response bodies or exception
    messages verbatim.  They often contain credentials, URLs, or opaque data.
    """
    if isinstance(error, IntegrationExternalError):
        return error
    if isinstance(error, IntegrationExecutionTimeout) or isinstance(error, (TimeoutError, httpx.TimeoutException)):
        category = IntegrationErrorCategory.TIMEOUT
    elif isinstance(error, asyncio.CancelledError):
        category = IntegrationErrorCategory.CANCELLED
    elif isinstance(error, (httpx.ConnectError, httpx.NetworkError)):
        category = IntegrationErrorCategory.CONNECTION_FAILED
    elif isinstance(error, httpx.RequestError):
        category = IntegrationErrorCategory.PROVIDER_UNAVAILABLE
    elif isinstance(error, (ValueError, UnicodeDecodeError)):
        category = IntegrationErrorCategory.INVALID_RESPONSE
    elif isinstance(error, PlatformValidationError):
        category = IntegrationErrorCategory.INVALID_REQUEST
    elif isinstance(error, PlatformNotFoundError):
        category = IntegrationErrorCategory.NOT_FOUND
    elif isinstance(error, PlatformConflictError):
        category = IntegrationErrorCategory.CONFLICT
    else:
        lower = str(error).lower()
        if "unsupported" in lower or "not implemented" in lower:
            category = IntegrationErrorCategory.UNSUPPORTED_FEATURE
        elif "timeout" in lower or "timed out" in lower:
            category = IntegrationErrorCategory.TIMEOUT
        elif "cancel" in lower:
            category = IntegrationErrorCategory.CANCELLED
        elif "connect" in lower or "connection" in lower:
            category = IntegrationErrorCategory.CONNECTION_FAILED
        else:
            category = IntegrationErrorCategory.UNKNOWN_EXTERNAL_ERROR
    safe_diagnostic = dict(diagnostic or {})
    safe_diagnostic["error_type"] = type(error).__name__
    if provider:
        safe_diagnostic["provider"] = provider
    if str(error):
        safe_diagnostic["provider_message"] = str(error)[:500]
    return _error(category, diagnostic=redact_diagnostic(safe_diagnostic, secrets=secrets))


def integration_error(
    category: IntegrationErrorCategory,
    *,
    diagnostic: dict[str, Any] | None = None,
) -> IntegrationExternalError:
    """Create a safe error for a known provider-neutral runtime condition."""
    if category not in IntegrationErrorCategory:
        raise ValueError("Unknown integration error category.")
    return _error(category, diagnostic=redact_diagnostic(diagnostic or {}))


def redact_diagnostic(value: Any, *, secrets: Iterable[str] = ()) -> Any:
    """Recursively remove credential material from diagnostics and log payloads."""
    secret_values = tuple(item for item in secrets if item)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact_diagnostic(item, secrets=secret_values)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_diagnostic(item, secrets=secret_values) for item in value]
    if not isinstance(value, str):
        return value
    result = value
    for secret in secret_values:
        result = result.replace(secret, "[REDACTED]")
    result = _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)
    return _BEARER_TOKEN.sub(lambda match: f"{match.group(1)} [REDACTED]", result)


def _error(category: IntegrationErrorCategory, *, diagnostic: dict[str, Any]) -> IntegrationExternalError:
    messages = {
        IntegrationErrorCategory.AUTHENTICATION_FAILED: "Provider authentication failed.",
        IntegrationErrorCategory.AUTHORIZATION_FAILED: "Provider authorization was denied.",
        IntegrationErrorCategory.RATE_LIMITED: "Provider rate limit was reached.",
        IntegrationErrorCategory.INVALID_REQUEST: "Provider rejected the request.",
        IntegrationErrorCategory.NOT_FOUND: "Provider resource was not found.",
        IntegrationErrorCategory.CONFLICT: "Provider reported a conflicting change.",
        IntegrationErrorCategory.UNSUPPORTED_FEATURE: "Provider does not support this operation.",
        IntegrationErrorCategory.PROVIDER_UNAVAILABLE: "Provider is currently unavailable.",
        IntegrationErrorCategory.CONNECTION_FAILED: "Connection to provider failed.",
        IntegrationErrorCategory.TIMEOUT: "Provider operation timed out.",
        IntegrationErrorCategory.INVALID_RESPONSE: "Provider returned an invalid response.",
        IntegrationErrorCategory.CANCELLED: "Provider operation was cancelled.",
        IntegrationErrorCategory.UNKNOWN_EXTERNAL_ERROR: "Provider operation failed unexpectedly.",
    }
    return IntegrationExternalError(IntegrationErrorDetails(category, messages[category], diagnostic))
