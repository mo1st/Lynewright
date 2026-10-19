"""Typed decision adapter backed by Lynewright's existing provider boundary.

PydanticAI is intentionally not placed between the provider and the durable
Operator loop.  The previous ``FunctionModel`` shim wrapped a provider JSON
object in PydanticAI's internal output envelope; with some PydanticAI versions
that envelope was interpreted as a validation error and the same request was
replayed.  Lynewright already owns provider routing, credentials, retries and
usage accounting, so this adapter asks that boundary for one JSON object and
validates it once against the shared discriminated decision union.
"""
from __future__ import annotations

import json
from typing import Any

from app.services.ai_provider import ANALYTICAL_TEMPERATURE, AiProviderError, generate_ai_json
from app.services.ai_task_provider import get_ai_settings_for_task
from app.services.operator_runtime.decisions import decision_json_schema, validate_decision


class PydanticAIDecisionBackend:
    name = "pydantic_ai"

    def __init__(
        self, *, system_prompt: str, json_schema: dict[str, Any],
        task_type: str = "operator_agent",
    ) -> None:
        self.system_prompt = system_prompt
        self.task_type = task_type
        # Prefer the strict typed union. ``json_schema`` is retained as a
        # compatibility argument for callers that construct the backend with
        # the legacy loop constant.
        self.json_schema = decision_json_schema() or json_schema

    def decide(self, *, settings: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        user_prompt = json.dumps(payload, ensure_ascii=False, default=str)
        effective_settings = settings
        if self.task_type != "operator_agent":
            # Judge requests must use the independently configured task route.
            # Copy only run-local accounting and bounded decision controls; do
            # not leak the ordinary operator route's provider credentials or
            # fallback chain into the judge request.
            effective_settings = get_ai_settings_for_task(self.task_type)
            for key in (
                "_operator_run_id", "_operator_decision_max_tokens",
                "_operator_plan_max_tokens", "_operator_decision_policy",
                "_needle_candidate", "_fallback_reason",
            ):
                if key in settings:
                    effective_settings[key] = settings[key]
            effective_settings["_max_provider_attempts"] = 1
            effective_settings["fallback_chain"] = []
        try:
            raw = generate_ai_json(
                settings=effective_settings,
                system_prompt=self.system_prompt,
                user_prompt=user_prompt,
                max_tokens=int(effective_settings.get("_operator_decision_max_tokens") or 1024),
                temperature=ANALYTICAL_TEMPERATURE,
                json_schema=self.json_schema,
            )
            if effective_settings is not settings:
                for key in (
                    "_last_provider_used", "_last_provider_profile_id", "_last_provider_model",
                    "_last_provider_protocol", "_last_provider_usage", "_last_provider_attempt_count",
                    "_last_provider_attempt_records", "_last_prompt_chars", "_last_input_token_estimate",
                ):
                    if key in effective_settings:
                        settings[key] = effective_settings[key]
                settings["_decision_backend"] = "operator_judge"
                settings["_judge_provider"] = str(effective_settings.get("_last_provider_used") or "")
                settings["_judge_model"] = str(effective_settings.get("_last_provider_model") or "")
                settings["_judge_usage"] = dict(effective_settings.get("_last_provider_usage") or {})
                settings["_judge_attempt_count"] = int(effective_settings.get("_last_provider_attempt_count") or 0)
            return validate_decision(raw)
        except AiProviderError:
            if effective_settings is not settings:
                for key in (
                    "_last_provider_used", "_last_provider_profile_id", "_last_provider_model",
                    "_last_provider_protocol", "_last_provider_usage", "_last_provider_attempt_count",
                    "_last_provider_attempt_records", "_last_prompt_chars", "_last_input_token_estimate",
                ):
                    if key in effective_settings:
                        settings[key] = effective_settings[key]
                settings["_decision_backend"] = "operator_judge"
                settings["_judge_provider"] = str(effective_settings.get("_last_provider_used") or "")
                settings["_judge_model"] = str(effective_settings.get("_last_provider_model") or "")
                settings["_judge_usage"] = dict(effective_settings.get("_last_provider_usage") or {})
                settings["_judge_attempt_count"] = int(effective_settings.get("_last_provider_attempt_count") or 0)
            raise
        except Exception as exc:
            raise AiProviderError(
                f"PydanticAI decision validation failed: {str(exc)[:500]}",
                failure_kind="decision_validation",
            ) from exc


__all__ = ["PydanticAIDecisionBackend"]
