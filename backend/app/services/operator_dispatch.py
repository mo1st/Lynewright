"""Integration seam between the Operator runtime and its producers/consumers.

Session 6 wiring only: every function here adapts two finished subsystems to
each other using the contracts their handoffs froze; no owned module was
redesigned.

- Session 4 -> 3: pending rows in ``operator_objective_requests`` (the generic
  dispatch contract in ``operator_objectives.py``) are claimed and driven
  through ``OperatorService`` runs, then settled back onto the request.
- Session 5 -> 3: pending rows in ``interaction_requests`` are routed to an
  operator profile, answered through the outbound response queue, and high-risk
  outcomes surface as interaction approval actions.

All run-state names use the shared vocabulary from the master plan:
queued running waiting_tool waiting_job waiting_approval sleeping completed
failed cancelled. Risk classes stay in session G's vocabulary.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable

CONSUMER_LABEL = "operator-dispatch"

# Sources whose results have no live human conversation attached (plan §14):
# their outcomes are delivered through the notification gateway instead.
_AUTOMATED_OBJECTIVE_SOURCES = {"schedule", "event", "condition"}

# Deterministic, safe-to-show reply used when no enabled Operator profile
# exists for a channel request (plan §15). Never contains exception detail.
OPERATOR_UNCONFIGURED_REPLY = (
    "Internal Operator is not configured yet. Ask a workspace admin to enable "
    "an Internal Operator profile in Lynewright, then send your request again."
)

# DispatchEvent callback contract: sync drains report lifecycle moments as
# small dicts; the hosted cycle turns them into notification-gateway sends.
DispatchEvent = dict[str, Any]

_RESERVED_ARGUMENT_KEYS = {"project_id", "require_verified"}


def _requested_project_id(actor: Any, arguments: dict[str, Any] | None) -> str | None:
    """The project an external (MCP) call works in.

    An explicit project_id wins. Without one, use the project of the
    tenant's active chat link when there is exactly one: that is the
    operator's working project. Otherwise stay unscoped, never a guessed
    literal (an MCP operator once saw "0 runs" because its calls landed in
    an empty default scope while requests used the linked project).
    """
    explicit = str((arguments or {}).get("project_id") or "").strip()
    if explicit:
        return explicit
    tenant_id = str(getattr(actor, "tenant_id", "") or "")
    try:
        from contextlib import closing
        from app.database import get_connection

        with closing(get_connection()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT project_id FROM interaction_channel_links "
                "WHERE status = 'active' AND (? = '' OR tenant_id = ?) AND TRIM(COALESCE(project_id, '')) <> ''",
                (tenant_id, tenant_id),
            ).fetchall()
    except Exception:
        return None
    projects = [str(row[0]) for row in rows]
    return projects[0] if len(projects) == 1 else None

# Keys a provider declares alongside an operation's JSON Schema. They are tool
# metadata, not schema keywords, so they are stripped before the schema is
# published to a caller.
_OPERATION_METADATA_KEYS = frozenset({
    "read_only", "risk_class", "caller_capability", "mcp_exposed", "description",
    "run_actions", "review_items", "review_submit",
})

# An unrecognized risk name normalizes to PUBLICATION rather than to the
# permissive default: a label the vocabulary does not know must never end up
# *less* guarded than the approval path it should have taken.
_UNKNOWN_RISK_FALLBACK = "PUBLICATION"


def _declared_operation_metadata(schema: Any) -> dict[str, Any] | None:
    """Read the bounded tool metadata a provider declared for one operation.

    Returns ``None`` when the operation must not become a tool. This fails
    closed on purpose: an operation that declares no caller capability has no
    authority the tool layer is entitled to assume on its behalf, and guessing
    one would let a manifest widen its own access simply by omitting a field.
    """
    from app.services.tool_models import RISK_CLASSES

    if not isinstance(schema, dict):
        return None
    caller_capability = str(schema.get("caller_capability") or "").strip()
    if not caller_capability:
        return None
    read_only = bool(schema.get("read_only", False))
    declared_risk = str(schema.get("risk_class") or "").strip()
    if read_only:
        risk_class = "READ_ONLY"
    elif declared_risk in RISK_CLASSES:
        risk_class = declared_risk
    else:
        risk_class = _UNKNOWN_RISK_FALLBACK
    return {
        "caller_capability": caller_capability,
        "read_only": read_only,
        "risk_class": risk_class,
        "destructive": risk_class == "DESTRUCTIVE",
        # Reaching external MCP clients is a separate, explicit decision from
        # being callable inside Lynewright. Absent declaration means not exposed.
        "mcp_exposed": bool(schema.get("mcp_exposed", False)),
        "description": str(schema.get("description") or "").strip(),
        "run_actions": schema.get("run_actions") if isinstance(schema.get("run_actions"), list) else [],
        "review_items": schema.get("review_items") if isinstance(schema.get("review_items"), dict) else None,
        "review_submit": schema.get("review_submit") if isinstance(schema.get("review_submit"), dict) else None,
        "schema": schema,
    }


def discover_contract_operations(contributions: Any) -> list[tuple[str, str, dict[str, Any]]]:
    """Enumerate declared contract operations that may become tools.

    A production line is not a new runtime object: it is whatever an activated
    integration declared through ``provides``. Reading that declaration here is
    what lets a new line appear without editing this module.
    """
    found: list[tuple[str, str, dict[str, Any]]] = []
    for contribution in contributions.list(kind="service"):
        metadata = getattr(contribution, "metadata", None) or {}
        contract = str(metadata.get("contract") or "").strip()
        if not contract.startswith("lynewright."):
            continue
        schemas = metadata.get("operation_schemas")
        if not isinstance(schemas, dict):
            continue
        capabilities = metadata.get("capabilities")
        names = (
            [str(value) for value in capabilities]
            if isinstance(capabilities, list) else list(schemas)
        )
        # Carried alongside the declared metadata (not just discovered here)
        # so a caller that needs the declaring integration's own identity --
        # operator_automation.py's scheduled dispatch, to read that
        # integration's requested capabilities -- never has to re-walk
        # contributions itself (Phase 16j).
        integration_id = str(getattr(contribution, "integration_id", "") or "")
        for operation in names:
            declared = _declared_operation_metadata(schemas.get(operation))
            if declared is None:
                continue
            found.append((contract, operation, {**declared, "integration_id": integration_id}))
    return found


def find_schedulable_operation(contract: str, operation: str) -> dict[str, Any] | None:
    """The declared metadata for one contract operation, only when its own
    schema opts into ``"schedulable": true``. Carries the declaring
    contribution's ``integration_id`` (Phase 16j), so a caller can look up
    that integration's own requested capabilities without a second walk.

    A thin, router-import-confined lookup for operator_automation.py (core,
    no integration imports of its own) so a scheduled automation's action
    can be gated the same way the tool runtime discovers operations, without
    that module reaching into app.routers.integration_gateway itself.
    """
    try:
        from app.routers.integration_gateway import _contributions
    except Exception:
        return None
    try:
        for found_contract, found_operation, declared in discover_contract_operations(_contributions):
            if found_contract == contract and found_operation == operation:
                if bool(declared.get("schema", {}).get("schedulable")):
                    return declared
                return None
    except Exception:
        return None
    return None


def _contract_registry() -> Any:
    """The shared IntegrationServiceContractRegistry over the host singletons.

    Built lazily exactly as Session 2's handoff documents for agent/tool-layer
    callers, so domain sources registered on the Integration Host resolve here
    without importing any adapter.

    ``sdk_factory`` was missing here until this fix. capability_dispatch.py's
    own docstring names the exact failure: "no IntegrationSDKFactory was
    constructed outside tests. An sdk_scoped provider fails closed without
    one, so those providers had been compiled and registered and never once
    invoked." That module exists to solve precisely this, and this function
    was simply never wired to it -- so every sdk_scoped contract operation
    reached via the tool runtime (MCP or Operator) raised "no SDK factory is
    available" the first time anything actually called one. Discovered by
    running edition.materialize through the live tool runtime rather than
    only through a hand-built registry or a fake SDK, which is what every
    earlier check of this seam had done.

    This dispatch path has never been tenant-parameterized -- ``_integrations``
    is the Integration Host's default-tenant singleton -- so the SDK factory
    stays on the same tenant for consistency rather than threading a tenant_id
    through every caller of this function for a seam that is single-tenant
    today regardless.
    """
    from app.routers.integration_gateway import _contributions, _integrations
    from app.services.capability_dispatch import get_sdk_factory
    from app.services.integration_capability_evidence import (
        IntegrationCapabilityEvidenceService,
    )
    from app.services.integration_provider_selection import (
        IntegrationProviderSelectionService,
    )
    from app.services.integration_service_contracts import (
        IntegrationServiceContractRegistry,
    )
    from app.services.tenant_schema import DEFAULT_TENANT_ID

    return IntegrationServiceContractRegistry(
        _contributions,
        evidence=IntegrationCapabilityEvidenceService(_integrations),
        selections=IntegrationProviderSelectionService(_integrations),
        sdk_factory=get_sdk_factory(DEFAULT_TENANT_ID),
    )


# ----------------------------------------------------------------------
# Profile selection helper (shared by both dispatch paths)
# ----------------------------------------------------------------------

def select_operator_profile(
    operator: Any, *, project_id: str | None, profile_id: str | None = None,
) -> dict[str, Any] | None:
    """Pick an enabled profile, honouring a channel's explicit selection first."""
    profiles = [
        profile for profile in operator.list_profiles()
        if profile.get("enabled")
    ]
    selected_id = str(profile_id or "")
    if selected_id:
        for profile in profiles:
            if str(profile.get("id") or "") != selected_id:
                continue
            profile_project_id = str(profile.get("project_id") or "")
            if not profile_project_id or profile_project_id == str(project_id or ""):
                return profile
        # An explicit selection must not silently turn into a different role.
        return None
    for profile in profiles:
        if str(profile.get("project_id") or "") == str(project_id or ""):
            return profile
    for profile in profiles:
        if not str(profile.get("project_id") or ""):
            return profile
    return None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _wake_due(run: dict[str, Any]) -> bool:
    wake_at = str(run.get("wake_at") or "")
    if not wake_at:
        return False
    try:
        due = datetime.fromisoformat(wake_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return due <= datetime.now(UTC)


# ----------------------------------------------------------------------
# Seam 4 -> 3: objective requests to Operator runs
# ----------------------------------------------------------------------

def _settle_request_from_run(objectives: Any, request: dict[str, Any], run: dict[str, Any]) -> str:
    status = str(run.get("status"))
    if status == "completed":
        objectives.complete(request["id"], result_summary=str(run.get("summary") or "")[:2000])
        return "completed"
    if status == "failed":
        objectives.fail(
            request["id"],
            error_code=str(run.get("error_code") or "operator_run_failed")[:64],
            message=str(run.get("error_summary") or "")[:500],
        )
        return "failed"
    if status == "cancelled":
        objectives.cancel(request["id"], reason=str(run.get("error_summary") or "run cancelled")[:500])
        return "cancelled"
    return status


def _emit_dispatch_event(on_event: Callable[[DispatchEvent], None] | None, event: DispatchEvent) -> None:
    """Report a dispatch lifecycle moment; a broken observer never breaks a drain."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:
        pass


def dispatch_operator_objective_requests(
    *, limit: int = 5, operator: Any = None, objectives: Any = None,
    on_event: Callable[[DispatchEvent], None] | None = None,
) -> dict[str, int]:
    """Claim pending objective requests and drive them through runs (4->3).

    Per Session 4's dispatch contract: claim succeeds only from ``pending``;
    an already-accepted request with a ``run_ref`` means an existing run should
    progress (execute queued runs, settle terminal ones, wake due sleepers) —
    never restart it. Non-terminal waiting states belong to their own wake
    sources (job workers, approvals), so they are left untouched here.

    ``on_event`` (production wiring) receives lifecycle events for settled
    requests and approval holds so the hosted cycle can route deterministic
    notifications without this drain ever touching the gateway itself.
    """
    from app.services.operator_objectives import OperatorObjectiveService
    from app.services.operator_runtime import OperatorService

    objectives = objectives or OperatorObjectiveService()
    operator = operator or OperatorService()
    counts = {"claimed": 0, "progressed": 0, "settled": 0}
    tenant_id = str(getattr(objectives, "tenant_id", ""))

    def event(kind: str, request: dict[str, Any], **extra: Any) -> None:
        _emit_dispatch_event(on_event, {
            "kind": kind,
            "tenant_id": tenant_id,
            "request_id": request.get("id"),
            "project_id": request.get("project_id"),
            "source": request.get("source"),
            "title": request.get("title"),
            "automation_id": request.get("automation_id"),
            "run_id": request.get("run_ref") or "",
            **extra,
        })

    for request in objectives.list(status="accepted", limit=max(limit, 50)):
        run_ref = str(request.get("run_ref") or "")
        if not run_ref:
            continue
        try:
            run = operator.get_run(run_ref)
        except Exception:
            objectives.fail(request["id"], error_code="operator_run_missing",
                            message="Referenced operator run no longer exists.")
            counts["settled"] += 1
            event("objective_settled", request, status="failed",
                  error_code="operator_run_missing", summary="")
            continue
        status = str(run.get("status"))
        if status == "queued":
            run = operator.execute_run(run_ref)
            counts["progressed"] += 1
        elif status == "sleeping" and _wake_due(run):
            run = operator.resume_run(run_ref, reason="operator-dispatch:sleep-due")
            counts["progressed"] += 1
        outcome = _settle_request_from_run(objectives, request, run)
        if outcome in {"completed", "failed", "cancelled"}:
            counts["settled"] += 1
            event("objective_settled", {**request, "run_ref": run_ref}, status=outcome,
                  error_code=str(run.get("error_code") or ""),
                  summary=str(run.get("summary") or run.get("error_summary") or ""))
        elif outcome == "waiting_approval":
            event("objective_waiting_approval", {**request, "run_ref": run_ref},
                  approval_id=str(run.get("pending_approval_id") or ""),
                  action=str(run.get("pending_approval_action") or ""))

    for request in objectives.list(status="pending", limit=limit):
        profile = select_operator_profile(operator, project_id=request.get("project_id"))
        if profile is None:
            # Phase 10: terminal rejection — no retry storm, no unbounded
            # pending accumulation. Manual/UI consumers surface the reason as
            # a configuration error; automated sources additionally raise one
            # deduped configuration notification via the hosted cycle.
            objectives.reject(request["id"], reason="no_enabled_operator_profile")
            counts["settled"] += 1
            event("objective_unconfigured", request)
            continue
        context: dict[str, Any] = {}
        raw_context = request.get("context_json")
        if isinstance(raw_context, str) and raw_context:
            try:
                context = json.loads(raw_context)
            except ValueError:
                context = {}
        try:
            run = operator.start_run(
                profile["id"],
                project_id=request.get("project_id") or "",
                objective=request["objective"],
                idempotency_key=f"opr:{request['id']}",
                created_by=f"operator-objectives:{request.get('source') or 'manual'}",
                context=context,
                auto_execute=False,
            )
            objectives.claim(request["id"], run_ref=run["id"])
            counts["claimed"] += 1
            log_dispatch_event(
                level="info", message="operator.objective.dispatched",
                details={"event": "operator.objective.dispatched",
                         "request_id": request.get("id"), "run_id": run["id"],
                         "source": request.get("source"),
                         "profile_id": profile.get("id")},
                tenant_id=tenant_id or None,
                project_id=str(request.get("project_id") or "") or None,
            )
        except Exception:
            objectives.fail(request["id"], error_code="operator_start_failed",
                            message="Operator could not start a run for this request.")
            counts["settled"] += 1
            event("objective_settled", request, status="failed",
                  error_code="operator_start_failed", summary="")
            continue
        final_run = operator.execute_run(run["id"])
        outcome = _settle_request_from_run(objectives, request, final_run)
        if outcome in {"completed", "failed", "cancelled"}:
            counts["settled"] += 1
            event("objective_settled", {**request, "run_ref": run["id"]}, status=outcome,
                  error_code=str(final_run.get("error_code") or ""),
                  summary=str(final_run.get("summary") or final_run.get("error_summary") or ""))
        elif outcome == "waiting_approval":
            event("objective_waiting_approval", {**request, "run_ref": run["id"]},
                  approval_id=str(final_run.get("pending_approval_id") or ""),
                  action=str(final_run.get("pending_approval_action") or ""))
    return counts


def dispatch_queued_manual_runs(
    *, limit: int = 5, operator: Any = None, tenant_id: str = "",
) -> dict[str, int]:
    """Drive queued runs that are not owned by another dispatch producer.

    Dashboard/MCP callers may intentionally choose ``execution_mode=queued``
    without creating an objective or interaction envelope.  Those durable runs
    still need a hosted consumer; otherwise they remain queued forever after a
    successful ``POST /operator/runs``.  Objective and interaction-backed runs
    are excluded here because their dedicated drains own their lifecycle.

    The Operator executor lease remains the final duplicate guard, so this
    drain is safe across multiple API workers and a concurrent resume request.
    """
    from contextlib import closing

    from app.database import get_connection
    from app.services.operator_runtime import OperatorService

    service = operator or OperatorService(tenant_id=tenant_id)
    scoped_tenant = str(tenant_id or getattr(service, "tenant_id", "") or "")
    if not scoped_tenant:
        return {"checked": 0, "progressed": 0, "failed": 0}
    bounded_limit = max(1, min(100, int(limit)))
    with closing(get_connection()) as db:
        tables = {
            str(row[0]) for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        exclusions = []
        if "operator_objective_requests" in tables:
            exclusions.append(
                "NOT EXISTS (SELECT 1 FROM operator_objective_requests AS o "
                "WHERE o.run_ref=r.id AND o.status IN ('pending','accepted'))"
            )
        if "operator_interaction_outcomes" in tables:
            exclusions.append(
                "NOT EXISTS (SELECT 1 FROM operator_interaction_outcomes AS i "
                "WHERE i.run_id=r.id)"
            )
        where = " AND ".join(["r.tenant_id=?", "r.status='queued'", *exclusions])
        rows = db.execute(
            f"SELECT r.id FROM operator_runs AS r WHERE {where} "
            "ORDER BY r.updated_at ASC, r.id ASC LIMIT ?",
            (scoped_tenant, bounded_limit),
        ).fetchall()
    counts = {"checked": len(rows), "progressed": 0, "failed": 0}
    for row in rows:
        run_id = str(row["id"])
        try:
            service.execute_run(run_id)
            counts["progressed"] += 1
        except Exception as exc:  # one queued run must not starve the batch
            counts["failed"] += 1
            log_dispatch_failure(
                message="Queued manual Operator run could not be progressed.",
                exc=exc, tenant_id=scoped_tenant, unit="manual_runs",
            )
    return counts


# ----------------------------------------------------------------------
# Seam 5 -> 3: interaction requests to Operator runs and replies back
# ----------------------------------------------------------------------

def _reply_text_for_run(run: dict[str, Any]) -> str:
    """Render an operator run state as a channel reply (Session 3 snippet)."""
    status = str(run.get("status"))
    if status == "completed":
        return str(run.get("summary") or "Done.")
    if status == "waiting_approval":
        return "I need your approval to continue."
    if status in {"waiting_job", "sleeping", "waiting_tool"}:
        return f"Working on it — currently {run.get('wake_reason') or status}."
    if status == "failed":
        return f"Could not finish: {run.get('error_summary') or 'unknown error'}"
    if status == "cancelled":
        return "This request was cancelled."
    return "Queued."


def _media_refs_for_run(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return explicit managed/artifact media refs for a terminal run.

    The channel layer is intentionally provider-neutral.  A provider may
    return ``media_refs``/``artifacts`` in its result envelope; the host keeps
    those references and lets every channel choose its native upload method.
    Arbitrary URLs and plain filesystem paths are ignored by the extractor.
    """
    from app.services.integration_artifacts import extract_media_refs

    context = run.get("context") if isinstance(run.get("context"), dict) else {}
    wake = context.get("wake_observation") if isinstance(context, dict) else None
    refs = extract_media_refs(wake)
    if refs:
        return refs
    for step in run.get("steps") or []:
        if not isinstance(step, dict):
            continue
        refs = extract_media_refs(step.get("output"))
        if refs:
            return refs
    return []


def _async_poll_due(run: dict[str, Any]) -> bool:
    """Whether a provider-declared operation is due for a model-free poll."""
    operation = run.get("pending_operation")
    if not isinstance(operation, dict):
        return False
    candidate = str(operation.get("next_poll_at") or "")
    if not candidate:
        return True
    try:
        due = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        return due <= datetime.now(UTC)
    except ValueError:
        return True


_CONVERSATION_RESET_COMMANDS = frozenset({"/new", "/break", "/breack"})


def _conversation_history(*, tenant_id: str, conversation_id: str,
                          current_message_id: str = "") -> list[dict[str, str]]:
    """Return bounded channel context since the latest explicit task reset."""
    from app.services.interaction_conversations import list_messages

    messages = list_messages(
        tenant_id=tenant_id, conversation_id=conversation_id, limit=60,
    )
    boundary = -1
    for index, message in enumerate(messages):
        if (
            str(message.get("direction") or "") == "inbound"
            and str(message.get("text") or "").strip().lower() in _CONVERSATION_RESET_COMMANDS
        ):
            boundary = index
    history: list[dict[str, str]] = []
    total = 0
    for message in messages[boundary + 1:]:
        if str(message.get("id") or "") == current_message_id:
            continue
        message_text = str(message.get("text") or "").strip()
        if not message_text:
            continue
        excerpt = message_text[:1500]
        if total + len(excerpt) > 12_000:
            continue
        total += len(excerpt)
        history.append({
            "role": "user" if message.get("direction") == "inbound" else "assistant",
            "text": excerpt,
        })
    return history[-30:]


async def route_interaction_requests(
    *, limit: int = 10, operator: Any = None, create_actions: bool = True,
    operator_factory: Callable[[str], Any] | None = None,
) -> dict[str, int]:
    """Route claimed interaction requests to same-tenant Operator runs (5->3).

    Tenant safety (plan §8.2): ``claim_pending_requests`` may return requests
    for several tenants. When no explicit ``operator`` is supplied, every
    request is served by an ``OperatorService`` bound to *its own* tenant via a
    small per-iteration tenant->service cache, so a Tenant A request can never
    select a Tenant B (or global/default) profile. Supplying ``operator``
    explicitly pins one tenant-bound service (used by seam tests and single-
    tenant callers); production wiring never does.
    """
    from app.services.interaction_actions import (
        bind_action_prompt,
        buttons_for,
        create_action,
        find_pending_action_for_run,
        restrict_pending_action_to_approve_refuse,
    )
    from app.services.interaction_conversations import (
        get_conversation,
        set_run_binding,
    )
    from app.services.interaction_linking import get_connection_row
    from app.services.interaction_requests import (
        claim_pending_requests,
        mark_request_failed,
        submit_response,
    )
    from app.services.operator_runtime import OperatorService

    tenant_operators: dict[str, Any] = {}

    def operator_for(request: dict[str, Any]) -> Any:
        if operator is not None:
            return operator
        request_tenant = str(request["tenant_id"])
        cached = tenant_operators.get(request_tenant)
        if cached is None:
            cached = (
                operator_factory(request_tenant) if operator_factory is not None
                else OperatorService(tenant_id=request_tenant)
            )
            tenant_operators[request_tenant] = cached
        return cached

    counts = {"routed": 0, "answered": 0, "held_for_approval": 0, "failed": 0}
    claimed = claim_pending_requests(limit=limit, consumer=CONSUMER_LABEL)

    for request in claimed:
        conversation = get_conversation(
            tenant_id=request["tenant_id"], conversation_id=request["conversation_id"]
        )
        if conversation is None:
            mark_request_failed(request["id"], "conversation vanished")
            counts["failed"] += 1
            continue
        project_id = str(conversation.get("project_id") or "")
        bound_operator = operator_for(request)
        connection = get_connection_row(
            tenant_id=str(request["tenant_id"]),
            connection_id=str(conversation.get("connection_id") or ""),
        )
        profile = select_operator_profile(
            bound_operator,
            project_id=project_id,
            profile_id=str((connection or {}).get("operator_profile_id") or ""),
        )
        if profile is None:
            # Phase 10: a deterministic configuration notice goes back to the
            # channel instead of a silent failure or a leaked exception.
            try:
                await submit_response(
                    tenant_id=request["tenant_id"],
                    project_id=project_id,
                    conversation_id=request["conversation_id"],
                    text=OPERATOR_UNCONFIGURED_REPLY,
                    actor="system",
                    request_id=request["id"],
                )
                counts["answered"] += 1
            except Exception as exc:
                mark_request_failed(request["id"], f"response failed: {exc}"[:500])
                counts["failed"] += 1
            continue
        text = str(request.get("text") or "").strip() or "[media message]"
        raw_payload = request.get("payload_json") or request.get("payload") or {}
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except ValueError:
                raw_payload = {}
        message_row_id = str(
            raw_payload.get("message_row_id") if isinstance(raw_payload, dict) else ""
        )
        history = _conversation_history(
            tenant_id=str(request["tenant_id"]),
            conversation_id=str(request["conversation_id"]),
            current_message_id=message_row_id,
        )
        try:
            # Persist the run->conversation outcome binding before the first
            # model call. A service restart can interrupt synchronous
            # execution; previously that happened before the outcome row was
            # created, leaving the request permanently dispatched with no
            # recovery path or final Telegram reply.
            run = bound_operator.start_run(
                profile["id"],
                project_id=project_id,
                objective=text,
                idempotency_key=f"irq:{request['id']}",
                created_by=f"interaction:{request['conversation_id']}",
                context={
                    "interaction_request_id": request["id"],
                    "kind": request.get("kind"),
                    "conversation_id": request["conversation_id"],
                    "conversation_history": history,
                },
                auto_execute=False,
            )
            _track_pending_interaction_outcome(
                run=run,
                tenant_id=str(request["tenant_id"]),
                project_id=project_id,
                conversation_id=str(request["conversation_id"]),
                request_id=str(request["id"]),
            )
            run = bound_operator.execute_run(str(run["id"]))
        except Exception as exc:
            mark_request_failed(request["id"], f"operator start failed: {exc}"[:500])
            counts["failed"] += 1
            continue
        counts["routed"] += 1
        log_dispatch_event(
            level="info", message="operator.interaction.dispatched",
            details={"event": "operator.interaction.dispatched",
                     "request_id": request["id"], "run_id": run["id"],
                     "conversation_id": request["conversation_id"],
                     "profile_id": profile.get("id")},
            tenant_id=str(request["tenant_id"]), project_id=project_id or None,
        )
        set_run_binding(
            tenant_id=request["tenant_id"],
            conversation_id=request["conversation_id"],
            run_id=run["id"],
            run_state=run["status"],
        )
        action: dict[str, Any] | None = None
        if create_actions and run["status"] == "waiting_approval":
            action = find_pending_action_for_run(
                tenant_id=str(request["tenant_id"]),
                conversation_id=str(request["conversation_id"]),
                run_id=str(run["id"]),
            )
            if action is None:
                action = create_action(
                    tenant_id=request["tenant_id"],
                    project_id=project_id,
                    conversation_id=request["conversation_id"],
                    payload_ref=json.dumps({
                        "approval_id": run.get("pending_approval_id"),
                        "run_id": run["id"],
                    }),
                    summary=f"Operator requests approval: {str(run.get('pending_approval_action') or '')}"[:2000],
                    # Telegram intentionally exposes only the two decisions
                    # requested by the product: approve or refuse.
                    decision_options=("approve", "reject"),
                    run_id=run["id"],
                )
            else:
                restrict_pending_action_to_approve_refuse(
                    tenant_id=str(request["tenant_id"]), action_id=str(action["id"]),
                )
                action["decision_options"] = ["approve", "reject"]
            counts["held_for_approval"] += 1
        try:
            response = await submit_response(
                tenant_id=request["tenant_id"],
                project_id=project_id,
                conversation_id=request["conversation_id"],
                text=_reply_text_for_run(run),
                actor="operator",
                request_id=request["id"],
                buttons=list(buttons_for(action)) if action is not None else None,
            )
            if action is not None and response.get("delivery_status") == "sent":
                external_message_id = str(response.get("sent_external_message_id") or "")
                if external_message_id:
                    bind_action_prompt(
                        tenant_id=str(request["tenant_id"]),
                        action_id=str(action["id"]),
                        prompt_ref=f"{conversation['external_chat_id']}:{external_message_id}",
                    )
            counts["answered"] += 1
            if str(run.get("status")) in _TERMINAL_RUN_STATES:
                _resolve_pending_interaction_outcome(str(run["id"]))
        except Exception as exc:
            mark_request_failed(request["id"], f"response failed: {exc}"[:500])
            counts["failed"] += 1
    return counts


def apply_resolved_interaction_approvals(
    *, operator: Any = None, limit: int = 200
) -> int:
    """Carry resolved interaction approval actions into the Operator (5->3).

    Resolution does NOT auto-resume operator runs (Session 3 contract), so this
    closes the loop: approve -> resolve_approval + resume_run; reject/request_changes
    -> denial verdict (run fails with error_code=approval_denied). Double
    application is impossible: operator approvals transition once and the
    resume of a non-waiting run raises OperatorConflict, both swallowed after
    the first successful application.

    The caller must supply (or default to) an ``operator`` bound to a single
    tenant: only that tenant's interaction actions are read and only that
    tenant's approvals/runs are touched (plan §8.3).
    """
    import json as _json

    from app.services.interaction_actions import list_actions
    from app.services.operator_runtime import OperatorConflict, OperatorNotFound, OperatorService

    operator = operator or OperatorService()
    applied = 0
    verdict_for_status = {"approved": "approved", "rejected": "denied",
                          "changes_requested": "denied"}
    bounded_limit = max(1, min(int(limit), 500))
    for action in list_actions(tenant_id=operator.tenant_id, limit=bounded_limit):
        verdict = verdict_for_status.get(str(action.get("status")))
        if verdict is None:
            continue
        raw = str(action.get("payload_ref") or "")
        if not raw:
            continue
        try:
            ref = _json.loads(raw)
        except ValueError:
            continue
        approval_id = str(ref.get("approval_id") or "")
        run_id = str(ref.get("run_id") or "")
        if not approval_id:
            continue
        try:
            operator.resolve_approval(approval_id, decision=verdict,
                                      resolved_by=f"interaction:{action['id']}",
                                      note=str(action.get("status")))
        except (OperatorConflict, OperatorNotFound):
            continue
        applied += 1
        log_dispatch_event(
            level="info", message="operator.approval.applied",
            details={"event": "operator.approval.applied",
                     "approval_id": approval_id, "run_id": run_id,
                     "action_id": action.get("id"), "verdict": verdict},
            tenant_id=str(operator.tenant_id),
        )
        if run_id and verdict == "approved":
            try:
                operator.resume_run(run_id, reason=f"interaction:{action['id']}")
            except (OperatorConflict, OperatorNotFound):
                pass
    return applied


# ----------------------------------------------------------------------
# Phase 8: completion semantics — the final answer after "Working on it"
# ----------------------------------------------------------------------

# Durable record of interaction-started runs that have not yet delivered a
# final channel answer. Created in the Python init path (no migrations/*.sql
# execution anywhere); rows are removed once the outcome is delivered.
_OUTCOME_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS operator_interaction_outcomes (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    last_state TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_operator_interaction_outcomes_tenant
    ON operator_interaction_outcomes(tenant_id, created_at);
"""

_TERMINAL_RUN_STATES = frozenset({
    "completed", "failed", "cancelled",
})


def _outcome_connection() -> Any:
    """Open a connection with the interaction + outcome schema ensured.

    Schema lives in the Python init path (nothing executes migrations/*.sql);
    ensuring per use follows the same pattern as the automation services.
    """
    from app.database import get_connection, init_db
    from app.services.interaction_schema import init_interaction_schema

    init_db()

    class _Ctx:
        def __enter__(self):
            self.db = get_connection()
            init_interaction_schema(self.db)
            self.db.executescript(_OUTCOME_SCHEMA_SQL)
            return self.db

        def __exit__(self, *exc: Any) -> None:
            self.db.close()

    return _Ctx()


def _track_pending_interaction_outcome(
    *, run: dict[str, Any], tenant_id: str, project_id: str,
    conversation_id: str, request_id: str,
) -> None:
    """Remember a still-open interaction run so its final answer is guaranteed.

    Runs that already finished before the first reply are skipped: that first
    reply *is* the final answer. Best-effort by contract — a tracking failure
    must never fail the routing that started the run.
    """
    try:
        status = str(run.get("status"))
        if status in _TERMINAL_RUN_STATES:
            return
        with _outcome_connection() as db, db:
            db.execute(
                """INSERT INTO operator_interaction_outcomes
                   (run_id,tenant_id,project_id,conversation_id,request_id,last_state)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET last_state=excluded.last_state""",
                (str(run["id"]), tenant_id, project_id[:120], conversation_id,
                 request_id, status),
            )
    except Exception as exc:
        log_dispatch_event(
            level="warning", message="Could not track interaction run for completion.",
            details={"run_id": str(run.get("id"))}, exc=exc,
        )


def _resolve_pending_interaction_outcome(run_id: str) -> None:
    """Forget an outcome after its terminal reply was delivered."""
    try:
        with _outcome_connection() as db, db:
            db.execute(
                "DELETE FROM operator_interaction_outcomes WHERE run_id=?", (run_id,),
            )
    except Exception as exc:
        log_dispatch_event(
            level="warning", message="Could not resolve delivered interaction outcome.",
            details={"run_id": run_id}, exc=exc,
        )


def discard_interaction_outcome(run_id: str) -> None:
    """Stop future channel delivery for a task explicitly reset by its user."""
    _resolve_pending_interaction_outcome(run_id)


async def reconcile_interaction_outcomes(
    *, limit: int = 10, operator_factory: Callable[[str], Any] | None = None,
) -> dict[str, int]:
    """Deliver final answers for interaction runs that reached an end state.

    This closes plan §13's invariant: a channel user never receives only
    "Working on it". For each tracked run (oldest first, bounded per tick):

    - terminal  -> send the final summary to the bound conversation, mark the
      tracking row resolved, and refresh the conversation binding;
    - vanished  -> resolve without sending (nothing can be answered);
    - otherwise -> refresh the stored state and wait for a later tick.

    Delivery is at-least-once: a crash between send and resolve can repeat one
    final summary, which is safe because run completion itself is idempotent.
    """
    from app.services.interaction_actions import (
        create_action,
        deliver_action,
        find_pending_action_for_approval,
        restrict_pending_action_to_approve_refuse,
    )
    from app.services.interaction_conversations import get_conversation, set_run_binding
    from app.services.interaction_linking import get_connection_row
    from app.services.interaction_requests import submit_response
    from app.services.operator_runtime import OperatorNotFound, OperatorService

    bounded = max(1, min(int(limit), 100))
    with _outcome_connection() as db:
        rows = db.execute(
            """SELECT * FROM operator_interaction_outcomes
               ORDER BY created_at ASC, run_id ASC LIMIT ?""",
            (bounded,),
        ).fetchall()
        tracked = [dict(row) for row in rows]

    operators: dict[str, Any] = {}

    def operator_for(tenant_id: str) -> Any:
        cached = operators.get(tenant_id)
        if cached is None:
            cached = (
                operator_factory(tenant_id) if operator_factory is not None
                else OperatorService(tenant_id=tenant_id)
            )
            operators[tenant_id] = cached
        return cached

    counts = {"checked": len(tracked), "finalized": 0, "resolved_missing": 0,
              "resumed": 0, "still_open": 0, "failed": 0,
              "approval_actions_created": 0, "approvals_delivered": 0}

    def resolve_row(row: dict[str, Any]) -> None:
        with _outcome_connection() as db, db:
            db.execute(
                "DELETE FROM operator_interaction_outcomes WHERE run_id=?",
                (row["run_id"],),
            )

    def touch_row(row: dict[str, Any], state: str) -> None:
        with _outcome_connection() as db, db:
            db.execute(
                "UPDATE operator_interaction_outcomes SET last_state=? WHERE run_id=?",
                (state[:64], row["run_id"]),
            )

    for row in tracked:
        tenant_id = str(row["tenant_id"])
        try:
            run = operator_for(tenant_id).get_run(str(row["run_id"]))
        except OperatorNotFound:
            resolve_row(row)
            counts["resolved_missing"] += 1
            continue
        except Exception as exc:
            counts["failed"] += 1
            log_dispatch_event(
                level="error", message="Outcome reconciliation could not read a run.",
                details={"run_id": str(row["run_id"]), "tenant_id": tenant_id}, exc=exc,
                tenant_id=tenant_id,
            )
            continue
        status = str(run.get("status"))
        if status in {"queued", "running"}:
            # These states should normally be driven to a boundary inside the
            # dispatch call. Seeing one on a later tick means execution was
            # interrupted (most commonly by a deploy/restart), so resume the
            # durable run instead of leaving Telegram silent forever.
            try:
                import asyncio

                run = await asyncio.to_thread(
                    operator_for(tenant_id).execute_run, str(row["run_id"]),
                )
                status = str(run.get("status"))
                counts["resumed"] += 1
            except Exception as exc:
                counts["failed"] += 1
                log_dispatch_event(
                    level="warning", message="Interrupted interaction run could not resume.",
                    details={"run_id": str(row["run_id"]), "tenant_id": tenant_id},
                    exc=exc, tenant_id=tenant_id,
                )
                continue
        if status == "waiting_approval":
            # A resumed run can request a second approval. The initial route
            # creates its channel envelope, but outcome reconciliation must do
            # the same for every later approval boundary, including after a
            # restart. Identity is the approval id, not only the run id.
            approval_id = str(run.get("pending_approval_id") or "")
            if approval_id:
                try:
                    approval = operator_for(tenant_id).get_approval(approval_id)
                    conversation = get_conversation(
                        tenant_id=tenant_id, conversation_id=str(row["conversation_id"]),
                    )
                    connection = get_connection_row(
                        tenant_id=tenant_id,
                        connection_id=str((conversation or {}).get("connection_id") or ""),
                    ) if conversation else None
                    action = find_pending_action_for_approval(
                        tenant_id=tenant_id,
                        conversation_id=str(row["conversation_id"]),
                        run_id=str(row["run_id"]),
                        approval_id=approval_id,
                    )
                    if action is None:
                        action = create_action(
                            tenant_id=tenant_id,
                            project_id=str(row["project_id"] or ""),
                            conversation_id=str(row["conversation_id"]),
                            payload_ref=json.dumps({
                                "approval_id": approval_id,
                                "run_id": str(row["run_id"]),
                            }),
                            summary=str(approval.get("summary") or "Operator approval requested.")[:2000],
                            decision_options=("approve", "reject"),
                            run_id=str(row["run_id"]),
                        )
                        counts["approval_actions_created"] += 1
                    else:
                        restrict_pending_action_to_approve_refuse(
                            tenant_id=tenant_id, action_id=str(action["id"]),
                        )
                        action["decision_options"] = ["approve", "reject"]
                    prompt_note = str(action.get("resolution_note") or "")
                    if connection and not prompt_note.startswith("prompt:"):
                        delivery = await deliver_action(
                            connection=connection,
                            action=action,
                            intro_text="I need your approval to continue.",
                        )
                        if bool(delivery.get("send_result", {}).get("ok")):
                            counts["approvals_delivered"] += 1
                    elif connection is None:
                        raise LookupError("Approval conversation connection not found.")
                except Exception as exc:
                    counts["failed"] += 1
                    log_dispatch_event(
                        level="warning", message="Pending approval prompt could not be reconciled.",
                        details={"run_id": str(row["run_id"]), "approval_id": approval_id},
                        exc=exc, tenant_id=tenant_id,
                    )
            touch_row(row, status)
            counts["still_open"] += 1
            continue
        if status == "waiting_job" and _async_poll_due(run):
            # Provider-declared async work is polled by the runtime itself;
            # the model is never asked to repeat the write or invent a task id.
            try:
                import asyncio

                run = await asyncio.to_thread(
                    operator_for(tenant_id).resume_run,
                    str(row["run_id"]), reason="integration-operation:poll",
                )
                status = str(run.get("status"))
                counts["resumed"] += 1
            except Exception as exc:
                counts["failed"] += 1
                log_dispatch_event(
                    level="warning", message="Async integration operation could not be polled.",
                    details={"run_id": str(row["run_id"]), "tenant_id": tenant_id},
                    exc=exc, tenant_id=tenant_id,
                )
                continue
        if status not in _TERMINAL_RUN_STATES:
            touch_row(row, status)
            counts["still_open"] += 1
            continue
        # Terminal delivery needs the bounded structured step/result envelope,
        # not only the model's prose summary.  This is what carries an
        # integration's managed video/image/document reference to Telegram or
        # another future channel.
        try:
            run = operator_for(tenant_id).get_run(str(row["run_id"]), include_steps=True)
        except Exception:
            pass
        try:
            await submit_response(
                tenant_id=tenant_id,
                project_id=str(row["project_id"] or ""),
                conversation_id=str(row["conversation_id"]),
                text=_reply_text_for_run(run),
                media_refs=_media_refs_for_run(run),
                actor="operator",
                request_id=str(row["request_id"]) if row["request_id"] else None,
            )
        except Exception as exc:
            # The conversation or request may be gone; stop retrying either way
            # so one poisoned row cannot block the rest of the queue forever.
            counts["failed"] += 1
            log_dispatch_event(
                level="warning", message="Final interaction reply could not be delivered.",
                details={"run_id": str(row["run_id"]), "tenant_id": tenant_id},
                exc=exc, tenant_id=tenant_id,
            )
            resolve_row(row)
            continue
        resolve_row(row)
        try:
            set_run_binding(
                tenant_id=tenant_id,
                conversation_id=str(row["conversation_id"]),
                run_id=row["run_id"],
                run_state=status,
            )
        except Exception:
            pass  # display-only binding; delivery already succeeded
        counts["finalized"] += 1
        log_dispatch_event(
            level="info", message="operator.interaction.completed",
            details={"event": "operator.interaction.completed",
                     "run_id": str(row["run_id"]), "final_state": status},
            tenant_id=tenant_id, project_id=str(row["project_id"]) or None,
        )
    return counts


# ----------------------------------------------------------------------
# Phase 3/11: the hosted dispatch cycle — production wiring, backpressure,
# per-tenant isolation, and deterministic notifications (Phase 9)
# ----------------------------------------------------------------------

def log_dispatch_event(
    *, level: str, message: str, details: dict[str, Any] | None = None,
    exc: BaseException | None = None, tenant_id: str | None = None,
    project_id: str | None = None,
) -> None:
    """Bounded structured logging under the shared application-log conventions."""
    try:
        from app.services.application_logging import log_event

        log_event(
            level=level, category="operator_dispatch", source="hosted_cycle",
            message=message, details=details or {}, exc=exc,
            tenant_id=tenant_id, project_id=project_id,
        )
    except Exception:
        pass


def default_project_for_notification(tenant_id: str, project_id: str | None) -> str:
    """Resolve the notification scope for a request without an explicit project."""
    if project_id:
        return str(project_id)
    try:
        from app.services.tenancy import default_project_for_tenant

        return str(default_project_for_tenant(tenant_id) or "")
    except Exception:
        return ""


async def _send_objective_result_notification(event: DispatchEvent) -> bool:
    """Route one automated objective outcome through the notification gateway.

    Deterministic text only — the Operator is never woken to deliver it. The
    gateway dedupes on ``notification_key``, so replays of the same event are
    no-ops at the transport layer.
    """
    from app.services.notification_gateway import send_notification

    tenant_id = str(event.get("tenant_id") or "")
    project_id = default_project_for_notification(tenant_id, event.get("project_id"))
    if not tenant_id or not project_id:
        return False
    status = str(event.get("status"))
    title = str(event.get("title") or event.get("request_id") or "Operator objective")
    error_code = str(event.get("error_code") or "")
    summary = str(event.get("summary") or "")[:500]
    if status == "completed":
        heading = f"Operator finished: {title}"
        body = summary or "Completed."
    elif status == "cancelled":
        heading = f"Operator cancelled: {title}"
        body = summary or "The run was cancelled."
    else:
        heading = f"Operator failed: {title}"
        body = f"{error_code}: {summary}" if error_code else (summary or "Failed.")
    result = await send_notification(
        tenant_id=tenant_id,
        project_id=project_id,
        event_class="operator_result",
        title=heading,
        body=body,
        intent=None,
        dedupe_key=f"operator-result:{event.get('request_id')}",
        actor="system",
        data={
            "request_id": event.get("request_id"),
            "run_id": event.get("run_id"),
            "status": status,
        },
    )
    log_dispatch_event(
        level="info", message="operator.notification.sent",
        details={"event": "operator.notification.sent", "notification_class": "operator_result",
                 "request_id": event.get("request_id"), "statuses": [
                     item.get("status") for item in result.get("results", [])
                 ]},
        tenant_id=tenant_id, project_id=project_id,
    )
    return True


async def _send_approval_needed_notification(event: DispatchEvent) -> bool:
    from app.services.notification_gateway import send_notification

    tenant_id = str(event.get("tenant_id") or "")
    project_id = default_project_for_notification(tenant_id, event.get("project_id"))
    approval_id = str(event.get("approval_id") or "")
    if not tenant_id or not project_id or not approval_id:
        return False
    action = str(event.get("action") or "sensitive change")
    await send_notification(
        tenant_id=tenant_id,
        project_id=project_id,
        event_class="approval",
        title=f"Operator paused: approval required ({action})",
        body="Approve or reject this action from your configured channel or dashboard.",
        intent="IMMEDIATE",
        dedupe_key=f"operator-approval:{approval_id}",
        actor="system",
        data={"approval_id": approval_id, "run_id": event.get("run_id"),
              "request_id": event.get("request_id")},
    )
    log_dispatch_event(
        level="info", message="operator.notification.sent",
        details={"event": "operator.notification.sent", "notification_class": "approval",
                 "approval_id": approval_id},
        tenant_id=tenant_id, project_id=project_id,
    )
    return True


async def _send_unconfigured_automation_notification(event: DispatchEvent) -> bool:
    """Tell the owner a scheduled automation fired with no Operator to run it.

    Deduped per tenant/automation/day so a persistent misconfiguration nags
    daily rather than every tick.
    """
    from app.services.notification_gateway import send_notification

    tenant_id = str(event.get("tenant_id") or "")
    automation_id = str(event.get("automation_id") or "")
    project_id = default_project_for_notification(tenant_id, event.get("project_id"))
    if not tenant_id or not project_id:
        return False
    day_bucket = datetime.now(UTC).strftime("%Y%m%d")
    title = str(event.get("title") or "Scheduled objective")
    await send_notification(
        tenant_id=tenant_id,
        project_id=project_id,
        event_class="operator_configuration",
        title="Internal Operator is not configured",
        body=(f"A scheduled objective ({title}) could not run because no enabled "
              "Operator profile exists. Configure the Internal Operator to resume "
              "automations."),
        intent="IMMEDIATE",
        dedupe_key=f"operator-config:{tenant_id}:{automation_id}:{day_bucket}",
        actor="system",
        data={"automation_id": automation_id, "request_id": event.get("request_id")},
    )
    log_dispatch_event(
        level="warning", message="operator.objective.configuration_blocked",
        details={"event": "operator.objective.configuration_blocked",
                 "automation_id": automation_id, "request_id": event.get("request_id")},
        tenant_id=tenant_id, project_id=project_id,
    )
    return True


async def _flush_dispatch_notifications(events: list[DispatchEvent], limit: int) -> int:
    """Turn drain lifecycle events into notification-gateway sends (bounded).

    Only automated sources produce gateway notifications: manual/UI results are
    read in the product, and Telegram flows receive their answers in-channel.
    """
    sent = 0
    for event in events[:limit]:
        kind = str(event.get("kind"))
        source = str(event.get("source") or "")
        try:
            if kind == "objective_settled":
                if source in _AUTOMATED_OBJECTIVE_SOURCES:
                    if await _send_objective_result_notification(event):
                        sent += 1
            elif kind == "objective_waiting_approval":
                if source in _AUTOMATED_OBJECTIVE_SOURCES:
                    if await _send_approval_needed_notification(event):
                        sent += 1
            elif kind == "objective_unconfigured":
                if source in _AUTOMATED_OBJECTIVE_SOURCES:
                    if await _send_unconfigured_automation_notification(event):
                        sent += 1
        except Exception as exc:
            log_dispatch_event(
                level="warning", message="Objective notification could not be routed.",
                details={"kind": kind, "request_id": event.get("request_id")}, exc=exc,
                tenant_id=str(event.get("tenant_id") or "") or None,
            )
    return sent


class _TenantServiceCache:
    """Per-iteration tenant -> (OperatorService, OperatorObjectiveService).

    Instances live only for one dispatch cycle; long-lived caching belongs to
    the hosted automation tick, which prunes on tenant deactivation. An
    ``operator_factory`` (tests, alternative embeddings) replaces the default
    ``OperatorService(tenant_id=...)`` construction but receives the exact
    tenant id — the tenant boundary itself is not negotiable.
    """

    def __init__(self, operator_factory: Callable[[str], Any] | None = None) -> None:
        self._operators: dict[str, Any] = {}
        self._objectives: dict[str, Any] = {}
        self._operator_factory = operator_factory

    def operator(self, tenant_id: str) -> Any:
        cached = self._operators.get(tenant_id)
        if cached is None:
            if self._operator_factory is not None:
                cached = self._operator_factory(tenant_id)
            else:
                from app.services.operator_runtime import OperatorService

                cached = OperatorService(tenant_id=tenant_id)
            self._operators[tenant_id] = cached
        return cached

    def objectives(self, tenant_id: str) -> Any:
        from app.services.operator_objectives import OperatorObjectiveService

        cached = self._objectives.get(tenant_id)
        if cached is None:
            cached = OperatorObjectiveService(tenant_id=tenant_id)
            self._objectives[tenant_id] = cached
        return cached


async def run_hosted_dispatch_cycle(
    *, tick_seconds: float | None = None,
    operator_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """One production dispatch iteration across every active tenant.

    Executed on the hosted scheduler's event loop by
    ``sync_scheduler._run_hosted_operator_cycle`` — never via ``asyncio.run``.
    Per plan §8.4 the order is:

    1. apply resolved interaction approvals per tenant (so approved runs
       resume before new work is claimed),
    2. dispatch pending/accepted objective requests per tenant,
    3. route pending interaction requests,
    4. reconcile interaction outcomes so completed runs always emit their
       final channel answer.

    Every unit is isolated (plan §8.5): a failing tenant or drain logs and
    continues; nothing here can kill the scheduler. Backpressure (§8.6/§16):
    tenants, objectives, interactions and reconciliations are each bounded per
    iteration via configuration, so a backlog drains across ticks instead of
    starving ``run_due_syncs()``.
    """
    import asyncio

    from app.config import get_settings
    from app.services.tenant_enumerator import list_active_tenant_ids

    settings = get_settings()
    totals: dict[str, int] = {
        "approvals_applied": 0, "objectives_claimed": 0, "objectives_progressed": 0,
        "objectives_settled": 0, "interactions_routed": 0, "interactions_failed": 0,
        "manual_runs_progressed": 0, "manual_runs_failed": 0,
        "outcomes_finalized": 0, "notifications_sent": 0,
    }
    failed_units = 0
    events: list[DispatchEvent] = []
    services = _TenantServiceCache(operator_factory)
    tenant_ids = list_active_tenant_ids()
    from app.services.operator_runtime_overrides import effective_int

    objectives_limit = max(1, effective_int(
        "platform.operator.objectives_per_tick", int(settings.operator_objectives_per_tick)))
    interactions_limit = max(1, effective_int(
        "platform.operator.interactions_per_tick", int(settings.operator_interactions_per_tick)))
    notify_limit = interactions_limit + objectives_limit * 2

    # 1. approvals — sync drain off the event loop, per tenant.
    for tenant_id in tenant_ids:
        try:
            applied = await asyncio.to_thread(
                apply_resolved_interaction_approvals,
                operator=services.operator(tenant_id),
            )
            totals["approvals_applied"] += applied
        except Exception as exc:
            failed_units += 1
            log_dispatch_failure(message="Approval drain failed.", exc=exc,
                                 tenant_id=tenant_id, unit="approvals")

    # 2. objective requests — sync drain off the event loop, per tenant.
    for tenant_id in tenant_ids:
        try:
            manual_runs = await asyncio.to_thread(
                dispatch_queued_manual_runs,
                limit=objectives_limit,
                operator=services.operator(tenant_id),
                tenant_id=tenant_id,
            )
            totals["manual_runs_progressed"] += manual_runs["progressed"]
            totals["manual_runs_failed"] += manual_runs["failed"]
        except Exception as exc:
            failed_units += 1
            log_dispatch_failure(message="Queued manual-run drain failed.", exc=exc,
                                 tenant_id=tenant_id, unit="manual_runs")
        try:
            counts = await asyncio.to_thread(
                dispatch_operator_objective_requests,
                limit=objectives_limit,
                operator=services.operator(tenant_id),
                objectives=services.objectives(tenant_id),
                on_event=events.append,
            )
            totals["objectives_claimed"] += counts["claimed"]
            totals["objectives_progressed"] += counts["progressed"]
            totals["objectives_settled"] += counts["settled"]
        except Exception as exc:
            failed_units += 1
            log_dispatch_failure(message="Objective drain failed.", exc=exc,
                                 tenant_id=tenant_id, unit="objectives")

    # 3. interaction requests — async drain awaited directly on this loop;
    #    routing resolves each request's tenant internally.
    try:
        routed = await route_interaction_requests(
            limit=interactions_limit, operator_factory=operator_factory
        )
        totals["interactions_routed"] += routed["routed"]
        totals["interactions_failed"] += routed["failed"]
    except Exception as exc:
        failed_units += 1
        log_dispatch_failure(message="Interaction drain failed.", exc=exc,
                             tenant_id=None, unit="interactions")

    # 4. completion semantics — deliver any final answers now due.
    try:
        reconciled = await reconcile_interaction_outcomes(
            limit=interactions_limit, operator_factory=operator_factory
        )
        totals["outcomes_finalized"] += reconciled["finalized"]
    except Exception as exc:
        failed_units += 1
        log_dispatch_failure(message="Outcome reconciliation failed.", exc=exc,
                             tenant_id=None, unit="outcomes")

    # 5. deterministic notifications for automated results/approval holds.
    try:
        totals["notifications_sent"] += await _flush_dispatch_notifications(
            events, limit=notify_limit
        )
    except Exception as exc:
        failed_units += 1
        log_dispatch_failure(message="Notification flush failed.", exc=exc,
                             tenant_id=None, unit="notifications")

    if any(totals.values()) or failed_units:
        log_dispatch_event(
            level="info" if not failed_units else "warning",
            message="operator.dispatch.tick",
            details={
                "event": "operator.dispatch.tick",
                "tenants": len(tenant_ids), "failed_units": failed_units,
                **totals,
            },
        )
    return {**totals, "tenants": len(tenant_ids), "failed_units": failed_units}


def log_dispatch_failure(
    *, message: str, exc: BaseException, tenant_id: str | None, unit: str
) -> None:
    """Record one failing dispatch unit with its failure streak."""
    try:
        from app.services.application_logging import log_loop_failure

        log_loop_failure(
            category="operator_dispatch", source=f"drain_{unit}",
            message=message, exc=exc, tenant_id=tenant_id,
            details={"event": "operator.dispatch.failed", "unit": unit,
                     "tenant_id": tenant_id},
        )
    except Exception:
        pass
