"""Internal BYOK Operator Agent runtime.

Public surface for other sessions (automation E, interaction F, policy G):

    from app.services.operator_runtime import OperatorService

    operator = OperatorService()
    profile = operator.create_profile("Ops", project_id="prj_x", granted_capabilities=[...])
    run = operator.start_run(profile["id"], project_id="prj_x", objective="...",
                             idempotency_key="unique-per-request")
    # run["status"] is one of RUN_STATES; when waiting:
    operator.resume_run(run["id"], reason="job finished")
    # approvals:
    approvals = operator.list_pending_approvals(project_id="prj_x")
    operator.resolve_approval(approvals[0]["id"], decision="approved")
"""
from __future__ import annotations

from app.services.operator_runtime.actor import (
    DEFAULT_OPERATOR_CAPABILITIES,
    build_operator_actor,
)
from app.services.operator_runtime.loop import (
    APPROVAL_REQUIRED_RISK_CLASSES,
    DECISION_JSON_SCHEMA,
    SYSTEM_PROMPT,
    OperatorLoop,
)
from app.services.operator_runtime.schema import RUN_STATES, TERMINAL_RUN_STATES
from app.services.operator_runtime.service import OperatorService
from app.services.operator_runtime.store import (
    OperatorConflict,
    OperatorError,
    OperatorInvalidTransition,
    OperatorNotFound,
    OperatorValidationError,
)

__all__ = [
    "APPROVAL_REQUIRED_RISK_CLASSES",
    "DECISION_JSON_SCHEMA",
    "DEFAULT_OPERATOR_CAPABILITIES",
    "RUN_STATES",
    "SYSTEM_PROMPT",
    "TERMINAL_RUN_STATES",
    "OperatorConflict",
    "OperatorError",
    "OperatorInvalidTransition",
    "OperatorLoop",
    "OperatorNotFound",
    "OperatorService",
    "OperatorValidationError",
    "build_operator_actor",
]
