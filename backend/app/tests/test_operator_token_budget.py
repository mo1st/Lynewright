"""The operator's output ceiling follows the model: a reasoning model thinks from its output budget (found live on
2026-10-02, when deepseek-v4-flash was truncated on every decision of a "neues Buch" run at the 1024-token ceiling)."""
from __future__ import annotations

import pytest

from app.services.ai_provider import AiProviderError
from app.services.operator_runtime import loop as loop_module
from app.services.operator_runtime.loop import (
    OPERATOR_TOKEN_MAXIMUM, REASONING_TOKENS, OperatorLoop, is_reasoning_model,
)


@pytest.mark.parametrize("model", [
    "deepseek-v4-flash", "deepseek-v4-pro", "DeepSeek-R1", "deepseek-reasoner", "qwen3-235b-a22b", "qwen3-max", "qwq-32b",
    "o1", "o3-mini", "o4-mini", "openai/o3", "gpt-5", "gpt-5-mini", "gemini-2.5-pro", "gemini-3-pro", "kimi-k2-thinking", "r1",
])
def test_reasoning_models_are_recognised(model) -> None:
    assert is_reasoning_model(model) is True


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4.1", "claude-operator", "claude-sonnet-4", "qwen-plus", "qwen2.5-72b", "deepseek-chat",
                                   "llama-3.3-70b", "gemini-2.0-flash", "mistral-large", "", None, "o2x-custom", "robot"])
def test_other_models_are_not(model) -> None:
    assert is_reasoning_model(model) is False


def _loop(monkeypatch, model: str, **env) -> dict:
    monkeypatch.setattr(loop_module, "get_ai_settings_for_task", lambda task: {"provider": "openai_compatible", "model": model})
    for name in ("LYNEWRIGHT_OPERATOR_DECISION_MAX_TOKENS", "LYNEWRIGHT_OPERATOR_PLAN_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return OperatorLoop.__new__(OperatorLoop)._resolve_settings()


def test_a_reasoning_model_gets_at_least_16000_tokens_for_a_decision_and_a_plan(monkeypatch) -> None:
    settings = _loop(monkeypatch, "deepseek-v4-flash")

    assert settings["_operator_decision_max_tokens"] >= 16000 and settings["_operator_plan_max_tokens"] >= 16000
    assert settings["_operator_decision_max_tokens"] == settings["_operator_plan_max_tokens"] == REASONING_TOKENS
    assert settings["_operator_token_maximum"] == OPERATOR_TOKEN_MAXIMUM == 32000


def test_any_other_model_keeps_the_small_ceilings(monkeypatch) -> None:
    settings = _loop(monkeypatch, "qwen-plus")

    assert settings["_operator_decision_max_tokens"] == 1024 and settings["_operator_plan_max_tokens"] == 4096


def test_the_environment_still_decides_and_both_maxima_are_32000(monkeypatch) -> None:
    low = _loop(monkeypatch, "deepseek-v4-flash", LYNEWRIGHT_OPERATOR_DECISION_MAX_TOKENS="2000", LYNEWRIGHT_OPERATOR_PLAN_MAX_TOKENS="3000")
    high = _loop(monkeypatch, "qwen-plus", LYNEWRIGHT_OPERATOR_DECISION_MAX_TOKENS="99999", LYNEWRIGHT_OPERATOR_PLAN_MAX_TOKENS="40000")

    assert (low["_operator_decision_max_tokens"], low["_operator_plan_max_tokens"]) == (2000, 3000)
    assert (high["_operator_decision_max_tokens"], high["_operator_plan_max_tokens"]) == (32000, 32000)


class _Backend:
    name = "fake"

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.budgets: list[int] = []

    def decide(self, *, settings, payload):
        self.budgets.append(int(settings["_operator_decision_max_tokens"]))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _truncated() -> AiProviderError:
    return AiProviderError("AI provider response was truncated at the output limit.", failure_kind="truncated")


def _decider(backend) -> OperatorLoop:
    loop = OperatorLoop.__new__(OperatorLoop)
    loop._decision_fn = None
    loop._decision_backend = backend
    return loop


def _run(loop, budget: int):
    return loop._decide_safely({"_operator_decision_max_tokens": budget, "_operator_token_maximum": OPERATOR_TOKEN_MAXIMUM, "provider": "x"}, {})


def test_a_truncated_decision_is_asked_again_once_with_double_the_budget() -> None:
    backend = _Backend(_truncated(), {"type": "final", "message": "ok"})

    decision, _meta = _run(_decider(backend), 4096)

    assert decision == {"type": "final", "message": "ok"}
    assert backend.budgets == [4096, 8192]


def test_the_retry_budget_never_exceeds_the_maximum() -> None:
    backend = _Backend(_truncated(), {"type": "final"})

    _run(_decider(backend), 20000)

    assert backend.budgets == [20000, 32000]


def test_a_second_truncation_fails_the_step_after_exactly_one_retry() -> None:
    backend = _Backend(_truncated(), _truncated(), {"type": "never reached"})

    decision, _meta = _run(_decider(backend), 1024)

    assert isinstance(decision, AiProviderError) and decision.context["failure_kind"] == "truncated"
    assert backend.budgets == [1024, 2048]


def test_a_decision_already_at_the_maximum_is_not_retried_and_other_failures_never_are() -> None:
    at_maximum = _Backend(_truncated())
    other = _Backend(AiProviderError("provider paused", failure_kind="rate_limited"))

    decision, _ = _run(_decider(at_maximum), 32000)
    other_decision, _ = _run(_decider(other), 1024)

    assert isinstance(decision, AiProviderError) and at_maximum.budgets == [32000]
    assert isinstance(other_decision, AiProviderError) and other.budgets == [1024]
