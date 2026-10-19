from __future__ import annotations
from contextlib import closing

import os
import sqlite3
from typing import Any

from app.database import get_ai_settings, get_connection, init_db
from app.models.settings import mask_key, sanitize_secret_input
from app.services.ai_provider import selected_protocol, selected_provider
from app.services.platform_services import CredentialService


TASK_TYPES = (
    "diagnosis", "tracking_builder", "ad_text", "ad_image", "code_builder",
    "experience_builder",
    "visitor_chat",
    "research_mining",
    "offer_builder",
    "results_copy",
    "email_copy",
    "product_recommendation",
    "site_product_mapping",
    # Internal Operator Agent orchestration loop. Resolves through the same
    # per-task BYOK provider/protocol/model configuration as every other task;
    # the operator runtime never builds a second provider framework.
    "operator_agent",
    "operator_judge",
)
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
DEFAULT_ENV_NAMES = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "vertex": "VERTEX_ACCESS_TOKEN",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "grok": "GROK_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}


CODEX_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


def get_default_provider_for_task(task_type: str) -> dict[str, Any]:
    if task_type == "ad_image":
        return {
            "task_type": task_type,
            "provider": "disabled",
            "protocol": "openai_chat",
            "model": "",
            "credential_mode": "raw_key",
            "api_key_env_name": "",
            "credential_ref": "",
            "api_key_value": "",
            "base_url": "",
            "enabled": False,
        }
    return {
        "task_type": task_type,
        # New routes inherit the app-wide Default AI provider. A specific
        # route persists an independent provider/key/model configuration.
        "provider": "default",
        "protocol": "openai_chat",
        "model": "",
        "credential_mode": "raw_key",
        "api_key_env_name": "",
        "credential_ref": "",
        "api_key_value": "",
        "base_url": "",
        "enabled": True,
    }


def get_provider_for_task(
    db: sqlite3.Connection, task_type: str,
) -> dict[str, Any]:
    if task_type not in TASK_TYPES:
        raise ValueError(f"Unsupported AI task type: {task_type}")
    stored = _read_route(db, task_type)
    if stored is None:
        return get_default_provider_for_task(task_type)
    return stored


def _read_route(db: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """The stored route row for ``key``, or None when nothing was saved."""
    row = db.execute(
        "SELECT * FROM ai_task_providers WHERE task_type = ? LIMIT 1",
        (key,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["enabled"] = bool(result.get("enabled"))
    return result


def list_task_providers(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [get_provider_for_task(db, task_type) for task_type in TASK_TYPES]


def mask_task_provider(provider: dict[str, Any]) -> dict[str, Any]:
    """Return provider settings without exposing a stored raw credential."""
    result = dict(provider)
    raw_key = str(result.pop("api_key_value", "") or "").strip()
    result["credential_ref"] = str(result.get("credential_ref") or "")
    credential_configured = bool(
        result["credential_ref"]
        and CredentialService().get_status(result["credential_ref"]).get("configured")
    )
    result["api_key_value_masked"] = (
        mask_key(raw_key) if raw_key else "configured" if credential_configured else ""
    )
    return result


def list_task_providers_masked(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [mask_task_provider(provider) for provider in list_task_providers(db)]


def save_provider_for_task(
    db: sqlite3.Connection,
    *,
    task_type: str,
    provider: str,
    model: str,
    credential_mode: str,
    api_key_env_name: str,
    api_key_value: str | None,
    base_url: str,
    enabled: bool,
    protocol: str | None = None,
    credential_ref: str | None = None,
) -> dict[str, Any]:
    if task_type not in TASK_TYPES:
        raise ValueError(f"Unsupported AI task type: {task_type}")
    _write_route(
        db, task_type,
        provider=provider, protocol=protocol, model=model,
        credential_mode=credential_mode, api_key_env_name=api_key_env_name,
        api_key_value=api_key_value, credential_ref=credential_ref,
        base_url=base_url, enabled=enabled,
    )
    return mask_task_provider(get_provider_for_task(db, task_type))


def _write_route(
    db: sqlite3.Connection,
    key: str,
    *,
    provider: str,
    protocol: str | None,
    model: str,
    credential_mode: str,
    api_key_env_name: str,
    api_key_value: str | None,
    credential_ref: str | None,
    base_url: str,
    enabled: bool,
    fallback_provider: str | None = None,
    fallback_model: str | None = None,
    reasoning_effort: str | None = None,
) -> None:
    """Validate and upsert one route row under ``key`` (a task type or a slot key)."""
    if credential_mode not in {"credential_ref", "env_var", "raw_key"}:
        raise ValueError(f"Unsupported credential mode: {credential_mode}")
    protocol = protocol or DEFAULT_PROTOCOL_BY_PROVIDER.get(provider, "openai_chat")
    if protocol not in PROVIDER_PROTOCOLS:
        raise ValueError(f"Unsupported provider protocol: {protocol}")
    # Codex is a provider, not a wire protocol for another provider: "gemini"
    # with protocol "codex" would silently run Codex with a Gemini model id.
    if (protocol in {"codex", "gemini_cli"} or provider.strip() in {"codex", "gemini_cli"}) and protocol != provider.strip():
        raise ValueError("Choose the matching CLI provider; its protocol cannot be used with another provider.")
    existing = _read_route(db, key) or {}
    existing_raw_key = (
        str(existing.get("api_key_value") or "")
        if existing.get("credential_mode") == "raw_key"
        else ""
    )
    existing_ref = str(existing.get("credential_ref") or "").strip()
    stored_ref = str(credential_ref or existing_ref).strip()
    if credential_mode == "credential_ref":
        if not stored_ref:
            raise ValueError("A CredentialRef is required for this provider route.")
        if not CredentialService().get_status(stored_ref).get("configured"):
            raise ValueError("The selected AI credential is not configured.")
    stored_env_name = api_key_env_name.strip() if credential_mode == "env_var" else ""
    stored_fallback_provider = str(
        existing.get("fallback_provider") or "" if fallback_provider is None else fallback_provider
    ).strip()
    stored_fallback_model = str(
        existing.get("fallback_model") or "" if fallback_model is None else fallback_model
    ).strip() if stored_fallback_provider else ""
    stored_raw_key = (
        sanitize_secret_input(api_key_value, existing_raw_key)
        if credential_mode == "raw_key"
        else ""
    )
    # CLI model names are bounded and checked again by the runner. The Gemini
    # `auto` alias follows the official CLI default; it is text only.
    model_value = model.strip()
    effort_value = str(reasoning_effort or "").strip().lower() if provider.strip() == "codex" else ""
    if effort_value and effort_value not in CODEX_REASONING_EFFORTS:
        raise ValueError(f"Unsupported reasoning effort: {effort_value}")
    db.execute(
        """INSERT INTO ai_task_providers (
               task_type, provider, protocol, model, credential_mode, api_key_env_name,
               api_key_value, credential_ref, base_url, fallback_provider, fallback_model,
               reasoning_effort, enabled
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(task_type) DO UPDATE SET
               provider = excluded.provider,
               protocol = excluded.protocol,
               model = excluded.model,
               credential_mode = excluded.credential_mode,
               api_key_env_name = excluded.api_key_env_name,
               api_key_value = excluded.api_key_value,
               credential_ref = excluded.credential_ref,
               base_url = excluded.base_url,
               fallback_provider = excluded.fallback_provider,
               fallback_model = excluded.fallback_model,
               reasoning_effort = excluded.reasoning_effort,
               enabled = excluded.enabled,
               updated_at = CURRENT_TIMESTAMP""",
        (
            key, provider.strip(), protocol, model_value, credential_mode,
            stored_env_name, stored_raw_key, stored_ref, base_url.strip(),
            stored_fallback_provider, stored_fallback_model, effort_value, int(enabled),
        ),
    )
    db.commit()


def resolve_provider_api_key(provider: dict[str, Any]) -> str:
    mode = str(provider.get("credential_mode") or "env_var")
    if mode == "raw_key":
        key = str(provider.get("api_key_value") or "").strip()
        if not key:
            raise ValueError("Raw API key is empty for this provider.")
        return key
    if mode == "credential_ref":
        ref = str(provider.get("credential_ref") or "").strip()
        if not ref:
            raise ValueError("AI CredentialRef is not configured.")
        try:
            return str(CredentialService().use(ref, lambda secret: {"value": secret})["value"] or "").strip()
        except Exception as exc:
            raise ValueError("The configured AI credential is unavailable.") from exc
    if mode != "env_var":
        raise ValueError(f"Unsupported credential mode: {mode}")
    env_name = str(provider.get("api_key_env_name") or "").strip()
    if not env_name:
        raise ValueError("API key environment variable name is not configured.")
    key = os.getenv(env_name, "").strip()
    if not key:
        raise ValueError(
            f"Environment variable {env_name} is not set in the backend process."
        )
    return key


def task_provider_ai_settings(
    provider: dict[str, Any], *, connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    provider_name = str(provider.get("provider") or "mock")
    if provider_name == "default":
        # General routes deliberately reuse the entire app-wide configuration
        # rather than copying an API key into every task record.
        settings = _get_ai_settings(connection)
        return {
            "provider": selected_provider(settings),
            "protocol": selected_protocol(settings),
            "model": str(settings.get("model") or "mock-brief-v1"),
            "base_url": str(settings.get("base_url") or ""),
            "api_key_env_name": "",
            "credential_ref": str(settings.get("credential_ref") or ""),
            "api_keys": settings.get("api_keys") if isinstance(settings.get("api_keys"), dict) else {},
            "profile_id": settings.get("profile_id"),
            "profile_name": settings.get("profile_name"),
            "fallback_chain": settings.get("fallback_chain") if isinstance(settings.get("fallback_chain"), list) else [],
        }
    protocol = str(provider.get("protocol") or DEFAULT_PROTOCOL_BY_PROVIDER.get(provider_name, "openai_chat"))
    if provider_name == "disabled":
        provider_name = "mock"
    stored = str(provider.get("api_key_env_name") or "").strip()
    api_key = ""
    if provider_name not in {"mock", "codex", "gemini_cli"}:
        try:
            api_key = resolve_provider_api_key(provider)
        except ValueError:
            # Generation callers need the provider error to occur inside
            # generate_ai_json so their deterministic fallback remains active.
            api_key = ""
        if not api_key:
            conventional = DEFAULT_ENV_NAMES.get(provider_name, "")
            api_key = os.getenv(conventional, "").strip() if conventional else ""
        if not api_key:
            global_keys = _get_ai_settings(connection).get("api_keys") or {}
            if isinstance(global_keys, dict):
                api_key = str(global_keys.get(provider_name) or "").strip()
    global_settings = _get_ai_settings(connection)
    global_chain = []
    if provider_name != "mock":
        global_chain = [
            {key: value for key, value in global_settings.items() if key != "fallback_chain"},
            *(
                global_settings.get("fallback_chain")
                if isinstance(global_settings.get("fallback_chain"), list) else []
            ),
        ]
    return {
        "provider": provider_name,
        "protocol": protocol,
        "model": str(provider.get("model") or "mock-brief-v1"),
        "base_url": str(provider.get("base_url") or ""),
        "api_key_env_name": stored,
        "credential_ref": str(provider.get("credential_ref") or "").strip(),
        "api_keys": {provider_name: api_key} if api_key else {},
        "reasoning_effort": str(provider.get("reasoning_effort") or ""),
        # A task-specific route is attempted first. The global priority chain
        # then remains available instead of dropping directly to templates.
        "fallback_chain": global_chain,
    }


def _get_ai_settings(connection: sqlite3.Connection | None) -> dict[str, Any]:
    # Keep the zero-argument call for existing integrations/tests while allowing
    # transactional domain tools to avoid a second SQLite connection.
    return get_ai_settings(connection) if connection is not None else get_ai_settings()


def get_ai_settings_for_task(task_type: str) -> dict[str, Any]:
    init_db()
    with closing(get_connection()) as db, db:
        return task_provider_ai_settings(get_provider_for_task(db, task_type))
