from __future__ import annotations

import hashlib
import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.database import get_ai_settings, get_latest_ai_artifact, insert_ai_artifact
from app.services.ai_provider import (
    ANALYTICAL_TEMPERATURE,
    CREATIVE_TEMPERATURE,
    AiProviderError,
    active_provider,
    generate_ai_json,
)
from app.services.application_logging import log_event

T = TypeVar("T", bound=BaseModel)


def stable_hash(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())) or "(root)",
            "type": error.get("type", ""),
            "message": str(error.get("msg", ""))[:200],
        }
        for error in exc.errors()[:10]
    ]


# Enough of the rejected answer for the model to repair it rather than start over,
# without re-spending the whole output budget on echoing itself back.
_REPAIR_ECHO_LIMIT = 4_000


def _schema_repair_prompt(exc: ValidationError, rejected: Any) -> str:
    problems = "\n".join(
        f"- {error['field']}: {error['message']} ({error['type']})"
        for error in _validation_errors(exc)
    )
    echo = json.dumps(rejected, ensure_ascii=False, default=str)[:_REPAIR_ECHO_LIMIT]
    return (
        "Your previous answer was rejected by the required JSON schema. This was it:\n"
        f"{echo}\n\n"
        "These fields failed validation:\n"
        f"{problems}\n\n"
        "Return the same content again as one JSON object with those fields corrected. Keep every "
        "line you already wrote — this is a repair, not a rewrite — and do not drop content to make "
        "it validate."
    )


def generate_structured_artifact(
    *,
    artifact_type: str,
    context: dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    schema_model: type[T],
    fallback: dict[str, Any],
    use_cache: bool = True,
    provider_settings: dict[str, Any] | None = None,
    project_id: str,
    tenant_id: str = "ten_default",
    parent_artifact_id: int | None = None,
    max_tokens: int = 3_500,
    temperature: float = ANALYTICAL_TEMPERATURE,
    thinking_level: str | None = "low",
) -> dict[str, Any]:
    schema = schema_model.model_json_schema()
    effective_system_prompt = (
        f"{system_prompt.rstrip()}\n\n"
        "Return exactly one JSON object that validates against the supplied JSON schema. "
        "Use the schema's exact top-level field names and include every required field. "
        "Do not wrap the object in markdown or add commentary."
    )
    effective_user_prompt = (
        f"{user_prompt.rstrip()}\n\n"
        f"Required response JSON schema:\n{json.dumps(schema, ensure_ascii=False)}"
    )
    hash_payload: dict[str, Any] = {
        "artifact_type": artifact_type,
        "context": context,
        "system_prompt": effective_system_prompt,
        "user_prompt": effective_user_prompt,
        "schema": schema,
        "max_tokens": max_tokens,
        "thinking_level": thinking_level,
    }
    # Temperature belongs in the provenance, but adding the key unconditionally
    # would re-hash every artifact ever cached — including cached metadata enrichment,
    # which would then re-spend a provider call per product. Only a deviation
    # from the analytical default is recorded, so existing hashes are untouched.
    if temperature != ANALYTICAL_TEMPERATURE:
        hash_payload["temperature"] = temperature
    input_hash = stable_hash(hash_payload)

    if use_cache:
        cached = get_latest_ai_artifact(
            artifact_type=artifact_type,
            input_hash=input_hash,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if cached:
            return cached["artifact"]

    settings = provider_settings or get_ai_settings()
    provider = active_provider(settings)
    ai_error: str | None = None
    artifact = fallback
    repair_attempted = False

    try:
        raw = generate_ai_json(
            settings=settings,
            system_prompt=effective_system_prompt,
            user_prompt=effective_user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking_level=thinking_level,
            json_schema=schema,
        )
        provider = str(settings.get("_last_provider_used") or provider)
        try:
            artifact = schema_model.model_validate(raw).model_dump()
        except ValidationError as invalid:
            # One re-ask, and only for schema failures. A generation can be good
            # copy that misnames a single field, and dropping straight to the
            # canned fallback throws the whole thing away. Transport and provider
            # errors are deliberately excluded — the circuit breaker owns those,
            # and retrying an overloaded provider only deepens the outage.
            repair_attempted = True
            log_event(
                level="info", category="ai_fallback", source=f"ai_json.{artifact_type}",
                message=f"{artifact_type} failed schema validation; re-asking once",
                details={
                    "artifact_type": artifact_type,
                    "schema": schema_model.__name__,
                    "failure_kind": "schema_validation",
                    "validation_errors": _validation_errors(invalid),
                    "input_hash": input_hash,
                    "hint": "The provider answered valid JSON in the wrong shape; the rejected fields were quoted back for one repair attempt.",
                },
                capture_active_exception=False,
            )
            raw = generate_ai_json(
                settings=settings,
                system_prompt=effective_system_prompt,
                user_prompt=(
                    f"{effective_user_prompt}\n\n{_schema_repair_prompt(invalid, raw)}"
                ),
                max_tokens=max_tokens,
                temperature=temperature,
                thinking_level=thinking_level,
                json_schema=schema,
            )
            provider = str(settings.get("_last_provider_used") or provider)
            artifact = schema_model.model_validate(raw).model_dump()
    except (AiProviderError, ValidationError, ValueError) as exc:
        ai_error = str(exc)
        artifact = schema_model.model_validate(fallback).model_dump()
        provider = "mock"
        # A fallback is a *silent* quality regression: the operator gets a valid
        # artifact and no visible failure. The log is therefore the only place
        # that can say which stage failed and whether it was the provider or us.
        details: dict[str, Any] = {
            "artifact_type": artifact_type,
            "configured_provider": active_provider(settings),
            "configured_model": str(settings.get("model") or ""),
            "schema": schema_model.__name__,
            "temperature": temperature,
            "fallback_stage": (
                "provider_call" if isinstance(exc, AiProviderError) else "schema_validation"
            ),
            # Distinguishes "the model got it wrong once" from "it got it wrong
            # twice, with the errors quoted back" — only the second is a prompt
            # or schema defect worth acting on.
            "repair_attempted": repair_attempted,
            "input_hash": input_hash,
        }
        if isinstance(exc, AiProviderError):
            details.update(exc.context)
        elif isinstance(exc, ValidationError):
            # Which field the model got wrong, not just "validation failed".
            details["failure_kind"] = "schema_validation"
            details["validation_errors"] = _validation_errors(exc)
            details["hint"] = (
                "The model answered valid JSON in the wrong shape twice — the second time with the "
                "rejected fields quoted back — so the prompt or schema needs tightening."
                if repair_attempted else
                "The model answered valid JSON in the wrong shape; the prompt or schema needs tightening."
            )
        log_event(
            level="warning", category="ai_fallback", source=f"ai_json.{artifact_type}",
            message=f"{artifact_type} used deterministic fallback: {exc}",
            details=details, exc=exc,
        )

    # Surface whether deterministic fallback was used so the UI can label it.
    if "used_fallback" in schema_model.model_fields:
        artifact["used_fallback"] = ai_error is not None

    confidence = float(artifact.get("confidence") or 0)
    stored = insert_ai_artifact(
        artifact_type=artifact_type,
        artifact=artifact,
        input_hash=input_hash,
        provider_used=provider,
        ai_error=ai_error,
        project_id=project_id,
        tenant_id=tenant_id,
        confidence=confidence,
        parent_artifact_id=parent_artifact_id,
    )
    # Hand back the row we just wrote, on the same convention as `used_fallback`.
    # Callers used to recover it with `ORDER BY id DESC LIMIT 1`, which returns
    # *someone else's* row when two generations for the same resource overlap —
    # and the caller then overwrites that row's artifact_json.
    if "artifact_id" in schema_model.model_fields:
        artifact["artifact_id"] = int(stored["id"])
    return artifact
