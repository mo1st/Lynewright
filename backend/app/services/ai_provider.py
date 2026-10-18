from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

# `httpx` logs full request URLs at INFO level. Gemini authenticates with a
# query-string API key, so allowing those transport lines into application
# logs or a process console would disclose a credential.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from app.config import get_settings
from app.models.settings import AI_PROVIDERS, mask_key
from app.services.application_logging import log_event
from app.services.url_security import (
    DNSResolutionError,
    URLSecurityError,
    resolve_public_target,
)


DEMO_KEY_STORAGE_WARNING = (
    "For production, prefer environment variables for API keys. "
    "SQLite key storage is intended only for local MVP testing unless encrypted."
)

# Sampling temperature is a per-task decision, not a global one. Structured
# diagnosis wants the modal answer; marketing copy wants range. Shipping one
# value for both is why every regeneration came back a near-duplicate of the
# last: a near-greedy decode on an unchanged prompt is definitionally the same
# output, so no amount of prompt work upstream could produce variety.
ANALYTICAL_TEMPERATURE = 0.2
CREATIVE_TEMPERATURE = 0.9

OPENAI_STYLE_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "grok": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
}
PROVIDER_PROTOCOLS = {
    "openai_chat", "openai_responses", "anthropic_messages", "gemini_generate",
    "vertex_generate", "codex", "gemini_cli",
}
DEFAULT_PROTOCOL_BY_PROVIDER = {
    "openai": "openai_chat",
    "openai_compatible": "openai_chat",
    "grok": "openai_chat",
    "mistral": "openai_chat",
    "deepseek": "openai_chat",
    "anthropic": "anthropic_messages",
    "gemini": "gemini_generate",
    "vertex": "vertex_generate",
    "codex": "codex",
    "gemini_cli": "gemini_cli",
}
KEYLESS_PROVIDERS = {"codex", "gemini_cli", "mock"}

# Seeded when Vertex's live model model list is unreachable (its list endpoint may
# reject express API keys). The model field stays free-text, so newer ids can
# always be typed in; these just give the picker sensible defaults.
_VERTEX_FALLBACK_MODELS: list[dict[str, str]] = [
    {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
    {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
    {"id": "gemini-2.5-flash-lite", "name": "Gemini 2.5 Flash-Lite"},
    {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
    {"id": "gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash-Lite"},
]

_PROVIDER_CIRCUIT: dict[str, tuple[float, str]] = {}
_CIRCUIT_SECONDS = 60.0


class ProviderText(str):
    """Provider text with normalized usage metadata attached.

    It remains a ``str`` for compatibility with existing adapters and tests,
    while the Operator can account for the provider's actual token usage when
    the protocol returned it.
    """

    def __new__(cls, value: str, usage: dict[str, Any] | None = None):
        instance = super().__new__(cls, value)
        instance.usage = usage or {}
        return instance


_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
_DEFAULT_READ_TIMEOUT_SECONDS = 120.0
# Gemini thinking models consume the output budget for their private reasoning
# as well as their visible response. Keep this health check bounded but leave
# ample room for the model to finish its tiny JSON reply.
_PROVIDER_TEST_MAX_TOKENS = 4_096


def _provider_test_thinking_level(settings: dict[str, Any]) -> str:
    """Use the cheapest thinking mode that the selected Gemini model accepts."""
    model = str(settings.get("model") or "").removeprefix("models/").lower()
    minimal_prefixes = (
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3-flash",
        "gemini-3.1-flash-lite",
    )
    return "minimal" if model.startswith(minimal_prefixes) else "low"


class AiProviderError(Exception):
    """A provider call that did not produce usable output.

    `context` carries the structured facts a log needs: `failure_kind` (so
    outages can be told apart from truncation and bad keys without parsing the
    message), the endpoint, the provider's own error body, and a `hint`.
    """

    def __init__(self, message: str, *, status_code: int | None = None, **context: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        if status_code is not None:
            context.setdefault("http_status", status_code)
        self.context: dict[str, Any] = {
            key: value for key, value in context.items() if value not in (None, "")
        }


def settings_response(settings: dict[str, Any]) -> dict[str, Any]:
    from app.database import list_ai_provider_profiles

    api_keys: dict[str, str] = settings.get("api_keys") or {}
    provider = selected_provider(settings)
    profiles = []
    for profile in list_ai_provider_profiles():
        from app.database import _ai_profile_as_settings
        resolved_profile = _ai_profile_as_settings(profile)
        key = str((resolved_profile.get("api_keys") or {}).get(str(profile.get("provider") or "")) or "")
        credential_ref = str(profile.get("credential_ref") or "")
        profiles.append({
            "id": int(profile["id"]), "name": str(profile["name"]),
            "provider": str(profile["provider"]), "protocol": str(profile["protocol"]),
            "model": str(profile["model"]), "base_url": str(profile["base_url"] or ""),
            "priority": int(profile["priority"]), "enabled": bool(profile["enabled"]),
            "api_key_set": bool(key),
            "api_key_masked": mask_key(key) if key else ("configured" if credential_ref else ""),
            "credential_ref": credential_ref,
            "updated_at": str(profile["updated_at"] or ""),
        })
    return {
        "provider": provider,
        "model": str(settings.get("model") or "mock-brief-v1"),
        "base_url": str(settings.get("base_url") or "https://api.openai.com/v1"),
        "api_keys": {name: mask_key(api_keys.get(name, "")) for name in AI_PROVIDERS},
        "api_key_set": has_configured_api_key(settings),
        "updated_at": str(settings.get("updated_at") or ""),
        "storage_warning": DEMO_KEY_STORAGE_WARNING,
        "providers": profiles,
    }


def test_provider(settings: dict[str, Any]) -> dict[str, Any]:
    provider = selected_provider(settings)
    identity = _provider_identity(settings)
    response: dict[str, Any] = {
        "ok": True,
        "provider": provider,
        "model": str(settings.get("model") or "mock-brief-v1"),
        "base_url": provider_base_url(settings),
        "fallback_available": True,
        "error": None,
    }
    if provider == "mock":
        return response
    if provider == "gemini_cli" or selected_protocol(settings) == "gemini_cli":
        from app.services.gemini_cli_provider import GeminiCliProvider
        try:
            health = GeminiCliProvider.health(settings)
        except Exception as exc:
            response["ok"] = False
            response["error"] = str(exc)
            response["details"] = getattr(exc, "context", {})
            return response
        response.update({key: value for key, value in health.items() if key not in {"ok", "error"}})
        response["ok"] = bool(health.get("ok"))
        response["error"] = health.get("error")
        return response
    if provider == "codex" or selected_protocol(settings) == "codex":
        from app.services.codex_provider import CodexProvider, CodexProviderError
        try:
            health = CodexProvider.health(settings)
        except CodexProviderError as exc:
            response["ok"] = False
            response["error"] = str(exc)
            response["details"] = dict(exc.context)
            return response
        response.update({key: value for key, value in health.items() if key not in {"ok", "error"}})
        response["ok"] = bool(health.get("ok"))
        response["error"] = health.get("error")
        return response
    if not selected_api_key(settings):
        response["ok"] = False
        response["error"] = f"No API key is configured for {provider}."
        return response

    try:
        content = _call_provider(
            settings=settings,
            system_prompt='Return only this JSON object: {"ok": true}',
            user_prompt="connection test",
            max_tokens=_PROVIDER_TEST_MAX_TOKENS,
            thinking_level=_provider_test_thinking_level(settings),
        )
        if not content.strip():
            raise AiProviderError("AI provider returned an empty response.", failure_kind="empty_response")
        _PROVIDER_CIRCUIT.pop(identity, None)
    except AiProviderError as exc:
        _open_provider_circuit(identity, str(exc))
        log_event(
            level="error", category="ai_provider", source="ai_provider.test",
            message=str(exc), exc=exc,
            details={
                "provider": provider,
                "model": response["model"],
                "protocol": selected_protocol(settings),
                "base_url": _safe_url(provider_base_url(settings)),
                "status_code": exc.status_code,
                **exc.context,
            },
        )
        response["ok"] = False
        response["error"] = str(exc)
        response["details"] = dict(exc.context)
        response["hint"] = str(exc.context.get("hint") or "")
    return response


def generate_ai_json(
    *,
    settings: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 3500,
    temperature: float = ANALYTICAL_TEMPERATURE,
    thinking_level: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prompt_chars = len(system_prompt) + len(user_prompt)
    settings["_last_prompt_chars"] = prompt_chars
    settings["_last_input_token_estimate"] = max(1, math.ceil(prompt_chars / 4))
    candidates = _provider_candidates(settings)
    failures: list[dict[str, Any]] = []
    last_error: AiProviderError | None = None
    configured_attempt_limit = settings.get("_max_provider_attempts")
    try:
        attempt_limit = max(1, min(len(candidates), int(configured_attempt_limit))) \
            if configured_attempt_limit is not None else len(candidates)
    except (TypeError, ValueError):
        attempt_limit = len(candidates)
    settings["_last_provider_usage"] = {}
    settings["_last_provider_attempt_count"] = 0
    settings["_last_provider_used"] = ""
    settings["_last_provider_profile_id"] = ""
    settings["_last_provider_model"] = ""
    settings["_last_provider_protocol"] = ""
    settings["_last_provider_attempt_records"] = []
    attempt_records: list[dict[str, Any]] = []
    slot_fallback_allowed = True
    for position, candidate in enumerate(candidates[:attempt_limit], start=1):
        if candidate.get("_slot_fallback_candidate") and not slot_fallback_allowed:
            continue
        provider = selected_provider(candidate)
        identity = _provider_identity(candidate)
        attempt_id = f"aiatt_{secrets.token_hex(12)}"
        network_attempt = False
        usage: dict[str, Any] = {}
        try:
            if provider not in {"codex", "gemini_cli"} and (provider == "mock" or not selected_api_key(candidate)):
                raise AiProviderError(
                    f"No API key is configured for {candidate.get('profile_name') or provider}.",
                    failure_kind="not_configured", provider=provider,
                    profile_id=candidate.get("profile_id"),
                    hint="Add a key to this provider profile or disable it.",
                )
            circuit = _PROVIDER_CIRCUIT.get(identity)
            if circuit and circuit[0] > time.monotonic():
                remaining = max(1, round(circuit[0] - time.monotonic()))
                raise AiProviderError(
                    f"{candidate.get('profile_name') or provider} is temporarily paused for {remaining}s "
                    f"after a recent failure: {circuit[1]}",
                    failure_kind="circuit_open", provider=provider,
                    profile_id=candidate.get("profile_id"),
                    paused_seconds_remaining=remaining, first_failure=str(circuit[1])[:500],
                    hint="This is a suppressed retry, not a new provider failure — see the original error above.",
                )
            network_attempt = True
            content = _call_provider(
                settings=candidate, system_prompt=system_prompt, user_prompt=user_prompt,
                max_tokens=max_tokens, temperature=temperature,
                thinking_level=thinking_level, json_schema=json_schema,
            )
            usage = dict(getattr(content, "usage", {}) or {})
            exact_usage = bool(usage.get("exact"))
            usage.setdefault("input_tokens", settings["_last_input_token_estimate"])
            usage.setdefault("output_tokens", max(1, math.ceil(len(content) / 4)))
            usage["exact"] = int(exact_usage)
            usage["prompt_chars"] = prompt_chars
            usage["provider_used"] = provider
            try:
                parsed = _parse_json_object(content)
            except (json.JSONDecodeError, ValueError) as exc:
                raise AiProviderError(
                    "AI provider returned non-JSON content.", failure_kind="non_json",
                    provider=provider, profile_id=candidate.get("profile_id"),
                    content_preview=content[:400],
                    hint="The model answered with prose; the next configured provider will be tried.",
                ) from exc
            if not isinstance(parsed, dict):
                raise AiProviderError(
                    "AI provider returned JSON that is not an object.",
                    failure_kind="non_object_json", provider=provider,
                    profile_id=candidate.get("profile_id"), content_preview=content[:400],
                )
            _PROVIDER_CIRCUIT.pop(identity, None)
            settings["_last_provider_used"] = provider
            settings["_last_provider_profile_id"] = candidate.get("profile_id")
            settings["_last_provider_model"] = str(candidate.get("model") or "")
            settings["_last_provider_protocol"] = selected_protocol(candidate)
            settings["_last_provider_usage"] = usage
            settings["_last_provider_attempt_count"] = position
            settings["_last_provider_attempts"] = failures
            attempt_records.append({
                "attempt_id": attempt_id, "ordinal": position, "provider": provider,
                "profile_id": candidate.get("profile_id"),
                "model": str(candidate.get("model") or ""),
                "protocol": selected_protocol(candidate), "outcome": "succeeded",
                "failure_kind": "", "network_attempt": network_attempt,
                "usage": dict(usage),
            })
            settings["_last_provider_attempt_records"] = attempt_records
            if failures:
                log_event(
                    level="warning", category="ai_provider", source="ai_provider.fallback",
                    message=f"AI request succeeded with fallback provider {candidate.get('profile_name') or provider}.",
                    details={"selected_position": position, "attempts": failures,
                             "usage": usage},
                )
            return parsed
        except AiProviderError as exc:
            last_error = exc
            if candidate is candidates[0] and provider == "codex" and exc.context.get("failure_kind") not in {
                "quota", "auth", "timeout", "runner_failed", "daily_cap",
            }:
                slot_fallback_allowed = False
            failure_kind = str(exc.context.get("failure_kind") or "provider_error")
            codex_fallback_failure = provider == "codex" and failure_kind in {
                "quota", "auth", "timeout", "runner_failed", "daily_cap",
            }
            if failure_kind != "circuit_open" and not codex_fallback_failure:
                _open_provider_circuit(identity, str(exc))
            failure = {
                "position": position, "profile_id": candidate.get("profile_id"),
                "profile_name": candidate.get("profile_name") or provider,
                "provider": provider, "model": str(candidate.get("model") or ""),
                "failure_kind": exc.context.get("failure_kind") or "provider_error",
                "error": str(exc)[:500],
            }
            failures.append(failure)
            error_usage = exc.context.get("usage")
            if not usage and isinstance(error_usage, dict):
                usage = dict(error_usage)
                usage.setdefault("input_tokens", settings["_last_input_token_estimate"])
                usage.setdefault("output_tokens", 0)
                usage.setdefault("prompt_chars", prompt_chars)
                usage.setdefault("exact", 0)
            attempt_records.append({
                "attempt_id": attempt_id, "ordinal": position, "provider": provider,
                "profile_id": candidate.get("profile_id"),
                "model": str(candidate.get("model") or ""),
                "protocol": selected_protocol(candidate), "outcome": "failed",
                "failure_kind": failure["failure_kind"], "network_attempt": network_attempt,
                "usage": dict(usage) if usage else {
                    "input_tokens": int(settings.get("_last_input_token_estimate") or 0),
                    "output_tokens": 0, "prompt_chars": prompt_chars, "exact": 0,
                    "http_attempts": int(exc.context.get("attempts") or 0),
                },
            })
            settings["_last_provider_attempt_records"] = attempt_records
            settings["_last_provider_attempt_count"] = position
            if failure_kind not in {"quota", "daily_cap"}:
                log_event(
                    level="warning" if position < len(candidates) or failure_kind == "circuit_open" else "error",
                    category="ai_provider", source="ai_provider.generate", message=str(exc), exc=exc,
                    details={
                        **failure, "protocol": selected_protocol(candidate),
                        "base_url": _safe_url(provider_base_url(candidate)),
                        "status_code": exc.status_code, "max_tokens": max_tokens,
                        "temperature": temperature, "thinking_level": thinking_level or "",
                        "prompt_chars": len(system_prompt) + len(user_prompt), **exc.context,
                    },
                )
    settings["_last_provider_attempts"] = failures
    settings["_last_provider_attempt_records"] = attempt_records
    if last_error is not None and len(attempt_records) == 1 and len(candidates) == 1:
        raise last_error
    raise AiProviderError(
        "All configured AI providers failed; deterministic fallback is active.",
        failure_kind="provider_chain_exhausted", attempts=failures,
        hint="Review the provider attempts in priority order and fix or disable failing profiles.",
    )


def _provider_candidates(settings: dict[str, Any]) -> list[dict[str, Any]]:
    primary = {
        key: value for key, value in settings.items()
        if key not in {"fallback_chain", "primary", "fallback", "_slot_fallback"}
    }
    chain = settings.get("fallback_chain")
    candidates = [primary]
    slot_fallback = settings.get("_slot_fallback")
    if isinstance(slot_fallback, dict) and slot_fallback:
        fallback = dict(slot_fallback)
        fallback["_slot_fallback_candidate"] = True
        candidates.append(fallback)
    if isinstance(chain, list):
        candidates.extend(dict(item) for item in chain if isinstance(item, dict))
    return candidates


def _provider_identity(settings: dict[str, Any]) -> str:
    profile_id = settings.get("profile_id")
    return f"profile:{profile_id}" if profile_id else selected_provider(settings)


def fetch_provider_models(settings: dict[str, Any]) -> list[dict[str, str]]:
    provider = selected_provider(settings)
    protocol = selected_protocol(settings)
    if provider == "mock":
        return [{"id": "mock-brief-v1", "name": "Deterministic mock"}]

    if provider == "gemini_cli" or protocol == "gemini_cli":
        from app.services.gemini_cli_provider import GeminiCliProvider
        try:
            return GeminiCliProvider.list_models(settings)
        except Exception as exc:
            raise AiProviderError(str(exc), **getattr(exc, "context", {})) from exc

    if provider == "codex" or protocol == "codex":
        from app.services.codex_provider import CodexProvider, CodexProviderError
        try:
            return CodexProvider.list_models(settings)
        except CodexProviderError as exc:
            raise AiProviderError(str(exc), **exc.context) from exc

    if protocol == "vertex_generate":
        return _fetch_vertex_models(settings)

    api_key = selected_api_key(settings)
    if not api_key:
        raise AiProviderError(f"No API key is configured for {provider}.")

    headers = {"Accept": "application/json"}
    if protocol == "gemini_generate":
        url = f"{provider_base_url(settings)}/models?key={quote(api_key)}"
    elif protocol == "anthropic_messages":
        url = f"{provider_base_url(settings)}/models"
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    else:
        url = f"{provider_base_url(settings)}/models"
        headers["Authorization"] = f"Bearer {api_key}"

    response = _request("get", url, headers=headers)
    try:
        payload = response.json()
    except ValueError as exc:
        raise AiProviderError("Provider model response was not valid JSON.") from exc

    models: list[dict[str, str]] = []
    rows = payload.get("models" if protocol == "gemini_generate" else "data", [])
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if protocol == "gemini_generate":
            methods = row.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue
            model_id = str(row.get("name") or "").strip()
            name = str(row.get("displayName") or model_id).strip()
        else:
            model_id = str(row.get("id") or "").strip()
            name = str(row.get("display_name") or row.get("name") or model_id).strip()
        if model_id:
            models.append({"id": model_id, "name": name or model_id})

    unique = {model["id"]: model for model in models}
    return sorted(unique.values(), key=lambda item: item["name"].lower())[:250]


def _fetch_vertex_models(settings: dict[str, Any]) -> list[dict[str, str]]:
    """List Vertex Gemini models, always seeded with a curated fallback.

    Vertex's ``publishers/google/models`` model list may reject express API keys, so
    a failed or empty live lookup falls back to known Gemini ids rather than
    erroring — the picker must never come up empty for Vertex.
    """
    models: dict[str, dict[str, str]] = {
        item["id"]: dict(item) for item in _VERTEX_FALLBACK_MODELS
    }
    api_key = selected_api_key(settings)
    if not api_key:
        return sorted(models.values(), key=lambda item: item["id"])

    payload: Any = None
    try:
        response = _request(
            "get",
            f"{provider_base_url(settings)}/publishers/google/models",
            headers={"x-goog-api-key": api_key, "Accept": "application/json"},
        )
        payload = response.json()
    except (AiProviderError, ValueError):
        payload = None

    rows = payload.get("publisherModels") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = str(row.get("name") or "").strip().split("/")[-1]
            # generateContent targets chat-capable Gemini models; skip embeddings
            # and image publishers that would not parse as JSON text.
            if model_id.startswith("gemini"):
                models.setdefault(model_id, {"id": model_id, "name": model_id})

    return sorted(models.values(), key=lambda item: item["id"])


def selected_provider(settings: dict[str, Any]) -> str:
    provider = str(settings.get("provider") or "mock").strip()
    return provider or "mock"


def selected_protocol(settings: dict[str, Any]) -> str:
    protocol = str(settings.get("protocol") or "").strip()
    if protocol in PROVIDER_PROTOCOLS:
        return protocol
    return DEFAULT_PROTOCOL_BY_PROVIDER.get(selected_provider(settings), "openai_chat")


def selected_api_key(settings: dict[str, Any]) -> str:
    provider = selected_provider(settings)
    if provider in {"codex", "gemini_cli"}:
        # CLI providers authenticate through their protected local sessions, never a
        # Lynewright API-key field.
        return ""
    explicit_env_name = str(settings.get("api_key_env_name") or "").strip()
    if explicit_env_name:
        explicit_key = os.getenv(explicit_env_name, "").strip()
        if explicit_key:
            return explicit_key
        api_keys = settings.get("api_keys") or {}
        return str(api_keys.get(provider) or "").strip() if isinstance(api_keys, dict) else ""
    env_names = {
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "vertex": "VERTEX_ACCESS_TOKEN",
        "anthropic": "ANTHROPIC_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "grok": "GROK_API_KEY",
        "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    }
    env_key = os.getenv(env_names.get(provider, ""), "").strip()
    if env_key:
        return env_key

    api_keys = settings.get("api_keys") or {}
    if not isinstance(api_keys, dict):
        return ""
    return str(api_keys.get(provider) or "").strip()


def active_provider(settings: dict[str, Any]) -> str:
    return selected_provider(settings)


def has_configured_api_key(settings: dict[str, Any]) -> bool:
    return any(
        (
            selected_provider(candidate) == "codex"
            and __import__("app.services.codex_provider", fromlist=["is_enabled"]).is_enabled(candidate)
        )
        or (
            selected_provider(candidate) == "gemini_cli"
            and __import__("app.services.gemini_cli_provider", fromlist=["GeminiCliProvider"]).GeminiCliProvider.is_enabled(candidate)
        )
        or (selected_provider(candidate) not in {"mock", "codex", "gemini_cli"} and bool(selected_api_key(candidate)))
        for candidate in _provider_candidates(settings)
    )


def provider_base_url(settings: dict[str, Any]) -> str:
    provider = selected_provider(settings)
    if provider in {"codex", "gemini_cli"}:
        return "gemini-cli://protected-runner" if provider == "gemini_cli" else "codex://protected-runtime"
    protocol = selected_protocol(settings)
    configured = str(settings.get("base_url") or "").strip().rstrip("/")
    if configured and (
        provider == "openai_compatible"
        or provider not in AI_PROVIDERS
        or protocol in {"openai_responses", "vertex_generate"}
    ):
        return configured
    if provider == "openai_compatible":
        return "https://api.openai.com/v1"
    if provider in OPENAI_STYLE_BASE_URLS:
        return OPENAI_STYLE_BASE_URLS[provider]
    if protocol == "anthropic_messages":
        return "https://api.anthropic.com/v1"
    if protocol == "gemini_generate":
        return "https://generativelanguage.googleapis.com/v1beta"
    if protocol == "vertex_generate":
        # Vertex AI express endpoint: a global host that authenticates with an
        # API key alone (no project/region). Express mode lives on v1beta1 — the
        # v1 path is project-scoped and 404s for express keys. Advanced users can
        # still override with a full project/region base URL.
        return "https://aiplatform.googleapis.com/v1beta1"
    return configured


def _call_provider(
    *,
    settings: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float = ANALYTICAL_TEMPERATURE,
    thinking_level: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> str:
    provider = selected_provider(settings)
    protocol = selected_protocol(settings)
    api_key = selected_api_key(settings)
    model = str(settings.get("model") or "").strip()
    if provider not in {"codex", "gemini_cli"} and (not api_key or not model):
        raise AiProviderError("Provider, API key, and model are required.")

    if provider == "gemini_cli" or protocol == "gemini_cli":
        from app.services.gemini_cli_provider import GeminiCliProvider
        try:
            content, usage = GeminiCliProvider.generate_structured(
                settings=settings,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_schema=json_schema,
                max_tokens=max_tokens,
                reasoning=thinking_level,
            )
        except Exception as exc:
            raise AiProviderError(str(exc), provider=provider, **getattr(exc, "context", {})) from exc
        return ProviderText(content, usage=usage)

    if provider == "codex" or protocol == "codex":
        from app.services.codex_provider import CodexProvider, CodexProviderError
        try:
            content, usage = CodexProvider.generate_structured(
                settings=settings,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                json_schema=json_schema,
                max_tokens=max_tokens,
                reasoning=thinking_level,
            )
        except CodexProviderError as exc:
            raise AiProviderError(str(exc), provider=provider, **exc.context) from exc
        return ProviderText(content, usage=usage)

    if protocol == "anthropic_messages":
        response = _request(
            "post",
            f"{provider_base_url(settings)}/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        return _response_text(response, protocol)

    if protocol in {"gemini_generate", "vertex_generate"}:
        normalized_model = model.removeprefix("models/")
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        }
        if thinking_level:
            if normalized_model.startswith("gemini-3"):
                generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
            elif normalized_model.startswith("gemini-2.5"):
                generation_config["thinkingConfig"] = {
                    "thinkingBudget": 1_024 if thinking_level == "low" else -1
                }
        if json_schema:
            generation_config["responseJsonSchema"] = _gemini_json_schema(json_schema)
        base_url = provider_base_url(settings)
        if protocol == "vertex_generate":
            # Vertex AI reaches Gemini through publisher-scoped model paths and
            # authenticates with an API key in the x-goog-api-key header. The base
            # URL defaults to the global express host, so no project/region setup
            # is required; a custom base URL adds the project/location prefix.
            url = (
                f"{base_url}/publishers/google/models/"
                f"{quote(normalized_model, safe='')}:generateContent"
            )
            headers = {
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            }
        else:
            url = (
                f"{base_url}/models/"
                f"{quote(normalized_model, safe='')}:generateContent?key={quote(api_key)}"
            )
            headers = {"Content-Type": "application/json"}
        response = _request(
            "post",
            url,
            headers=headers,
            json={
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "generationConfig": generation_config,
            },
        )
        return _response_text(response, protocol)

    if protocol == "openai_responses":
        response = _request(
            "post",
            f"{provider_base_url(settings)}/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "instructions": system_prompt,
                "input": user_prompt,
                "max_output_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        return _response_text(response, protocol)

    if protocol != "openai_chat":
        raise AiProviderError(f"Unsupported AI provider protocol: {protocol}.")
    chat_options: dict[str, Any] = {}
    host = urlsplit(provider_base_url(settings)).hostname or ""
    if (thinking_level == "minimal" and host.endswith(".aliyuncs.com")
            and model.lower().startswith(("deepseek-v4", "qwen"))):
        # Model Studio's hybrid models otherwise spend a short visitor reply's
        # output allowance on reasoning before producing any answer.
        chat_options["enable_thinking"] = False
    response = _request(
        "post",
        f"{provider_base_url(settings)}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            **chat_options,
        },
    )
    return _response_text(response, protocol)


def describe_image_json(
    settings: dict[str, Any],
    *,
    image_bytes: bytes,
    mime: str,
    prompt: str,
    json_schema: dict[str, Any] | None = None,
    max_tokens: int = 500,
) -> dict[str, Any]:
    """One vision call: ``prompt`` plus one image, answered as JSON.

    No fallback chain and no repair retry -- unlike `generate_ai_json`, the
    caller (a picture's own verify step) already retries at a higher level by
    drawing the picture again. Fails closed on any protocol that cannot carry
    an image.
    """
    provider = selected_provider(settings)
    protocol = selected_protocol(settings)
    api_key = selected_api_key(settings)
    model = str(settings.get("model") or "").strip()
    if provider == "gemini_cli" or selected_protocol(settings) == "gemini_cli":
        raise AiProviderError("Gemini CLI provides text only; it cannot read pictures.", failure_kind="unsupported_capability")
    if provider != "codex" and (not api_key or not model):
        raise AiProviderError("Provider, API key, and model are required.")
    if provider == "codex" or protocol == "codex":
        from app.services.codex_provider import CodexProvider, CodexProviderError
        try:
            content, _usage = CodexProvider.describe_image(
                settings=settings, prompt=prompt, image_bytes=image_bytes, mime=mime, json_schema=json_schema,
            )
        except CodexProviderError as exc:
            raise AiProviderError(str(exc), **exc.context) from exc
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise AiProviderError("Codex returned no JSON for the picture.") from exc
        if not isinstance(parsed, dict):
            raise AiProviderError("Codex returned no JSON object for the picture.")
        return parsed
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"

    if protocol == "openai_chat":
        response = _request(
            "post",
            f"{provider_base_url(settings)}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    },
                ],
                "max_tokens": max_tokens,
            },
        )
        content = _response_text(response, protocol)
    elif protocol in {"gemini_generate", "vertex_generate"}:
        normalized_model = model.removeprefix("models/")
        generation_config: dict[str, Any] = {
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        }
        if json_schema:
            generation_config["responseJsonSchema"] = _gemini_json_schema(json_schema)
        base_url = provider_base_url(settings)
        if protocol == "vertex_generate":
            url = (
                f"{base_url}/publishers/google/models/"
                f"{quote(normalized_model, safe='')}:generateContent"
            )
            headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        else:
            url = (
                f"{base_url}/models/"
                f"{quote(normalized_model, safe='')}:generateContent?key={quote(api_key)}"
            )
            headers = {"Content-Type": "application/json"}
        response = _request(
            "post",
            url,
            headers=headers,
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": prompt},
                            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode("ascii")}},
                        ],
                    },
                ],
                "generationConfig": generation_config,
            },
        )
        content = _response_text(response, protocol)
    else:
        raise AiProviderError(
            f"Vision calls are not supported for protocol {protocol!r}.", provider=provider,
        )

    try:
        return _parse_json_object(content)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AiProviderError(
            "AI provider returned non-JSON content.",
            failure_kind="non_json", provider=provider, content_preview=str(content)[:400],
        ) from exc


# Hosts this module ships as literals. They cannot be influenced by an
# operator, so they need no resolution — and skipping it keeps the DNS lookup
# off the path every normal AI call takes.
_SHIPPED_PROVIDER_HOSTS = frozenset(
    {urlsplit(url).hostname or "" for url in OPENAI_STYLE_BASE_URLS.values()}
    | {
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
        "aiplatform.googleapis.com",
    }
)


def assert_provider_url_allowed(url: str) -> None:
    """Refuse an operator-configured provider host on an internal address.

    A pasted base URL is an outbound request this app will make with its own
    network position, so `http://169.254.169.254/` or a loopback port turns the
    settings page into a probe of everything the deployment can reach. Only a
    custom host is checked: the shipped vendor hosts above are constants.

    A private address is *normal* for a self-hosted model server, so this is
    permitted off production and behind LYNEWRIGHT_AI_ALLOW_PRIVATE_BASE_URL on it.
    Unlike the visitor-facing page fetcher this does not pin the connection to
    the resolved IP, so it closes the direct case rather than a DNS rebind — the
    caller here is already an authenticated admin.
    """
    host = (urlsplit(url).hostname or "").strip().rstrip(".").lower()
    if not host or host in _SHIPPED_PROVIDER_HOSTS:
        return
    if get_settings().ai_allow_private_base_url:
        return
    try:
        resolve_public_target(url)
    except URLSecurityError as exc:
        raise AiProviderError(
            "The configured AI base URL points at a private or internal address.",
            failure_kind="blocked_base_url", endpoint=_safe_url(url),
            hint=(
                "Use a public provider host, or set "
                "LYNEWRIGHT_AI_ALLOW_PRIVATE_BASE_URL=true to allow a self-hosted "
                "model server on this network."
            ),
        ) from exc
    except DNSResolutionError as exc:
        raise AiProviderError(
            "The configured AI base URL could not be resolved.",
            failure_kind="connect", endpoint=_safe_url(url),
            hint="Check the base URL's hostname and this deployment's DNS.",
        ) from exc


def _request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    assert_provider_url_allowed(url)
    timeout = httpx.Timeout(
        connect=_timeout_setting(
            "LYNEWRIGHT_AI_CONNECT_TIMEOUT_SECONDS",
            _DEFAULT_CONNECT_TIMEOUT_SECONDS,
        ),
        read=_timeout_setting(
            "LYNEWRIGHT_AI_READ_TIMEOUT_SECONDS",
            _DEFAULT_READ_TIMEOUT_SECONDS,
        ),
        write=30.0,
        pool=10.0,
    )
    for attempt in range(2):
        try:
            response = (
                httpx.post(url, timeout=timeout, **kwargs)
                if method == "post"
                else httpx.get(url, timeout=timeout, **kwargs)
            )
        except httpx.TimeoutException as exc:
            if attempt == 0:
                time.sleep(0.25)
                continue
            raise AiProviderError(
                "AI provider timed out while waiting for a response. "
                "Retry the request or increase LYNEWRIGHT_AI_READ_TIMEOUT_SECONDS.",
                failure_kind="timeout", endpoint=_safe_url(url), attempts=attempt + 1,
                read_timeout_seconds=timeout.read, connect_timeout_seconds=timeout.connect,
                hint="Raise LYNEWRIGHT_AI_READ_TIMEOUT_SECONDS or ask for a smaller output.",
            ) from exc
        except httpx.ConnectError as exc:
            if attempt == 0:
                time.sleep(0.25)
                continue
            raise AiProviderError(
                "Could not connect to the AI provider. Check internet access and DNS, then retry.",
                failure_kind="connect", endpoint=_safe_url(url), attempts=attempt + 1,
                hint="The host is unreachable from this deployment: check egress, DNS and base URL.",
            ) from exc
        except httpx.HTTPError as exc:
            if attempt == 0:
                time.sleep(0.25)
                continue
            raise AiProviderError(
                f"AI provider request failed: {exc}",
                failure_kind="transport", endpoint=_safe_url(url), attempts=attempt + 1,
                transport_error=type(exc).__name__,
            ) from exc
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == 1:
            break
        retry_after = response.headers.get("retry-after", "")
        try:
            delay = min(max(float(retry_after), 0.0), 2.0)
        except ValueError:
            delay = 0.25
        time.sleep(delay)

    endpoint = _safe_url(url)
    # Everything an operator would otherwise have to reproduce by hand.
    http_context: dict[str, Any] = {
        "endpoint": endpoint,
        "attempts": attempt + 1,
        "provider_response": _error_detail(response),
        "retry_after": response.headers.get("retry-after", ""),
        "provider_request_id": (
            response.headers.get("x-request-id")
            or response.headers.get("request-id")
            or response.headers.get("apim-request-id", "")
        ),
    }
    if response.status_code in {401, 403}:
        raise AiProviderError(
            f"AI provider rejected the API key or permissions "
            f"(HTTP {response.status_code}) at {endpoint}: {_error_detail(response)}",
            status_code=response.status_code, failure_kind="auth_rejected",
            hint="The provider rejected this key. Re-check it in Settings → AI providers.",
            **http_context,
        )
    if response.status_code == 429:
        raise AiProviderError(
            f"AI provider rate-limited the request (HTTP 429) at {endpoint}: {_error_detail(response)}",
            status_code=response.status_code, failure_kind="rate_limited",
            hint="Quota or rate limit reached. Slow the request rate or raise the provider quota.",
            **http_context,
        )
    if response.status_code >= 400:
        raise AiProviderError(
            f"AI provider returned HTTP {response.status_code} at {endpoint}: "
            f"{_error_detail(response)}",
            status_code=response.status_code,
            failure_kind="server_error" if response.status_code >= 500 else "request_rejected",
            hint=(
                "The provider is unavailable or overloaded; the deterministic fallback ran instead."
                if response.status_code >= 500
                else "The provider rejected the request payload."
            ),
            **http_context,
        )
    response.extensions["lynewright_http_attempts"] = attempt + 1
    response.extensions["lynewright_provider_request_id"] = http_context["provider_request_id"]
    return response


def _timeout_setting(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return min(max(float(raw), 1.0), 600.0)
    except ValueError:
        return default


def _normalized_usage(payload: Any, protocol: str) -> dict[str, int]:
    """Normalize provider usage fields without retaining provider payloads."""
    usage: Any = payload.get("usage") if isinstance(payload, dict) else None
    if protocol in {"gemini_generate", "vertex_generate"}:
        usage = payload.get("usageMetadata") if isinstance(payload, dict) else None
        mapping = {
            "input_tokens": "promptTokenCount",
            "output_tokens": "candidatesTokenCount",
            "cached_input_tokens": "cachedContentTokenCount",
            "reasoning_tokens": "thoughtsTokenCount",
            "total_tokens": "totalTokenCount",
        }
    elif protocol == "anthropic_messages":
        mapping = {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cached_input_tokens": "cache_read_input_tokens",
            "reasoning_tokens": "reasoning_tokens",
        }
    else:
        mapping = {
            "input_tokens": "prompt_tokens",
            "output_tokens": "completion_tokens",
            "cached_input_tokens": "cached_tokens",
            "reasoning_tokens": "reasoning_tokens",
        }
        if isinstance(usage, dict) and isinstance(usage.get("input_tokens_details"), dict):
            mapping["cached_input_tokens"] = "input_tokens_details.cached_tokens"
        if isinstance(usage, dict) and isinstance(usage.get("output_tokens_details"), dict):
            mapping["reasoning_tokens"] = "output_tokens_details.reasoning_tokens"
    result: dict[str, int] = {}
    for name, source in mapping.items():
        value: Any = usage
        for part in source.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        try:
            if value is not None:
                result[name] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    if result:
        result["exact"] = 1
    return result


def _provider_text(payload: Any, text: str, protocol: str) -> ProviderText:
    return ProviderText(text, _normalized_usage(payload, protocol))


def _response_text(response: httpx.Response, protocol: str) -> ProviderText:
    payload: Any = None

    def with_transport(text: str) -> ProviderText:
        result = _provider_text(payload, text, protocol)
        result.usage["http_attempts"] = int(response.extensions.get("lynewright_http_attempts") or 1)
        request_id = str(response.extensions.get("lynewright_provider_request_id") or "").strip()
        if request_id:
            result.usage["provider_request_id"] = request_id[:160]
        return result

    def usage_snapshot() -> dict[str, Any]:
        usage = dict(_normalized_usage(payload, protocol)) if isinstance(payload, dict) else {}
        usage["http_attempts"] = int(response.extensions.get("lynewright_http_attempts") or 1)
        request_id = str(response.extensions.get("lynewright_provider_request_id") or "").strip()
        if request_id:
            usage["provider_request_id"] = request_id[:160]
        return usage

    try:
        payload = response.json()
        if protocol == "anthropic_messages":
            if payload.get("stop_reason") == "max_tokens":
                raise AiProviderError(
                    "AI provider response was truncated at the output limit. Request a smaller change.",
                    failure_kind="truncated", protocol=protocol,
                    usage=usage_snapshot(),
                    hint="The model hit its output cap: request fewer items or raise max_tokens.",
                )
            return with_transport(str(payload["content"][0]["text"] or ""))
        if protocol in {"gemini_generate", "vertex_generate"}:
            candidate = payload["candidates"][0]
            if candidate.get("finishReason") == "MAX_TOKENS":
                raise AiProviderError(
                    "AI provider response was truncated at the output limit. Request a smaller change.",
                    failure_kind="truncated", protocol=protocol,
                    usage=usage_snapshot(),
                    hint="The model hit its output cap: request fewer items or raise max_tokens.",
                )
            parts = candidate["content"]["parts"]
            return with_transport("".join(
                str(part.get("text") or "")
                for part in parts
                if isinstance(part, dict) and not part.get("thought")
            ))
        if protocol == "openai_responses":
            if payload.get("status") == "incomplete":
                raise AiProviderError(
                    "AI provider response was truncated at the output limit. Request a smaller change.",
                    failure_kind="truncated", protocol=protocol,
                    usage=usage_snapshot(),
                    hint="The model hit its output cap: request fewer items or raise max_tokens.",
                )
            text = str(payload.get("output_text") or "")
            if text:
                return with_transport(text)
            for item in payload.get("output") or []:
                for content in item.get("content") or []:
                    if content.get("type") == "output_text" and content.get("text"):
                        return with_transport(str(content["text"]))
            raise AiProviderError(
                "AI provider response did not include output text.",
                failure_kind="no_output_text", protocol=protocol,
                usage=usage_snapshot(),
                response_body=_payload_preview(payload),
            )
        if payload["choices"][0].get("finish_reason") == "length":
            raise AiProviderError(
                "AI provider response was truncated at the output limit. Request a smaller change.",
                failure_kind="truncated", protocol=protocol,
                usage=usage_snapshot(),
                hint="The model hit its output cap: request fewer items or raise max_tokens.",
            )
        return with_transport(str(payload["choices"][0]["message"]["content"] or ""))
    except AiProviderError:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AiProviderError(
            "AI provider response did not match the expected format.",
            failure_kind="malformed_response", protocol=protocol,
            usage=usage_snapshot(),
            missing_field=str(exc)[:200],
            response_body=_payload_preview(payload),
            hint="The provider answered in a different protocol's shape; check the configured protocol.",
        ) from exc


def _safe_url(url: Any) -> str:
    # Gemini authenticates with a ?key= query parameter; never surface it in
    # error messages or the Application Logs.
    return re.sub(r"([?&]key=)[^&]+", r"\1REDACTED", str(url))


def _payload_preview(payload: Any, limit: int = 700) -> str:
    """A bounded view of a provider payload that did not parse as expected.

    "did not match the expected format" is unactionable on its own; the shape the
    provider actually returned is what identifies the protocol mismatch.
    """
    if payload is None:
        return ""
    try:
        return json.dumps(payload, default=str)[:limit]
    except Exception:
        return str(payload)[:limit]


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        if text:
            return text[:500]
        content_type = response.headers.get("content-type", "unknown")
        return f"empty response body (content-type: {content_type})"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        detail = str(error.get("message") or "").strip()
        status = str(error.get("status") or "").strip()
        if detail and status:
            return f"{detail} ({status})"[:500]
        return (detail or str(error))[:500]
    return (str(error) if error else json.dumps(payload))[:500]


def _strip_json_fence(content: str) -> str:
    value = content.strip()
    if value.startswith("```json"):
        value = value.removeprefix("```json").strip()
    elif value.startswith("```"):
        value = value.removeprefix("```").strip()
    if value.endswith("```"):
        value = value.removesuffix("```").strip()
    return value


def _parse_json_object(content: str) -> dict[str, Any]:
    value = _strip_json_fence(content)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as original_error:
        start = value.find("{")
        if start < 0:
            raise original_error
        try:
            parsed, _ = json.JSONDecoder().raw_decode(value[start:])
        except json.JSONDecodeError:
            raise original_error
    if not isinstance(parsed, dict):
        raise ValueError("JSON response is not an object")
    return parsed


def _gemini_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    supported = {
        "$defs", "$ref", "type", "format", "title", "description", "enum",
        "items", "prefixItems", "properties", "required", "additionalProperties",
        "anyOf", "oneOf", "allOf", "nullable",
    }

    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, dict):
            return value
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key not in supported:
                continue
            if key in {"properties", "$defs"} and isinstance(item, dict):
                result[key] = {name: clean(child) for name, child in item.items()}
            else:
                result[key] = clean(item)
        return result

    return clean(schema)


def _open_provider_circuit(provider: str, message: str) -> None:
    _PROVIDER_CIRCUIT[provider] = (time.monotonic() + _CIRCUIT_SECONDS, message[:500])
