"""Transport-neutral inputs for external automation planning."""
from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from app.services.platform_models import _PlatformInput


class AutomationPlanInput(_PlatformInput):
    project_id: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=2_000)
    line_name: str = Field(min_length=1, max_length=200)
    definition: dict[str, Any]
    constraints: dict[str, Any] = Field(default_factory=dict, max_length=40)
    plan_id: str | None = Field(default=None, pattern=r"^apl_[a-f0-9]{32}$")
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def revision_requires_version(self) -> "AutomationPlanInput":
        if (self.plan_id is None) != (self.expected_revision is None):
            raise ValueError("plan_id and expected_revision must be supplied together when revising a plan.")
        return self


class AutomationPlanReference(_PlatformInput):
    project_id: str = Field(min_length=1, max_length=160)


class AutomationPlanApplyInput(AutomationPlanReference):
    revision: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AutomationPlanAuthorizationInput(AutomationPlanApplyInput):
    pass
