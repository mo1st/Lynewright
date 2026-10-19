"""Deployment-owned origins for services that are deliberately not public.

Some integrations run beside Lynewright on a private container network with no
published ports and no edge route — that is the safe way to deploy them. The
outbound SSRF boundary correctly refuses such an origin, because it cannot tell
an operator's private analytics host from a request that has been steered at
`169.254.169.254`.

So this is a second, narrower path, and its safety comes from *who* controls
it rather than from what it validates:

    manifest / provider / request     may name a ref, and nothing else
                -> resolver           checks the deployment allowlist
                -> pinned origin      exact scheme, host, and port

The allowlist lives in the process environment:

    LYNEWRIGHT_INTERNAL_ENDPOINT__UMAMI=http://umami:3000

Environment rather than a table, deliberately. A runtime-editable allowlist
would put the SSRF exemption behind whatever can reach the API that edits it,
and the exemption list is the one thing most worth being hard to change. Adding
an internal service costs a config change and a redeploy, and zero Core code:
nothing here knows what "umami" is.

What a caller can do is name a ref. What it cannot do is define one, supply an
origin, or reach an origin the deployment did not configure.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from app.services.platform_services import PlatformValidationError


ENVIRONMENT_PREFIX = "LYNEWRIGHT_INTERNAL_ENDPOINT__"

# A ref is a name, never a location. The pattern excludes every shape that
# could carry one: no scheme, no dots, no slashes, no traversal.
_REF_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _ref_from_variable(name: str) -> str:
    return name[len(ENVIRONMENT_PREFIX):].strip().lower()


def normalize_ref(value: str) -> str:
    """A ref, or a refusal.

    Rejects anything that looks like a location rather than a name, so a
    caller cannot smuggle an origin through the field that is supposed to
    select one.
    """
    candidate = str(value or "").strip().lower()
    if not _REF_PATTERN.match(candidate):
        raise PlatformValidationError(
            "An internal endpoint reference must be a plain name, not a URL or path."
        )
    return candidate


def normalize_origin(value: str) -> str:
    """Reduce a configured endpoint to exactly scheme://host:port.

    Anything beyond the origin is dropped rather than honoured: a path,
    query, or fragment in the configuration would become part of every request
    and make the pinning ambiguous. Credentials in the URL are refused outright
    -- they belong in the credential store, and one embedded here would be
    logged with the origin.
    """
    raw = str(value or "").strip()
    if not raw:
        raise PlatformValidationError("An internal endpoint origin cannot be empty.")
    parts = urlsplit(raw)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise PlatformValidationError(
            f"An internal endpoint origin must use http or https, not {parts.scheme!r}."
        )
    if parts.username or parts.password:
        raise PlatformValidationError(
            "An internal endpoint origin must not embed credentials."
        )
    if not parts.hostname:
        raise PlatformValidationError("An internal endpoint origin must name a host.")
    if parts.path.strip("/") or parts.query or parts.fragment:
        raise PlatformValidationError(
            "An internal endpoint origin must be a bare origin with no path or query."
        )
    host = parts.hostname
    port = parts.port
    return f"{parts.scheme}://{host}:{port}" if port else f"{parts.scheme}://{host}"


def configured_refs(environment: dict[str, str] | None = None) -> tuple[str, ...]:
    """Every ref this deployment has configured."""
    source = environment if environment is not None else os.environ
    return tuple(sorted(
        _ref_from_variable(name) for name in source
        if name.startswith(ENVIRONMENT_PREFIX) and _ref_from_variable(name)
    ))


def resolve_internal_endpoint(
    ref: str, *, environment: dict[str, str] | None = None,
) -> str:
    """The pinned origin for a ref, or a refusal naming what is configured."""
    source = environment if environment is not None else os.environ
    normalized = normalize_ref(ref)
    if normalized == "media" and source.get("LYNEWRIGHT_INTERNAL_MEDIA_URL"):
        return normalize_origin(source["LYNEWRIGHT_INTERNAL_MEDIA_URL"])
    raw = source.get(f"{ENVIRONMENT_PREFIX}{normalized.upper()}")
    if not raw:
        available = ", ".join(configured_refs(source)) or "none"
        raise PlatformValidationError(
            f"No internal endpoint is configured for {normalized!r} "
            f"(configured: {available})."
        )
    return normalize_origin(raw)


def validate_configured_endpoints(
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Normalize every configured endpoint, or fail loudly.

    Called at startup so a typo surfaces on deploy rather than inside a
    customer request, where it would arrive as an unexplained provider failure
    long after the change that caused it.
    """
    source = environment if environment is not None else os.environ
    resolved: dict[str, str] = {}
    problems: list[str] = []
    for ref in configured_refs(source):
        try:
            resolved[ref] = normalize_origin(source[f"{ENVIRONMENT_PREFIX}{ref.upper()}"])
        except PlatformValidationError as exc:
            problems.append(f"{ref}: {exc}")
    if problems:
        raise PlatformValidationError(
            "Internal endpoint configuration is invalid -- " + "; ".join(problems)
        )
    return resolved


def pinned_url(origin: str, path: str) -> str:
    """Append a relative path to a pinned origin without leaving it.

    The origin is fixed by configuration and the path is appended to it, so no
    provider response, resource id, or redirect can steer a call to a different
    host. An absolute URL is refused rather than resolved, because resolving it
    would silently discard the pin.
    """
    candidate = str(path or "")
    if "://" in candidate or candidate.startswith("//"):
        raise PlatformValidationError(
            "An internal endpoint path must be relative to its pinned origin."
        )
    segments = [segment for segment in candidate.split("/") if segment not in ("", ".")]
    if any(segment == ".." for segment in segments):
        raise PlatformValidationError(
            "An internal endpoint path must not traverse above its origin."
        )
    suffix = "/".join(segments)
    return f"{origin}/{suffix}" if suffix else origin


__all__ = [
    "ENVIRONMENT_PREFIX",
    "configured_refs",
    "normalize_origin",
    "normalize_ref",
    "pinned_url",
    "resolve_internal_endpoint",
    "validate_configured_endpoints",
]
