"""API models for the minimal Operator management surface (plan §10).

These shapes are a frozen contract: the product UI is built against them
without reading the implementation. Response models mirror exactly what the
operator runtime service returns (`profile_public`, `run_public`,
`step_public`, `approval_public`, automation `_public`) — never raw storage
rows, and never credentials or model keys.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.operator_automation import MAX_COOLDOWN_SECONDS


TriggerType = Literal["schedule", "event", "condition"]
ApprovalStatus = Literal["pending", "approved", "denied", "cancelled"]
ApprovalDecision = Literal["approved", "denied"]
DecisionPolicy = Literal["cloud_only", "needle_shadow", "needle_assisted", "needle_only"]
# One of RUN_STATES: queued running waiting_tool waiting_job waiting_approval
# sleeping completed failed cancelled. Kept as a described string because the
# state set lives in the runtime schema (CHECK-enforced in storage).
RunStatus = str


# ------------------------------------------------------------------ profiles

class OperatorProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    project_id: str = Field(min_length=1, max_length=100)
    instructions: str = Field(default="", max_length=8000)
    enabled: bool = True
    granted_capabilities: list[str] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=100)
    max_consecutive_tool_failures: int | None = Field(default=None, ge=1, le=20)
    max_model_failures: int | None = Field(default=None, ge=1, le=10)
    max_total_tokens: int | None = Field(default=None, ge=0, le=200_000)
    max_model_decisions: int | None = Field(default=None, ge=0, le=100)
    max_network_attempts: int | None = Field(default=None, ge=0, le=200)
    no_progress_limit: int | None = Field(default=None, ge=1, le=10)
    retrieval_mode: Literal["lexical", "hybrid_local"] | None = None
    retrieval_canary_percent: int | None = Field(default=None, ge=0, le=100)
    decision_policy: DecisionPolicy = "cloud_only"
    needle_canary_percent: int = Field(default=0, ge=0, le=100)
    needle_read_confidence: float = Field(default=0.92, ge=0, le=1)
    needle_write_confidence: float = Field(default=0.97, ge=0, le=1)
    needle_sensitive_confidence: float = Field(default=0.99, ge=0, le=1)


class ResourceOutboxReplayRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)


class ResourceOutboxReplayBatchRequest(BaseModel):
    outbox_ids: list[str] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2_000)


class ResourceBindingRepairRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2_000)


class ResourceSetupVerificationRequest(BaseModel):
    evidence: dict[str, Any] = Field(default_factory=dict)


class OperatorProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    instructions: str | None = Field(default=None, max_length=8000)
    enabled: bool | None = None
    granted_capabilities: list[str] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=100)
    max_consecutive_tool_failures: int | None = Field(default=None, ge=1, le=20)
    max_model_failures: int | None = Field(default=None, ge=1, le=10)
    max_total_tokens: int | None = Field(default=None, ge=0, le=200_000)
    max_model_decisions: int | None = Field(default=None, ge=0, le=100)
    max_network_attempts: int | None = Field(default=None, ge=0, le=200)
    no_progress_limit: int | None = Field(default=None, ge=1, le=10)
    retrieval_mode: Literal["lexical", "hybrid_local"] | None = None
    retrieval_canary_percent: int | None = Field(default=None, ge=0, le=100)
    decision_policy: DecisionPolicy | None = None
    needle_canary_percent: int | None = Field(default=None, ge=0, le=100)
    needle_read_confidence: float | None = Field(default=None, ge=0, le=1)
    needle_write_confidence: float | None = Field(default=None, ge=0, le=1)
    needle_sensitive_confidence: float | None = Field(default=None, ge=0, le=1)


class OperatorProfileRead(BaseModel):
    id: str
    tenant_id: str
    project_id: str
    name: str
    enabled: bool
    instructions: str
    granted_capabilities: list[str]
    max_steps: int
    max_consecutive_tool_failures: int
    max_model_failures: int
    max_total_tokens: int = 0
    max_model_decisions: int = 0
    max_network_attempts: int = 0
    no_progress_limit: int = 3
    retrieval_mode: Literal["lexical", "hybrid_local"] = "hybrid_local"
    retrieval_canary_percent: int = 10
    decision_policy: DecisionPolicy = "cloud_only"
    needle_canary_percent: int = Field(default=0, ge=0, le=100)
    needle_read_confidence: float = Field(default=0.92, ge=0, le=1)
    needle_write_confidence: float = Field(default=0.97, ge=0, le=1)
    needle_sensitive_confidence: float = Field(default=0.99, ge=0, le=1)
    created_at: str
    updated_at: str


class OperatorProfileList(BaseModel):
    profiles: list[OperatorProfileRead]


class OperatorDecisionModelRead(BaseModel):
    installed: bool
    healthy: bool
    deployment_safe: bool = True
    needle_installed: bool = False
    engine_verified: bool = False
    judge_route_ready: bool = False
    ready_for_shadow: bool = False
    ready_for_control: bool = False
    judge_provider: str = ""
    judge_model: str = ""
    package: str
    package_version: str
    generation: str
    engine_checksum: str
    global_mode: Literal["off", "shadow", "canary"]
    profile_policies: dict[str, int] = Field(default_factory=dict)
    acceptance_rate: float = 0
    fallback_rate: float = 0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    cloud_tokens_avoided: int = 0
    fallback_reasons: dict[str, int] = Field(default_factory=dict)


class OperatorBootstrapRequest(BaseModel):
    """Idempotent first-enable path: create (or return) the default profile."""

    project_id: str = Field(min_length=1, max_length=100)
    name: str = Field(default="Default Operator", min_length=1, max_length=120)
    instructions: str = Field(
        default=(
            "Coordinate Lynewright for this project. Read state before acting, stay "
            "inside granted capabilities, and request approval before any "
            "sensitive or hard-to-reverse action."
        ),
        max_length=8000,
    )
    granted_capabilities: list[str] | None = None


class OperatorBootstrapResult(BaseModel):
    profile: OperatorProfileRead
    created: bool


# --------------------------------------------------------------------- runs

class OperatorRunCreate(BaseModel):
    profile_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=8000)
    context: dict[str, Any] | None = None
    idempotency_key: str = Field(default="", max_length=160)
    # ``queued`` acknowledges immediately and lets the runtime worker own the
    # durable execution. ``sync`` preserves the established API behaviour.
    execution_mode: Literal["sync", "queued"] = "sync"
    workflow_id: str | None = Field(default=None, max_length=120)


class OperatorWorkflowCreate(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    objective_template: str = Field(min_length=1, max_length=8000)
    plan: dict[str, Any]
    contract_revisions: dict[str, Any] = Field(default_factory=dict)


class OperatorWorkflowReview(BaseModel):
    approve: bool = True
    reviewer: str = Field(default="", max_length=160)


class OperatorWorkflowRead(BaseModel):
    id: str
    tenant_id: str
    project_id: str
    name: str
    objective_template: str
    plan: dict[str, Any]
    contract_revisions: dict[str, Any]
    status: str
    version: int
    reviewed_by: str
    reviewed_at: str | None = None
    created_at: str
    updated_at: str


class OperatorWorkflowList(BaseModel):
    workflows: list[OperatorWorkflowRead]


class OperatorRunActionRequest(BaseModel):
    reason: str = Field(default="", max_length=2000)


class OperatorRunStepRead(BaseModel):
    id: int
    sequence: int
    step_type: str
    status: str
    tool_name: str
    decision: dict[str, Any]
    input: dict[str, Any]
    output: dict[str, Any]
    job_id: str | None = None
    approval_id: str | None = None
    provider: str
    protocol: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    usage_exact: bool = False
    provider_attempts: int = 0
    prompt_chars: int = 0
    decision_backend: str = ""
    decision_confidence: float | None = None
    decision_risk_class: str = ""
    needle_latency_ms: int = 0
    needle_schema_fingerprint: str = ""
    evidence_validation_result: str = ""
    sensitive_verification_required: bool = False
    fallback_reason: str = ""
    duration_ms: int
    error_code: str
    error_summary: str
    created_at: str


class OperatorRunRead(BaseModel):
    id: str
    profile_id: str
    tenant_id: str
    project_id: str
    objective: str
    status: RunStatus
    summary: str
    context: dict[str, Any]
    current_step: int
    consecutive_tool_failures: int
    model_failures: int
    wake_reason: str
    wake_at: str | None = None
    pending_job_id: str
    pending_approval_id: str
    error_code: str
    error_summary: str
    revision: int
    idempotency_key: str
    last_provider: str
    last_protocol: str
    last_model: str
    decision_policy: DecisionPolicy = "cloud_only"
    needle_read_confidence: float = 0.92
    needle_write_confidence: float = 0.97
    needle_sensitive_confidence: float = 0.99
    needle_canary_percent: int = Field(default=0, ge=0, le=100)
    decision_model_checksum: str = ""
    tool_schema_fingerprint: str = ""
    created_by: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    usage_summary: dict[str, int] = Field(default_factory=dict)
    steps: list[OperatorRunStepRead] | None = None


class OperatorRunList(BaseModel):
    runs: list[OperatorRunRead]


class OperatorRunClearResult(BaseModel):
    """Count returned after terminal run history is cleared."""

    cleared: int = Field(ge=0)


# ---------------------------------------------------------------- approvals

class OperatorApprovalRead(BaseModel):
    id: str
    run_id: str
    tenant_id: str
    project_id: str
    action: str
    summary: str
    payload_ref: dict[str, Any]
    status: ApprovalStatus
    requested_by: str
    resolved_by: str
    resolution_note: str
    created_at: str
    resolved_at: str | None = None


class OperatorApprovalList(BaseModel):
    approvals: list[OperatorApprovalRead]


class OperatorApprovalResolveRequest(BaseModel):
    decision: ApprovalDecision
    note: str = Field(default="", max_length=2000)
    resolved_by: str = Field(default="", max_length=160)


# --------------------------------------------------------------- automations

class OperatorAutomationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    trigger_type: TriggerType
    trigger_config: dict[str, Any]
    objective_template: str = Field(default="", max_length=8000)
    action: dict[str, Any] | None = None
    project_id: str | None = Field(default=None, max_length=100)
    objective_title: str = Field(default="", max_length=200)
    notification_policy: dict[str, Any] | None = None
    cooldown_seconds: int = Field(default=0, ge=0, le=MAX_COOLDOWN_SECONDS)
    enabled: bool = True


class OperatorAutomationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    objective_template: str | None = Field(default=None, max_length=8000)
    action: dict[str, Any] | None = None
    objective_title: str | None = Field(default=None, max_length=200)
    notification_policy: dict[str, Any] | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0, le=MAX_COOLDOWN_SECONDS)
    trigger_config: dict[str, Any] | None = None


class OperatorAutomationRead(BaseModel):
    id: str
    tenant_id: str
    project_id: str | None = None
    name: str
    enabled: bool
    trigger_type: TriggerType
    trigger_config: dict[str, Any]
    objective_template: str
    action: dict[str, Any] | None = None
    objective_title: str
    notification_policy: dict[str, Any]
    cooldown_seconds: int
    last_triggered_at: str | None = None
    last_triggered_epoch: float | None = None
    next_due_at_epoch: float | None = None
    created_at: str
    updated_at: str
    schedule: dict[str, Any] | None = None


class OperatorAutomationList(BaseModel):
    automations: list[OperatorAutomationRead]


class OperatorAutomationTemplateRead(BaseModel):
    key: str
    name: str
    trigger_type: TriggerType
    trigger_config: dict[str, Any]
    objective_title: str
    objective_template: str
    action: dict[str, Any] | None = None
