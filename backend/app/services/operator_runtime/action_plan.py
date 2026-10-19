"""Bounded, data-only action plans for deterministic Operator progression."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class PlanBindingSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern="^step_output$")
    step_id: str = Field(min_length=1, max_length=80)
    pointer: str = Field(pattern=r"^/[A-Za-z0-9_~./-]*$")
    # Optional contract metadata lets a planner communicate the expected
    # shape without embedding provider-specific code in the reducer.
    schema_revision: str = Field(default="", max_length=160)
    value_type: str = Field(default="", max_length=80)
    semantic_type: str = Field(default="", max_length=120)


class PlanBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_pointer: str = Field(pattern=r"^/arguments/[A-Za-z0-9_~./-]*$")
    source: PlanBindingSource
    expected_type: str = Field(default="", max_length=80)
    expected_semantic: str = Field(default="", max_length=120)


class ActionPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=80)
    # ``tool`` is the compatibility name used by the current runtime. New
    # planners may provide ``operation_id``; both resolve to the same generic
    # operation surface and no provider-specific branch is needed.
    tool: str = Field(default="", max_length=240)
    operation_id: str = Field(default="", max_length=240)
    schema_revision: str = Field(default="", max_length=160)
    arguments: dict[str, Any] = Field(default_factory=dict)
    bindings: list[PlanBinding] = Field(default_factory=list, max_length=12)
    success_evidence: list[str] = Field(default_factory=list, max_length=12)


class ActionPlanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["action_plan"]
    steps: list[ActionPlanStep] = Field(min_length=2, max_length=6)
    message: str = Field(default="", max_length=2000)


ACTION_PLAN_ADAPTER = TypeAdapter(ActionPlanDecision)


def validate_action_plan(value: Any) -> dict[str, Any]:
    """Validate a plan and reject unsafe or ambiguous binding references."""

    plan = ACTION_PLAN_ADAPTER.validate_python(value)
    for step in plan.steps:
        if not step.tool and not step.operation_id:
            raise ValueError("Action plan steps require an operation_id or tool.")
        if not step.tool:
            step.tool = step.operation_id
        if not step.operation_id:
            step.operation_id = step.tool
    step_ids = [step.step_id for step in plan.steps]
    if len(set(step_ids)) != len(step_ids):
        raise ValueError("Action plan step_id values must be unique.")
    position = {step_id: index for index, step_id in enumerate(step_ids)}
    for step in plan.steps:
        if _contains_secret_key(step.arguments):
            raise ValueError("Action-plan arguments cannot contain credential-shaped fields.")
        for binding in step.bindings:
            if binding.source.step_id not in position:
                raise ValueError(f"Unknown action-plan source step: {binding.source.step_id!r}.")
            if position[binding.source.step_id] >= position[step.step_id]:
                raise ValueError("Action-plan bindings may reference only an earlier step.")
            lower_pointer = binding.source.pointer.lower()
            if any(secret in lower_pointer for secret in ("secret", "token", "password", "credential", "authorization", "api_key", "apikey")):
                raise ValueError("Action-plan bindings cannot reference secret-shaped fields.")
            if binding.expected_type and binding.source.value_type and binding.expected_type != binding.source.value_type:
                raise ValueError("Action-plan binding value types are incompatible.")
            if binding.expected_semantic and binding.source.semantic_type and binding.expected_semantic != binding.source.semantic_type:
                raise ValueError("Action-plan binding semantic types are incompatible.")
    return plan.model_dump(mode="json")


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(word in str(key).lower() for word in ("secret", "token", "password", "credential", "authorization", "api_key", "apikey")):
                return True
            if _contains_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _pointer_get(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    current = value
    for raw_part in pointer.lstrip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(pointer)
    return current


def _pointer_set(arguments: dict[str, Any], pointer: str, value: Any) -> None:
    """Set only an already-declared argument path.

    Creating missing dictionaries made a malformed binding look valid and
    allowed a model to smuggle an unexpected argument shape past schema
    validation. Bindings now have to target a field present in the planned
    argument object; arrays are supported only when the index already exists.
    """
    parts = pointer.removeprefix("/arguments/").split("/")
    current = arguments
    for raw_part in parts[:-1]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(pointer)
        if not isinstance(current, (dict, list)):
            raise TypeError(pointer)
    final = parts[-1].replace("~1", "/").replace("~0", "~")
    if isinstance(current, dict):
        if final not in current:
            raise KeyError(pointer)
        current[final] = value
    elif isinstance(current, list) and final.isdigit() and int(final) < len(current):
        current[int(final)] = value
    else:
        raise KeyError(pointer)


def _json_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return ""


def materialize_step(step: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    """Resolve safe step-output bindings into a copy of one tool argument set."""

    parsed = ActionPlanStep.model_validate(step)
    arguments = dict(parsed.arguments)
    for binding in parsed.bindings:
        source_output = outputs.get(binding.source.step_id)
        if source_output is None:
            raise KeyError(binding.source.step_id)
        value = _pointer_get(source_output, binding.source.pointer)
        expected = str(binding.expected_type or binding.source.value_type or "").strip()
        if expected:
            actual = _json_value_type(value)
            compatible = actual == expected or (expected == "number" and actual == "integer")
            if not compatible:
                raise TypeError(
                    f"Binding {binding.source.step_id}{binding.source.pointer} expected {expected}, got {actual}."
                )
        _pointer_set(arguments, binding.target_pointer, value)
    return {
        "type": "tool_call",
        "tool": parsed.tool or parsed.operation_id,
        "operation_id": parsed.operation_id or parsed.tool,
        "arguments": arguments,
        "message": "Executing the next validated action-plan step.",
        "_plan_step_id": parsed.step_id,
        "_plan_schema_revision": parsed.schema_revision,
        "_plan_success_evidence": list(parsed.success_evidence),
    }


__all__ = [
    "ACTION_PLAN_ADAPTER",
    "ActionPlanDecision",
    "ActionPlanStep",
    "PlanBinding",
    "PlanBindingSource",
    "materialize_step",
    "validate_action_plan",
]
