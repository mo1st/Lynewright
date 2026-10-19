"""Text-only provider that invokes Google's official Gemini CLI in the runner."""
from __future__ import annotations

import json
import os
import time
from typing import Any

from app.services.codex_provider import (
    CodexProviderError,
    _bridge_enabled,
    _bridge_generate,
    _bridge_request,
)


def _settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    selected = dict(settings or {})
    selected["provider"] = "gemini_cli"
    selected["protocol"] = "gemini_cli"
    selected["codex_runner_cli_backend"] = "gemini"
    enabled = selected.get("gemini_cli_runner_enabled")
    if enabled is None:
        enabled = os.getenv("LYNEWRIGHT_GEMINI_CLI_RUNNER_ENABLED", "")
    selected["codex_runner_enabled"] = str(enabled).strip().lower() in {"1", "true", "yes", "on"}
    return selected


class GeminiCliProvider:
    """Safe structured-text adapter; auth state remains inside Gemini CLI."""

    @classmethod
    def is_enabled(cls, settings: dict[str, Any] | None = None) -> bool:
        return _bridge_enabled(_settings(settings))

    @classmethod
    def health(cls, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        selected = _settings(settings)
        if not _bridge_enabled(selected):
            raise CodexProviderError("The protected Gemini CLI runner is disabled.", failure_kind="configuration")
        started = time.monotonic()
        value = _bridge_request(selected, "/health", timeout=10)
        if value.get("status") != "ok":
            raise CodexProviderError("The protected Gemini CLI runner health check failed.", failure_kind="runner_unavailable")
        usage = _usage_status()
        return {
            "ok": True,
            "enabled": True,
            # The official CLI exposes interactive auth management, but no
            # supported noninteractive account identity/status command.
            "authenticated": None,
            "account_status": "not_exposed_by_cli",
            "mode": "official_cli",
            "model": str(selected.get("model") or "Gemini CLI default (auto)")[:160],
            "usage": usage,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }

    @classmethod
    def list_models(cls, settings: dict[str, Any] | None = None) -> list[dict[str, str]]:
        # The CLI's own default is intentionally retained. Listing account
        # models would require an undocumented/private interface or a model call.
        return [{"id": "auto", "name": "Gemini CLI default (auto)"}]

    @classmethod
    def generate_structured(
        cls,
        *,
        settings: dict[str, Any],
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None,
        max_tokens: int,
        reasoning: str | None = None,
        cwd: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        selected = _settings(settings)
        if not _bridge_enabled(selected):
            raise CodexProviderError("The protected Gemini CLI runner is disabled.", failure_kind="configuration")
        content, usage = _bridge_generate(
            selected,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_schema=json_schema,
        )
        _validate_schema_output(content, json_schema)
        return content, usage


def _usage_status() -> dict[str, Any]:
    try:
        from app.services.codex_usage import usage_status

        return usage_status("gemini_cli")
    except Exception:
        return {"text_calls": 0, "text_cap": 60, "date": ""}


def _validate_schema_output(content: str, schema: dict[str, Any] | None) -> None:
    """Validate the final object against the exact schema supplied to the CLI."""

    if not isinstance(schema, dict):
        raise CodexProviderError("Gemini CLI structured output requires a JSON schema.", failure_kind="invalid_request")
    try:
        result = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise CodexProviderError("Gemini CLI returned invalid JSON.", failure_kind="invalid_output") from exc
    try:
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(result), key=lambda item: tuple(str(part) for part in item.path))
    except Exception as exc:
        raise CodexProviderError("The structured output schema could not be validated.", failure_kind="invalid_request") from exc
    if errors:
        details = [
            {"path": ".".join(str(part) for part in item.path) or "(root)", "message": item.message[:160]}
            for item in errors[:5]
        ]
        raise CodexProviderError(
            "Gemini CLI returned JSON that does not match the requested schema.",
            failure_kind="invalid_output", validation_errors=details,
        )

