"""Minimal Operator management API (plan §10.1-10.7, error states §29).

A thin, validated surface over the existing operator runtime services:

    OperatorService                profiles / runs / steps / approvals
    OperatorAutomationService      automations (schedule/event/condition)
    operator_diagnostics           system explanation
    OperatorPolicyEngine           action registry + approval policy reads

Every endpoint is tenant-scoped through ``require_tenant_context`` /
``require_tenant_role``; no cross-tenant profile lookup, run access, or
approval application is possible because each service instance is bound to the
request's authorized tenant id. No endpoint returns or accepts credentials:
the Operator's BYOK model route lives exclusively in ``/api/ai/providers``.

Expected configuration states are explicit HTTP errors (plan §29), never
stack traces: 400 invalid input, 404 unknown/out-of-scope resource, 409 state
conflict (disabled profile, pending approval, terminal run).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.models.operator import (
    ApprovalStatus,
    OperatorApprovalList,
    OperatorApprovalRead,
    OperatorApprovalResolveRequest,
    OperatorAutomationCreate,
    OperatorAutomationList,
    OperatorAutomationRead,
    OperatorAutomationTemplateRead,
    OperatorAutomationUpdate,
    OperatorBootstrapRequest,
    OperatorBootstrapResult,
    OperatorDecisionModelRead,
    OperatorProfileCreate,
    OperatorProfileList,
    OperatorProfileRead,
    OperatorProfileUpdate,
    OperatorRunActionRequest,
    OperatorRunCreate,
    OperatorRunClearResult,
    OperatorRunList,
    OperatorRunRead,
    OperatorRunStepRead,
    OperatorWorkflowCreate,
    OperatorWorkflowList,
    OperatorWorkflowRead,
    OperatorWorkflowReview,
    ResourceOutboxReplayRequest,
    ResourceOutboxReplayBatchRequest,
    ResourceBindingRepairRequest,
    ResourceSetupVerificationRequest,
)
from app.security import require_tenant_context, require_tenant_role
from app.services.operator_automation import (
    AUTOMATION_TEMPLATES,
    OperatorAutomationService,
)
from app.services.operator_diagnostics import default_component_registry, explain_system
from app.services.operator_policy import RISK_CLASSES, OperatorPolicyEngine
from app.services.operator_runtime import (
    OperatorConflict,
    OperatorError,
    OperatorNotFound,
    OperatorService,
    OperatorValidationError,
)
from app.services.operator_runtime import store as opstore
from app.services.operator_workflow_drafts import OperatorWorkflowDraftService
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
)
from app.services.resource_outbox import ResourceOutboxService
from app.services.resource_lifecycle_host import get_resource_lifecycle_coordinator
from app.services.resource_setup_requirements import ResourceSetupRequirementService
from app.services.resource_bindings import ResourceBindingService
from app.services.resource_reconciliation import ResourceReconciliationService

router = APIRouter(prefix="/api/operator", tags=["operator"])

_READ = require_tenant_context
_WRITE = require_tenant_role("editor")
_ADMIN = require_tenant_role("admin")


def _map_errors(exc: Exception) -> HTTPException:
    """Translate stable service-layer errors into plan §29 HTTP states."""
    if isinstance(exc, (OperatorNotFound, PlatformNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, OperatorConflict):  # includes invalid transitions
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, PlatformOperationError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (OperatorValidationError, PlatformValidationError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Operator request failed.")


def _operator_service(tenant_id: str) -> OperatorService:
    return OperatorService(tenant_id=tenant_id)


def _automation_service(tenant_id: str) -> OperatorAutomationService:
    return OperatorAutomationService(tenant_id=tenant_id)


def _workflow_service(tenant_id: str) -> OperatorWorkflowDraftService:
    return OperatorWorkflowDraftService(store=PlatformStore(), tenant_id=tenant_id)


@router.get("/resource-lifecycle/outbox")
def list_resource_outbox(
    status_filter: str | None = Query(default=None, alias="status", max_length=32),
    aggregate_id: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=100, ge=1, le=1_000),
    context: dict = Depends(_READ),
) -> dict[str, Any]:
    """Return bounded tenant-scoped lifecycle work for operator diagnostics."""
    try:
        return {"items": ResourceOutboxService(tenant_id=context["tenant_id"]).list(status=status_filter, aggregate_id=aggregate_id, limit=limit)}
    except (PlatformNotFoundError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.get("/resource-lifecycle/outbox/{outbox_id}")
def get_resource_outbox(
    outbox_id: str,
    context: dict = Depends(_READ),
) -> dict[str, Any]:
    """Return one bounded tenant-scoped lifecycle item for diagnosis."""
    try:
        return {"item": ResourceOutboxService(tenant_id=context["tenant_id"]).get(outbox_id)}
    except (PlatformNotFoundError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.get("/resource-lifecycle/attention")
def list_resource_attention(
    limit: int = Query(default=200, ge=1, le=1_000),
    context: dict = Depends(_READ),
) -> dict[str, Any]:
    """Return open lifecycle attention items for the authorized tenant."""
    try:
        items = ResourceReconciliationService(tenant_id=context["tenant_id"]).list_open(limit=limit)
        return {"items": items}
    except PlatformValidationError as exc:
        raise _map_errors(exc) from exc


@router.post("/resource-lifecycle/reconcile")
def reconcile_resource_lifecycle(
    max_items: int = Query(default=5, ge=1, le=100),
    context: dict = Depends(_ADMIN),
) -> dict[str, Any]:
    """Run one bounded provider rediscovery pass and refresh attention items."""
    coordinator = get_resource_lifecycle_coordinator(context["tenant_id"])
    if coordinator is None:
        raise HTTPException(status_code=409, detail="Resource lifecycle worker is not available for this tenant.")
    try:
        reconciled = coordinator.reconcile_bindings(max_items=max_items)
        attention = coordinator.reconcile_attention(limit=min(1_000, max_items * 10))
        return {"reconciled": reconciled, "attention": attention}
    except (PlatformConflictError, PlatformNotFoundError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.post("/resource-lifecycle/bindings/{binding_id}/repair")
def repair_resource_binding(
    binding_id: str,
    payload: ResourceBindingRepairRequest,
    context: dict = Depends(_ADMIN),
) -> dict[str, Any]:
    """Run one explicit operator-approved repair for a degraded binding."""
    coordinator = get_resource_lifecycle_coordinator(context["tenant_id"])
    if coordinator is None:
        raise HTTPException(status_code=409, detail="Resource lifecycle worker is not available for this tenant.")
    try:
        return coordinator.repair_binding(
            binding_id=binding_id,
            operator_id=str(context.get("user_id") or "synthetic-admin"),
            reason=payload.reason,
        )
    except (PlatformConflictError, PlatformNotFoundError, PlatformOperationError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.post("/resource-lifecycle/outbox/replay")
def replay_resource_outbox_batch(
    payload: ResourceOutboxReplayBatchRequest,
    context: dict = Depends(_ADMIN),
) -> dict[str, Any]:
    """Atomically requeue a bounded set of dead letters."""
    try:
        items = ResourceOutboxService(tenant_id=context["tenant_id"]).replay_dead_letters(
            outbox_ids=payload.outbox_ids,
            operator_id=str(context.get("user_id") or "synthetic-admin"),
            reason=payload.reason,
        )
        return {"items": items}
    except (PlatformConflictError, PlatformNotFoundError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.post("/resource-lifecycle/outbox/{outbox_id}/replay")
def replay_resource_outbox(
    outbox_id: str,
    payload: ResourceOutboxReplayRequest,
    context: dict = Depends(_ADMIN),
) -> dict[str, Any]:
    """Requeue one dead letter after an explicit administrator decision."""
    try:
        return ResourceOutboxService(tenant_id=context["tenant_id"]).replay_dead_letter(
            outbox_id=outbox_id,
            operator_id=str(context.get("user_id") or "synthetic-admin"),
            reason=payload.reason,
        )
    except (PlatformConflictError, PlatformNotFoundError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.get("/resource-lifecycle/bindings/{binding_id}/setup")
def list_resource_setup_requirements(
    binding_id: str,
    context: dict = Depends(_READ),
) -> dict[str, Any]:
    try:
        # Resolve the binding first so an unknown (or cross-tenant) id is a
        # stable 404 instead of looking like a valid binding with no setup
        # requirements.  Both services are tenant-scoped to the request
        # context, so this does not disclose another tenant's existence.
        binding = ResourceBindingService(tenant_id=context["tenant_id"]).get(binding_id)
        requirements = ResourceSetupRequirementService(tenant_id=context["tenant_id"]).list(binding_id=binding["id"])
        return {"binding_id": binding["id"], "requirements": requirements}
    except (PlatformNotFoundError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.get("/resource-lifecycle/sites/{site_id}/setup")
def list_site_setup_requirements(
    site_id: str,
    context: dict = Depends(_READ),
) -> dict[str, Any]:
    """Resolve setup requirements from a canonical Site id for the UI."""
    canonical_site_id = str(site_id or "").strip()
    if not canonical_site_id:
        raise _map_errors(PlatformValidationError("Site id is invalid."))
    bindings = ResourceBindingService(tenant_id=context["tenant_id"]).list(resource_type="site", resource_id=canonical_site_id)
    if not bindings:
        raise _map_errors(PlatformNotFoundError("Resource was not found."))
    setup = ResourceSetupRequirementService(tenant_id=context["tenant_id"])
    requirements: list[dict[str, Any]] = []
    for binding in bindings:
        if binding.get("role") != "website.analytics":
            continue
        requirements.extend({"binding_id": binding["id"], **item} for item in setup.list(binding_id=binding["id"]))
    return {"site_id": site_id, "requirements": requirements[:50]}


@router.post("/resource-lifecycle/bindings/{binding_id}/setup/{requirement_id}/verify")
def verify_resource_setup_requirement(
    binding_id: str,
    requirement_id: str,
    payload: ResourceSetupVerificationRequest,
    context: dict = Depends(_ADMIN),
) -> dict[str, Any]:
    coordinator = get_resource_lifecycle_coordinator(context["tenant_id"])
    if coordinator is None:
        raise HTTPException(status_code=409, detail="Resource lifecycle worker is not available for this tenant.")
    try:
        return coordinator.verify_setup(binding_id=binding_id, requirement_id=requirement_id, evidence=payload.evidence)
    except (PlatformConflictError, PlatformNotFoundError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


# ---------------------------------------------------------------------
# 10.1 Profiles
# ---------------------------------------------------------------------

@router.get("/decision-model", response_model=OperatorDecisionModelRead)
def decision_model_status(context: dict = Depends(_READ)) -> dict:
    service = _operator_service(context["tenant_id"])
    try:
        return service.decision_model_status()
    except OperatorError as exc:
        raise _map_errors(exc) from exc

@router.get("/profiles", response_model=OperatorProfileList)
def list_profiles(
    project_id: str | None = Query(default=None, max_length=100),
    context: dict = Depends(_READ),
) -> dict:
    service = _operator_service(context["tenant_id"])
    try:
        profiles = service.list_profiles(project_id=project_id)
    except OperatorError as exc:
        raise _map_errors(exc) from exc
    return {"profiles": profiles}


@router.post(
    "/profiles",
    response_model=OperatorProfileRead,
    status_code=status.HTTP_201_CREATED,
)
def create_profile(
    payload: OperatorProfileCreate,
    context: dict = Depends(_WRITE),
) -> dict:
    service = _operator_service(context["tenant_id"])
    bounds: dict[str, Any] = {
        key: value
        for key in (
            "max_steps", "max_consecutive_tool_failures", "max_model_failures",
            "max_total_tokens", "max_model_decisions", "max_network_attempts", "no_progress_limit",
            "retrieval_mode", "retrieval_canary_percent",
            "decision_policy", "needle_canary_percent", "needle_read_confidence",
            "needle_write_confidence", "needle_sensitive_confidence",
        )
        if (value := getattr(payload, key)) is not None
    }
    try:
        return service.create_profile(
            payload.name,
            project_id=payload.project_id,
            instructions=payload.instructions,
            granted_capabilities=payload.granted_capabilities,
            enabled=payload.enabled,
            **bounds,
        )
    except OperatorError as exc:
        raise _map_errors(exc) from exc


@router.get("/profiles/{profile_id}", response_model=OperatorProfileRead)
def get_profile(profile_id: str, context: dict = Depends(_READ)) -> dict:
    service = _operator_service(context["tenant_id"])
    try:
        return service.get_profile(profile_id)
    except OperatorError as exc:
        raise _map_errors(exc) from exc


@router.patch("/profiles/{profile_id}", response_model=OperatorProfileRead)
def update_profile(
    profile_id: str,
    payload: OperatorProfileUpdate,
    context: dict = Depends(_WRITE),
) -> dict:
    service = _operator_service(context["tenant_id"])
    fields = {key: value for key, value in payload.model_dump().items() if value is not None}
    try:
        return service.update_profile(profile_id, **fields)
    except OperatorError as exc:
        raise _map_errors(exc) from exc


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str, context: dict = Depends(_WRITE)) -> dict:
    service = _operator_service(context["tenant_id"])
    try:
        return service.delete_profile(profile_id)
    except OperatorError as exc:
        raise _map_errors(exc) from exc


# ---------------------------------------------------------------------
# 10.2 Bootstrap / default profile
# ---------------------------------------------------------------------

@router.post("/bootstrap", response_model=OperatorBootstrapResult)
def bootstrap_operator(
    payload: OperatorBootstrapRequest,
    context: dict = Depends(_WRITE),
) -> dict:
    """Idempotent first-enable path for a project.

    Returns an existing enabled profile when one exists; otherwise creates one
    with the workspace's configured runtime defaults (the full public
    capability surface, with risky actions still approval-gated) and
    bounded steps/failures). Never touches secrets or provider keys.
    """
    service = _operator_service(context["tenant_id"])
    try:
        existing = service.list_profiles(project_id=payload.project_id)
        enabled = [item for item in existing if item["enabled"]]
        chosen = next(
            (item for item in enabled if item["project_id"] == payload.project_id),
            enabled[0] if enabled else None,
        )
        if chosen is not None:
            return {"profile": chosen, "created": False}
        same_name = next((item for item in existing if item["name"] == payload.name), None)
        if same_name is not None:
            # Idempotent even after the default profile was disabled; turning
            # it back on stays an explicit caller decision via PATCH.
            return {"profile": same_name, "created": False}
        profile = service.create_profile(
            payload.name,
            project_id=payload.project_id,
            instructions=payload.instructions,
            granted_capabilities=payload.granted_capabilities,
        )
        return {"profile": profile, "created": True}
    except OperatorError as exc:
        raise _map_errors(exc) from exc


# ---------------------------------------------------------- workflow drafts

@router.get("/workflows", response_model=OperatorWorkflowList)
def list_workflow_drafts(
    project_id: str | None = Query(default=None, max_length=100),
    workflow_status: str | None = Query(default=None, alias="status", max_length=32),
    context: dict = Depends(_READ),
) -> dict:
    try:
        return {"workflows": _workflow_service(context["tenant_id"]).list(project_id=project_id, status=workflow_status)}
    except (PlatformNotFoundError, PlatformConflictError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.post("/workflows", response_model=OperatorWorkflowRead, status_code=status.HTTP_201_CREATED)
def create_workflow_draft(
    payload: OperatorWorkflowCreate,
    context: dict = Depends(_WRITE),
) -> dict:
    try:
        return _workflow_service(context["tenant_id"]).create(
            project_id=payload.project_id,
            name=payload.name,
            objective_template=payload.objective_template,
            plan=payload.plan,
            contract_revisions=payload.contract_revisions,
        )
    except (PlatformNotFoundError, PlatformConflictError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.get("/workflows/{workflow_id}", response_model=OperatorWorkflowRead)
def get_workflow_draft(workflow_id: str, context: dict = Depends(_READ)) -> dict:
    try:
        return _workflow_service(context["tenant_id"]).get(workflow_id)
    except (PlatformNotFoundError, PlatformConflictError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.post("/workflows/{workflow_id}/review", response_model=OperatorWorkflowRead)
def review_workflow_draft(
    workflow_id: str,
    payload: OperatorWorkflowReview,
    context: dict = Depends(_WRITE),
) -> dict:
    try:
        return _workflow_service(context["tenant_id"]).review(
            workflow_id,
            reviewer=payload.reviewer or str(context.get("user_id") or "operator"),
            approve=payload.approve,
        )
    except (PlatformNotFoundError, PlatformConflictError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


@router.post("/workflows/{workflow_id}/enable", response_model=OperatorWorkflowRead)
def enable_workflow_draft(workflow_id: str, context: dict = Depends(_WRITE)) -> dict:
    try:
        return _workflow_service(context["tenant_id"]).enable(workflow_id)
    except (PlatformNotFoundError, PlatformConflictError, PlatformValidationError) as exc:
        raise _map_errors(exc) from exc


# ---------------------------------------------------------------------
# 10.3 Runs
# ---------------------------------------------------------------------

@router.get("/runs", response_model=OperatorRunList)
def list_runs(
    project_id: str | None = Query(default=None, max_length=100),
    run_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    context: dict = Depends(_READ),
) -> dict:
    service = _operator_service(context["tenant_id"])
    try:
        runs = service.list_runs(project_id=project_id, status=run_status, limit=limit)
    except OperatorError as exc:
        raise _map_errors(exc) from exc
    return {"runs": runs}


@router.delete("/runs", response_model=OperatorRunClearResult)
def clear_terminal_runs(
    project_id: str = Query(..., min_length=1, max_length=100),
    context: dict = Depends(_WRITE),
) -> dict:
    """Clear terminal history for one project without touching live runs."""
    service = _operator_service(context["tenant_id"])
    try:
        return {"cleared": service.clear_terminal_runs(project_id=project_id)}
    except OperatorError as exc:
        raise _map_errors(exc) from exc


@router.post(
    "/runs",
    response_model=OperatorRunRead,
    status_code=status.HTTP_201_CREATED,
)
def start_run(
    payload: OperatorRunCreate,
    context: dict = Depends(_WRITE),
) -> dict:
    """Launch a manual objective and drive it until it waits or finishes.

    ``execution_mode=sync`` preserves the historical behaviour.  Dashboard
    and channel clients can use ``execution_mode=queued`` to receive the
    durable run immediately and let the worker publish progress events.
    """
    service = _operator_service(context["tenant_id"])
    try:
        run = service.start_run(
            payload.profile_id,
            project_id=payload.project_id,
            objective=payload.objective,
            context={
                **(payload.context or {}),
                **({"workflow_id": payload.workflow_id} if payload.workflow_id else {}),
            },
            idempotency_key=payload.idempotency_key,
            created_by=str(context.get("user_id") or "dashboard"),
            auto_execute=payload.execution_mode == "sync",
        )
        # Replayed idempotent starts return without steps; normalize the shape.
        return service.get_run(str(run["id"]), include_steps=True)
    except OperatorError as exc:
        raise _map_errors(exc) from exc


@router.get("/runs/{run_id}", response_model=OperatorRunRead)
def get_run(run_id: str, context: dict = Depends(_READ)) -> dict:
    service = _operator_service(context["tenant_id"])
    try:
        return service.get_run(run_id)
    except OperatorError as exc:
        raise _map_errors(exc) from exc


@router.post("/runs/{run_id}/resume", response_model=OperatorRunRead)
def resume_run(
    run_id: str,
    payload: OperatorRunActionRequest | None = None,
    context: dict = Depends(_WRITE),
) -> dict:
    """Wake a queued/waiting/sleeping run and continue its loop.

    A run waiting for approval resumes only after that approval is resolved;
    resolving first then resuming keeps the required order (§29 states).
    """
    service = _operator_service(context["tenant_id"])
    reason = payload.reason if payload is not None else ""
    try:
        return service.resume_run(run_id, reason=reason or "manual")
    except OperatorError as exc:
        raise _map_errors(exc) from exc


@router.post("/runs/{run_id}/cancel", response_model=OperatorRunRead)
def cancel_run(
    run_id: str,
    payload: OperatorRunActionRequest | None = None,
    context: dict = Depends(_WRITE),
) -> dict:
    service = _operator_service(context["tenant_id"])
    reason = payload.reason if payload is not None else ""
    try:
        return service.cancel_run(run_id, reason=reason)
    except OperatorError as exc:
        raise _map_errors(exc) from exc


# ---------------------------------------------------------------------
# 10.4 Steps / history
# ---------------------------------------------------------------------

@router.get("/runs/{run_id}/steps", response_model=list[OperatorRunStepRead])
def list_run_steps(run_id: str, context: dict = Depends(_READ)) -> list[dict]:
    """Bounded, redacted step history for one run.

    Steps are sanitized at write time by the runtime store (secret-shaped text
    and sensitive JSON keys become ``[REDACTED]``); this endpoint adds no
    credential-bearing field of its own. History length is inherently bounded
    by the profile's max_steps (1-100).
    """
    service = _operator_service(context["tenant_id"])
    try:
        service.get_run(run_id)  # tenant-scoped existence check (404 otherwise)
        with service.store.connection() as db:
            return opstore.steps_for_run(db, run_id)
    except OperatorError as exc:
        raise _map_errors(exc) from exc


# ---------------------------------------------------------------------
# 10.5 Approvals — reuse of the existing operator approval records
# ---------------------------------------------------------------------

@router.get("/approvals", response_model=OperatorApprovalList)
def list_approvals(
    approval_status: ApprovalStatus | None = Query(default="pending", alias="status"),
    project_id: str | None = Query(default=None, max_length=100),
    context: dict = Depends(_READ),
) -> dict:
    service = _operator_service(context["tenant_id"])
    try:
        if approval_status == "pending":
            approvals = service.list_pending_approvals(project_id=project_id)
        else:
            # Same records, other statuses. Tenant filter always applies, so a
            # project owned by another tenant yields an empty list, not data.
            with service.store.connection() as db:
                query = "SELECT * FROM operator_approvals WHERE tenant_id=?"
                params: list[Any] = [context["tenant_id"]]
                if project_id:
                    query += " AND project_id=?"
                    params.append(project_id)
                if approval_status is not None:
                    query += " AND status=?"
                    params.append(approval_status)
                rows = db.execute(
                    f"{query} ORDER BY created_at DESC", params,
                ).fetchall()
            approvals = [opstore.approval_public(row) for row in rows]
    except OperatorError as exc:
        raise _map_errors(exc) from exc
    return {"approvals": approvals}


@router.post(
    "/approvals/{approval_id}/resolve",
    response_model=OperatorApprovalRead,
)
def resolve_approval(
    approval_id: str,
    payload: OperatorApprovalResolveRequest,
    context: dict = Depends(_WRITE),
) -> dict:
    """Resolve one approval ('approved' or 'denied').

    Denying fails the waiting run with error_code 'approval_denied'. After an
    approval, resume the run via POST /runs/{run_id}/resume.
    """
    service = _operator_service(context["tenant_id"])
    try:
        return service.resolve_approval(
            approval_id,
            decision=payload.decision,
            resolved_by=payload.resolved_by or str(context.get("user_id") or "dashboard"),
            note=payload.note,
        )
    except OperatorError as exc:
        raise _map_errors(exc) from exc


# ---------------------------------------------------------------------
# 10.6 Automations
# ---------------------------------------------------------------------

@router.get("/automations/templates", response_model=list[OperatorAutomationTemplateRead])
def list_automation_templates(context: dict = Depends(_READ)) -> list[dict]:
    """Safe shipped templates the UI can offer as one-click starting points."""
    return [
        {
            "key": key,
            "name": template["name"],
            "trigger_type": template["trigger_type"],
            "trigger_config": template["trigger_config"],
            "objective_title": template.get("objective_title", ""),
            "objective_template": template["objective_template"],
            "action": template.get("action"),
        }
        for key, template in AUTOMATION_TEMPLATES.items()
    ]


@router.get("/automations", response_model=OperatorAutomationList)
def list_automations(
    project_id: str | None = Query(default=None, max_length=100),
    enabled: bool | None = Query(default=None),
    trigger_type: str | None = Query(default=None),
    context: dict = Depends(_READ),
) -> dict:
    service = _automation_service(context["tenant_id"])
    try:
        automations = service.list(
            project_id=project_id, enabled=enabled, trigger_type=trigger_type,
        )
    except PlatformValidationError as exc:
        raise _map_errors(exc) from exc
    return {"automations": automations}


@router.post(
    "/automations",
    response_model=OperatorAutomationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_automation(
    payload: OperatorAutomationCreate,
    context: dict = Depends(_WRITE),
) -> dict:
    service = _automation_service(context["tenant_id"])
    try:
        return service.create(**payload.model_dump())
    except (PlatformValidationError, PlatformNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.get("/automations/{automation_id}", response_model=OperatorAutomationRead)
def get_automation(automation_id: str, context: dict = Depends(_READ)) -> dict:
    service = _automation_service(context["tenant_id"])
    try:
        return service.get(automation_id)
    except (PlatformValidationError, PlatformNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.patch("/automations/{automation_id}", response_model=OperatorAutomationRead)
def update_automation(
    automation_id: str,
    payload: OperatorAutomationUpdate,
    context: dict = Depends(_WRITE),
) -> dict:
    service = _automation_service(context["tenant_id"])
    patch = {key: value for key, value in payload.model_dump().items() if value is not None}
    if "action" in payload.model_fields_set:
        patch["action"] = payload.action
    try:
        return service.update(automation_id, patch=patch)
    except (PlatformValidationError, PlatformNotFoundError) as exc:
        raise _map_errors(exc) from exc


@router.delete(
    "/automations/{automation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_automation(automation_id: str, context: dict = Depends(_WRITE)) -> None:
    service = _automation_service(context["tenant_id"])
    try:
        service.delete(automation_id)
    except (PlatformValidationError, PlatformNotFoundError) as exc:
        raise _map_errors(exc) from exc


# ---------------------------------------------------------------------
# 10.7 Diagnostics / policy reads
# ---------------------------------------------------------------------

@router.get("/system-explanation")
def system_explanation(context: dict = Depends(_READ)) -> dict:
    """Machine-readable component explanation with live health surfaces."""
    return explain_system(default_component_registry())


@router.get("/actions")
def list_actions(
    risk_class: str | None = Query(default=None),
    context: dict = Depends(_READ),
) -> dict:
    """The action registry: risk, approval mode, capability, bounds metadata.

    Read-only policy visibility for the UI; no mutation happens here. Durable
    approval-mode overrides (admin-managed through Session 2's boundary) are
    reflected in ``effective_approval_policy``.
    """
    engine = OperatorPolicyEngine(store=PlatformStore())
    try:
        descriptors = engine.registry.list(risk_class=risk_class)
    except PlatformValidationError as exc:
        raise _map_errors(exc) from exc
    actions = []
    for descriptor in descriptors:
        override = engine.get_override(descriptor.name)
        effective = (
            descriptor.with_overrides(approval_policy=override)
            if override is not None else descriptor
        )
        actions.append({
            "name": descriptor.name,
            "risk_class": descriptor.risk_class,
            "required_capability": descriptor.required_capability,
            "base_approval_policy": descriptor.approval_policy,
            "override": override,
            "effective_approval_policy": effective.approval_policy,
            "reversible": descriptor.reversible,
            "idempotent": descriptor.idempotent,
            "requires_verification": descriptor.requires_verification,
            "timeout_seconds": descriptor.timeout_seconds,
            "bounds": descriptor.bounds,
            "description": descriptor.description,
        })
    return {"actions": actions, "risk_classes": list(RISK_CLASSES)}
