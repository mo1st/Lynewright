"""Strict, transport-neutral Operator decision contracts.

The durable Operator protocol still stores dictionaries for compatibility.  New
decision backends validate those dictionaries through these discriminated
models before the loop can act on them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.services.operator_runtime.action_plan import ActionPlanDecision


class ToolCallDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"]
    tool: str = Field(min_length=1, max_length=240)
    arguments: dict[str, Any] = Field(default_factory=dict)
    message: str = Field(default="", max_length=2000)


class FinishDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["finish"]
    message: str = Field(min_length=1, max_length=4000)


class WaitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["wait"]
    wait_reason: Literal["tool", "job", "approval", "sleep"]
    message: str = Field(default="", max_length=2000)
    job_id: str | None = Field(default=None, max_length=240)
    wake_at: datetime | None = None


OperatorDecision = Annotated[
    ToolCallDecision | FinishDecision | WaitDecision | ActionPlanDecision,
    Field(discriminator="type"),
]

OPERATOR_DECISION_ADAPTER = TypeAdapter(OperatorDecision)


def decision_json_schema() -> dict[str, Any]:
    """Return the strict schema sent to qualified structured-output providers."""

    return OPERATOR_DECISION_ADAPTER.json_schema()


def validate_decision(value: Any) -> dict[str, Any]:
    """Validate and normalize one new-format decision to the legacy wire shape."""

    decision = OPERATOR_DECISION_ADAPTER.validate_python(value)
    return decision.model_dump(mode="json", exclude_none=True)


__all__ = [
    "FinishDecision",
    "OperatorDecision",
    "OPERATOR_DECISION_ADAPTER",
    "ToolCallDecision",
    "WaitDecision",
    "decision_json_schema",
    "validate_decision",
]
