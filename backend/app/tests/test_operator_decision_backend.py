from __future__ import annotations

import pytest

from app.services.operator_runtime.decision_backend import (
    LegacyJSONDecisionBackend,
    configured_decision_backend,
)
from app.services.operator_runtime.decisions import validate_decision
from app.services.operator_runtime.pydantic_ai_backend import PydanticAIDecisionBackend
from app.services.operator_runtime.needle_backend import NeedleDecisionBackend
from app.services.operator_runtime import needle_backend as needle_module


def test_strict_decision_union_rejects_unknown_fields() -> None:
    assert validate_decision({"type": "finish", "message": "done"}) == {
        "type": "finish", "message": "done",
    }
    with pytest.raises(Exception):
        validate_decision({"type": "finish", "message": "done", "unknown": True})


def test_legacy_backend_is_default(monkeypatch) -> None:
    monkeypatch.delenv("LYNEWRIGHT_OPERATOR_DECISION_BACKEND", raising=False)
    backend = configured_decision_backend(system_prompt="system", json_schema={"type": "object"})
    assert isinstance(backend, LegacyJSONDecisionBackend)


def test_pydantic_ai_backend_uses_lynewright_provider_dispatch(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_generate(**kwargs):
        calls.append(kwargs)
        kwargs["settings"]["_last_provider_usage"] = {
            "input_tokens": 18, "output_tokens": 7, "exact": 1,
        }
        kwargs["settings"]["_last_provider_model"] = "fixture-model"
        kwargs["settings"]["_last_provider_protocol"] = "openai_chat"
        return {"type": "finish", "message": "done"}

    monkeypatch.setattr(
        "app.services.operator_runtime.pydantic_ai_backend.generate_ai_json",
        fake_generate,
    )
    backend = PydanticAIDecisionBackend(system_prompt="system", json_schema={"type": "object"})
    settings = {"_operator_decision_max_tokens": 128}
    decision = backend.decide(settings=settings, payload={"objective": "finish"})

    assert decision == {"type": "finish", "message": "done"}
    assert len(calls) == 1
    assert calls[0]["settings"] is settings
    assert calls[0]["max_tokens"] == 128
    assert calls[0]["json_schema"]["$defs"]


def test_pydantic_ai_backend_validates_action_plan_union(monkeypatch) -> None:
    def fake_generate(**kwargs):
        return {
            "type": "action_plan",
            "message": "complete the workflow",
            "steps": [
                {"step_id": "one", "tool": "operations.search", "arguments": {}, "bindings": []},
                {"step_id": "two", "tool": "media.prepare", "arguments": {}, "bindings": []},
            ],
        }

    monkeypatch.setattr(
        "app.services.operator_runtime.pydantic_ai_backend.generate_ai_json",
        fake_generate,
    )
    backend = PydanticAIDecisionBackend(system_prompt="system", json_schema={"type": "object"})
    decision = backend.decide(settings={"_operator_decision_max_tokens": 128}, payload={"decision_mode": "action_plan"})
    assert decision["type"] == "action_plan"
    assert [step["step_id"] for step in decision["steps"]] == ["one", "two"]


def test_pydantic_ai_backend_uses_one_typed_provider_request(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_generate(**kwargs):
        calls.append(kwargs)
        kwargs["settings"]["_last_provider_usage"] = {"input_tokens": 9, "output_tokens": 4, "exact": 1}
        return {"type": "finish", "message": "typed"}

    monkeypatch.setattr(
        "app.services.operator_runtime.pydantic_ai_backend.generate_ai_json", fake_generate,
    )
    backend = PydanticAIDecisionBackend(system_prompt="system", json_schema={"type": "object"})
    assert backend.decide(settings={"_operator_decision_max_tokens": 128}, payload={}) == {
        "type": "finish", "message": "typed",
    }
    assert len(calls) == 1


def test_judge_backend_loads_the_separate_operator_judge_route(monkeypatch) -> None:
    calls: list[dict] = []

    monkeypatch.setattr(
        "app.services.operator_runtime.pydantic_ai_backend.get_ai_settings_for_task",
        lambda task: {
            "provider": "openai", "model": "large-judge", "protocol": "openai_chat",
            "api_keys": {"openai": "judge-key"}, "fallback_chain": [],
        } if task == "operator_judge" else {},
    )

    def fake_generate(**kwargs):
        calls.append(kwargs)
        kwargs["settings"]["_last_provider_used"] = "openai"
        kwargs["settings"]["_last_provider_model"] = "large-judge"
        kwargs["settings"]["_last_provider_protocol"] = "openai_chat"
        kwargs["settings"]["_last_provider_usage"] = {"input_tokens": 12, "output_tokens": 3, "exact": 1}
        kwargs["settings"]["_last_provider_attempt_count"] = 1
        return {"type": "finish", "message": "judge verified"}

    monkeypatch.setattr(
        "app.services.operator_runtime.pydantic_ai_backend.generate_ai_json", fake_generate,
    )
    backend = PydanticAIDecisionBackend(
        system_prompt="system", json_schema={"type": "object"}, task_type="operator_judge",
    )
    settings = {"provider": "small-agent", "_operator_decision_max_tokens": 128}
    assert backend.decide(settings=settings, payload={}) == {
        "type": "finish", "message": "judge verified",
    }
    assert calls[0]["settings"] is not settings
    assert calls[0]["settings"]["model"] == "large-judge"
    assert calls[0]["settings"]["_max_provider_attempts"] == 1
    assert calls[0]["settings"]["fallback_chain"] == []
    assert settings["_decision_backend"] == "operator_judge"
    assert settings["_judge_model"] == "large-judge"


def test_needle_control_fallback_uses_judge_once(monkeypatch) -> None:
    calls = {"normal": 0, "judge": 0}

    class Normal:
        def decide(self, *, settings, payload):
            calls["normal"] += 1
            return {"type": "finish", "message": "ordinary"}

    class Judge:
        def decide(self, *, settings, payload):
            calls["judge"] += 1
            assert payload["judge_context"]["fallback_reason"].startswith("needle_schema_invalid")
            assert payload["judge_operations"]
            return {"type": "finish", "message": "judged"}

    monkeypatch.setattr(needle_module, "_agent_for", lambda _tools, _fingerprint: object())
    monkeypatch.setattr(
        needle_module,
        "_needle_complete",
        lambda *_args, **_kwargs: {
            "function_calls": [{"name": "op_001", "arguments": {}}], "confidence": 1.0,
        },
    )
    backend = NeedleDecisionBackend(
        fallback=Normal(), judge_fallback=Judge(),
        system_prompt="system", json_schema={"type": "object"}, mode="canary",
    )
    settings = {}
    result = backend.decide(
        settings=settings,
        payload={
            "objective": "Check health",
            "authorized_operations": [{
                "name": "health.check", "description": "Check health",
                "input_schema": {
                    "type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }],
        },
    )
    assert result == {"type": "finish", "message": "judged"}
    assert calls == {"normal": 0, "judge": 1}


def test_needle_read_only_uses_read_confidence_threshold(monkeypatch) -> None:
    monkeypatch.setattr(needle_module, "_agent_for", lambda _tools, _fingerprint: object())
    monkeypatch.setattr(
        needle_module,
        "_needle_complete",
        lambda *_args, **_kwargs: {
            "function_calls": [{"name": "op_001", "arguments": {"query": "health"}}],
            "confidence": 0.93,
        },
    )
    backend = NeedleDecisionBackend(
        fallback=object(), system_prompt="system", json_schema={"type": "object"}, mode="canary",
    )
    result = backend.decide(
        settings={},
        payload={
            "objective": "Check health",
            "verified_evidence": {"query": "health"},
            "authorized_operations": [{
                "name": "health.check", "risk_class": "SAFE_OPERATION", "read_only": True,
                "input_schema": {
                    "type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }],
        },
    )
    assert result["tool"] == "health.check"


def test_needle_negation_signal_falls_back(monkeypatch) -> None:
    calls: list[int] = []

    class Fallback:
        def decide(self, *, settings, payload):
            calls.append(1)
            return {"type": "finish", "message": "cloud"}

    monkeypatch.setattr(needle_module, "_agent_for", lambda _tools, _fingerprint: object())
    monkeypatch.setattr(
        needle_module,
        "_needle_complete",
        lambda *_args, **_kwargs: {
            "function_calls": [{"name": "op_001", "arguments": {"query": "health"}}],
            "confidence": 0.99,
            "validation": {"negation": True},
        },
    )
    backend = NeedleDecisionBackend(
        fallback=Fallback(), system_prompt="system", json_schema={"type": "object"}, mode="canary",
    )
    settings = {}
    result = backend.decide(
        settings=settings,
        payload={
            "objective": "Check health", "verified_evidence": {"query": "health"},
            "authorized_operations": [{
                "name": "health.check", "risk_class": "SAFE_OPERATION",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }],
        },
    )
    assert result == {"type": "finish", "message": "cloud"}
    assert calls == [1]
    assert settings["_fallback_reason"] == "needle_negation_detected"


def test_needle_canary_falls_back_once_when_local_model_is_unavailable(monkeypatch) -> None:
    calls = []

    class Fallback:
        def decide(self, *, settings, payload):
            calls.append(1)
            return {"type": "finish", "message": "fallback"}

    monkeypatch.setattr(
        "app.services.operator_runtime.needle_backend._needle_complete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("not installed")),
    )
    backend = NeedleDecisionBackend(
        fallback=Fallback(), system_prompt="system", json_schema={"type": "object"}, mode="canary",
    )
    settings = {"_operator_decision_max_tokens": 128}
    assert backend.decide(settings=settings, payload={}) == {"type": "finish", "message": "fallback"}
    assert calls == [1]
    assert settings["_needle_status"] == "fallback"


def test_pydantic_ai_backend_can_be_selected(monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_OPERATOR_DECISION_BACKEND", "pydantic_ai")
    backend = configured_decision_backend(system_prompt="system", json_schema={"type": "object"})
    assert isinstance(backend, PydanticAIDecisionBackend)


def test_needle_backend_is_opt_in_and_defaults_to_canary_when_requested(monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_OPERATOR_DECISION_BACKEND", "needle")
    monkeypatch.delenv("LYNEWRIGHT_OPERATOR_NEEDLE_MODE", raising=False)
    backend = configured_decision_backend(system_prompt="system", json_schema={"type": "object"})
    assert isinstance(backend, NeedleDecisionBackend)
    assert backend.mode == "canary"


def test_needle_uses_raw_authorized_schemas_and_complete_only(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class Agent:
        def reset(self):
            calls.append(("reset", None))

        def complete(self, prompt, *, max_new_tokens):
            calls.append(("complete", (prompt, max_new_tokens)))
            return {
                "function_calls": [{"name": "op_001", "arguments": {"query": "health"}}],
                "confidence": 0.99,
            }

        def run(self, *_args, **_kwargs):  # pragma: no cover - must never be called
            raise AssertionError("Needle.run must not execute the Operator runtime")

    captured: list[dict] = []

    def fake_agent(tools, fingerprint):
        captured.extend(tools)
        return Agent()

    monkeypatch.setattr(needle_module, "_agent_for", fake_agent)
    backend = NeedleDecisionBackend(
        fallback=object(), system_prompt="system", json_schema={"type": "object"}, mode="canary",
    )
    result = backend.decide(
        settings={"_operator_decision_max_tokens": 321},
        payload={
            "objective": "Check health",
            "authorized_operations": [
                {
                    "name": "health.check", "description": "Check health", "risk_class": "SAFE_OPERATION",
                    "input_schema": {
                        "type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
                {"name": "secret.read", "authorized": False, "input_schema": {"type": "object"}},
            ],
        },
    )

    assert result == {
        "type": "tool_call", "tool": "health.check",
        "arguments": {"query": "health"}, "message": "",
    }
    assert [name for name, _ in calls] == ["reset", "complete"]
    assert len(captured) == 1
    assert captured[0]["name"].startswith("op_001_health_check")
    assert captured[0]["parameters"]["required"] == ["query"]
    assert calls[1][1][1] == 321


def test_needle_schema_invalid_candidate_falls_back_once(monkeypatch) -> None:
    fallback_calls: list[dict] = []

    class Fallback:
        def decide(self, *, settings, payload):
            fallback_calls.append(payload)
            return {"type": "finish", "message": "cloud verified"}

    monkeypatch.setattr(
        needle_module,
        "_agent_for",
        lambda _tools, _fingerprint: object(),
    )
    monkeypatch.setattr(
        needle_module,
        "_needle_complete",
        lambda *_args, **_kwargs: {
            "function_calls": [{"name": "op_001", "arguments": {}}],
            "confidence": 0.99,
        },
    )
    backend = NeedleDecisionBackend(
        fallback=Fallback(), system_prompt="system", json_schema={"type": "object"}, mode="canary",
    )
    settings: dict = {}
    result = backend.decide(
        settings=settings,
        payload={
            "objective": "Check health",
            "authorized_operations": [{
                "name": "health.check", "risk_class": "SAFE_OPERATION",
                "input_schema": {
                    "type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }],
        },
    )

    assert result == {"type": "finish", "message": "cloud verified"}
    assert len(fallback_calls) == 1
    assert settings["_needle_status"] == "fallback"
    assert settings["_fallback_reason"].startswith("needle_schema_invalid")


def test_sensitive_needle_candidate_uses_semantic_verifier(monkeypatch) -> None:
    normal_calls: list[int] = []
    sensitive_calls: list[int] = []

    class Fallback:
        def decide(self, *, settings, payload):
            normal_calls.append(1)
            return {"type": "finish", "message": "normal"}

    class SensitiveFallback:
        def decide(self, *, settings, payload):
            sensitive_calls.append(1)
            assert payload["verification_request"]["candidate"]["tool"] == "publish.launch"
            return {"type": "finish", "message": "verified"}

    monkeypatch.setattr(
        needle_module,
        "_needle_complete",
        lambda *_args, **_kwargs: {
            "function_calls": [{"name": "op_001", "arguments": {"message": "launch"}}],
            "confidence": 1.0,
        },
    )
    monkeypatch.setattr(needle_module, "_agent_for", lambda _tools, _fingerprint: object())
    backend = NeedleDecisionBackend(
        fallback=Fallback(), sensitive_fallback=SensitiveFallback(),
        system_prompt="system", json_schema={"type": "object"}, mode="canary",
    )
    result = backend.decide(
        settings={},
        payload={
        "objective": "Launch the workflow",
            "authorized_operations": [{
                "name": "publish.launch", "risk_class": "PUBLICATION",
                "input_schema": {
                    "type": "object", "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            }],
        },
    )

    assert result == {"type": "finish", "message": "verified"}
    assert normal_calls == []
    assert sensitive_calls == [1]
