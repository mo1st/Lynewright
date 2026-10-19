"""Executable Social Publisher v1 contract shared by every provider adapter."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.integration_errors import IntegrationExternalError, normalize_external_error
from app.services.platform_services import PlatformOperationError, PlatformValidationError


SOCIAL_PUBLISHER_CONTRACT = "lynewright.social.publisher"
SOCIAL_PUBLISHER_VERSION = "1.0.0"
SOCIAL_PUBLISHER_REQUIRED_OPERATIONS = frozenset({
    "accounts.list", "posts.create", "posts.get", "posts.cancel", "health",
})
SOCIAL_PUBLISHER_OPTIONAL_OPERATIONS = frozenset({
    "posts.schedule", "posts.list", "analytics.summary", "media.upload",
})
SOCIAL_PUBLISHER_OPERATIONS = SOCIAL_PUBLISHER_REQUIRED_OPERATIONS | SOCIAL_PUBLISHER_OPTIONAL_OPERATIONS

# The contract validator requires a post envelope with stable identity and
# state. Providers may return additional normalized post fields (for example
# account_id, content, or scheduled_for), so the schema describes those
# guarantees without inventing a provider-independent field list.
SOCIAL_PUBLISHER_POST_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "post": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "status": {"type": "string", "minLength": 1},
            },
            "required": ["id", "status"],
            "additionalProperties": True,
        },
    },
    "required": ["post"],
    "additionalProperties": True,
}


def validate_social_publisher_request(
    operation: str,
    payload: Mapping[str, Any],
    *,
    direct_lookup: bool,
) -> dict[str, Any]:
    """Validate provider-neutral input, including the portable post lookup rule.

    ``posts.get`` always accepts a date-window hint. Direct-lookup providers
    ignore it; providers without direct lookup require both bounds and search
    within that window. This is contract behavior, not a provider branch in
    shared callers or the conformance harness.
    """
    value = dict(payload)
    if operation not in SOCIAL_PUBLISHER_OPERATIONS:
        raise _unsupported(operation)
    if operation in {"posts.create", "posts.schedule"}:
        _required(value, "account_id")
        content = _required(value, "content")
        if len(content) > 5_000:
            raise _invalid_request("Social post content must be at most 5,000 characters.")
        if operation == "posts.schedule":
            _required(value, "scheduled_for")
    elif operation in {"posts.get", "posts.cancel"}:
        _required(value, "post_id")
    if operation in {"posts.get", "posts.list"}:
        start, end = str(value.get("start_date") or ""), str(value.get("end_date") or "")
        if bool(start) != bool(end):
            raise _invalid_request("Social publisher lookup windows require both start_date and end_date.")
        if operation == "posts.get" and not direct_lookup and not start:
            raise _invalid_request("This provider requires start_date and end_date for post lookup.")
    return value


def validate_social_publisher_response(operation: str, value: Any) -> dict[str, Any]:
    """Reject malformed provider output before it crosses the contract boundary."""
    if not isinstance(value, dict):
        raise _invalid_response(operation)
    if operation == "accounts.list":
        accounts = value.get("accounts")
        if not isinstance(accounts, list) or not all(_account(item) for item in accounts):
            raise _invalid_response(operation)
    elif operation in {"posts.create", "posts.schedule", "posts.get", "posts.cancel"}:
        if not _post(value.get("post")):
            raise _invalid_response(operation)
    elif operation == "posts.list":
        posts = value.get("posts")
        if not isinstance(posts, list) or not all(_post(item) for item in posts):
            raise _invalid_response(operation)
    elif operation == "analytics.summary" and not isinstance(value.get("analytics"), dict):
        raise _invalid_response(operation)
    elif operation == "health" and not str(value.get("state") or ""):
        raise _invalid_response(operation)
    return value


def _required(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "")
    if not value:
        raise _invalid_request(f"Social publisher requires {key}.")
    return value


def _account(value: Any) -> bool:
    return isinstance(value, dict) and bool(str(value.get("id") or "")) and bool(str(value.get("name") or "")) and bool(str(value.get("platform") or ""))


def _post(value: Any) -> bool:
    return isinstance(value, dict) and bool(str(value.get("id") or "")) and bool(str(value.get("status") or ""))


def _invalid_request(message: str) -> IntegrationExternalError:
    return normalize_external_error(PlatformValidationError(message), provider="social_publisher")


def _unsupported(operation: str) -> IntegrationExternalError:
    return normalize_external_error(PlatformOperationError(f"unsupported social publisher operation: {operation}"), provider="social_publisher")


def _invalid_response(operation: str) -> IntegrationExternalError:
    return normalize_external_error(ValueError(f"invalid social publisher {operation} response"), provider="social_publisher")
