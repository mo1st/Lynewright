"""Decision backend boundary for the durable Operator loop.

Backends choose a decision only.  They never execute a Lynewright tool.  Keeping
this boundary small allows the legacy provider path to remain a safe rollback
while the optional PydanticAI adapter is qualified.
"""
from __future__ import annotations

import os
from typing import Any, Protocol

from app.services.ai_provider import ANALYTICAL_TEMPERATURE, generate_ai_json


class OperatorDecisionBackend(Protocol):
    name: str

    def decide(self, *, settings: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """Return exactly one decision dictionary or raise a provider error."""


class LegacyJSONDecisionBackend:
    name = "legacy"

    def __init__(self, *, system_prompt: str, json_schema: dict[str, Any]) -> None:
        self.system_prompt = system_prompt
        self.json_schema = json_schema

    def decide(self, *, settings: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        import json

        return generate_ai_json(
            settings=settings,
            system_prompt=self.system_prompt,
            user_prompt=json.dumps(payload, ensure_ascii=False, default=str),
            max_tokens=int(settings.get("_operator_decision_max_tokens") or 768),
            temperature=ANALYTICAL_TEMPERATURE,
            json_schema=self.json_schema,
        )


def configured_decision_backend(*, system_prompt: str, json_schema: dict[str, Any]) -> OperatorDecisionBackend:
    """Build the selected backend without making startup depend on PydanticAI."""

    mode = str(os.getenv("LYNEWRIGHT_OPERATOR_DECISION_BACKEND", "legacy") or "legacy").strip().lower()
    requested_needle = mode == "needle"
    judge_backend: OperatorDecisionBackend | None = None
    if requested_needle:
        # Needle is an optional local-first wrapper.  Its fallback is selected
        # explicitly here, so an unavailable or invalid local model can never
        # make the Operator unavailable.
        mode = "legacy"
        base: OperatorDecisionBackend
        base = LegacyJSONDecisionBackend(system_prompt=system_prompt, json_schema=json_schema)
        from app.services.operator_runtime.needle_backend import NeedleDecisionBackend, configured_needle_mode

        needle_mode = configured_needle_mode()
        if needle_mode == "off":
            needle_mode = "canary"
        if needle_mode in {"shadow", "canary"}:
            from app.services.operator_runtime.pydantic_ai_backend import PydanticAIDecisionBackend

            judge_backend = PydanticAIDecisionBackend(
                system_prompt=system_prompt, json_schema=json_schema,
                task_type="operator_judge",
            )
            return NeedleDecisionBackend(
                fallback=base, judge_fallback=judge_backend,
                sensitive_fallback=judge_backend,
                system_prompt=system_prompt, json_schema=json_schema, mode=needle_mode,
            )
    if mode == "pydantic_ai":
        from app.services.operator_runtime.pydantic_ai_backend import PydanticAIDecisionBackend

        base = PydanticAIDecisionBackend(system_prompt=system_prompt, json_schema=json_schema)
    else:
        base = LegacyJSONDecisionBackend(system_prompt=system_prompt, json_schema=json_schema)

    from app.services.operator_runtime.needle_backend import NeedleDecisionBackend, configured_needle_mode

    needle_mode = configured_needle_mode()
    if needle_mode in {"shadow", "canary"}:
        from app.services.operator_runtime.pydantic_ai_backend import PydanticAIDecisionBackend

        # All Needle fallbacks use the independently configured judge route.
        # This prevents a failed local proposal from silently returning to the
        # ordinary (often cheaper) operator-agent model.  Sensitive operations
        # use the same route but are marked for mandatory semantic verification.
        judge_backend = PydanticAIDecisionBackend(
            system_prompt=system_prompt, json_schema=json_schema,
            task_type="operator_judge",
        )
        return NeedleDecisionBackend(
            fallback=base, judge_fallback=judge_backend,
            sensitive_fallback=judge_backend,
            system_prompt=system_prompt, json_schema=json_schema, mode=needle_mode,
        )
    return base


__all__ = [
    "LegacyJSONDecisionBackend",
    "OperatorDecisionBackend",
    "configured_decision_backend",
]
