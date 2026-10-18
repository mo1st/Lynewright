"""Internal service API for the BYOK Operator Agent.

Channel- and trigger-free entry points for Sessions E (automation) and F
(interaction/Telegram): create a profile, start a run, and drive or resume it
from any scheduler or conversation adapter. Everything is tenant/project
scoped, idempotent on request, and durable across process restarts.
"""
from __future__ import annotations

import sqlite3
import json
from typing import Any

from app.services.application_logging import log_event
from app.services.ai_provider import has_configured_api_key
from app.services.ai_task_provider import get_ai_settings_for_task
from app.services.operator_runtime import store as opstore
from app.services.operator_runtime.actor import (
    DEFAULT_OPERATOR_CAPABILITIES,
    validate_operator_capabilities,
)
from app.services.operator_runtime.loop import OperatorLoop
from app.services.operator_runtime.action_plan import validate_action_plan
from app.services.operator_runtime.schema import RUN_STATES, init_operator_schema
from app.services.platform_models import EventCreateInput
from app.services.platform_services import PlatformStore, _validated
from app.services.core_tools import CoreToolRuntime
from app.services.tenant_schema import DEFAULT_TENANT_ID

DEFAULT_MAX_STEPS = 12


class OperatorService:
    """Facade over the durable operator store, shared tool runtime, and loop.

    Tenant isolation follows the platform-service convention: each instance is
    bound to one tenant (``DEFAULT_TENANT_ID`` unless constructed otherwise),
    and every profile/run/approval read or write is filtered by that tenant.
    """

    def __init__(
        self, *, runtime: CoreToolRuntime | None = None,
        decision_fn: Any | None = None, store: PlatformStore | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        contributions = None
        if runtime is None:
            # The Operator is an internal transport of the same integration
            # host, not a second tool universe.  Only attach the process host
            # registry when it belongs to this tenant and to the default store;
            # custom/test stores must never see another tenant's contributions.
            contributions = None
            if store is None:
                from app.services.integration_host import contributions as host_contributions
                if str(host_contributions.integrations.tenant_id) == str(tenant_id):
                    contributions = host_contributions
            self.runtime = CoreToolRuntime(store=store)
        else:
            self.runtime = runtime
        # The operator sees only generic integration operations and configured
        # MCP mappings. An empty host contributes no tools.
        from app.services.operator_integration_tools import register_operator_integration_tools
        register_operator_integration_tools(self.runtime, tenant_id=tenant_id, contributions=contributions)
        self.store = self.runtime.store
        self.tenant_id = tenant_id
        self._loop = OperatorLoop(self.runtime, decision_fn=decision_fn)

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    def create_profile(
        self, name: str, *, project_id: str = "", instructions: str = "",
        granted_capabilities: list[str] | frozenset[str] | None = None,
        enabled: bool = True, max_steps: int = DEFAULT_MAX_STEPS,
        max_consecutive_tool_failures: int = 3, max_model_failures: int = 2,
        max_total_tokens: int = 0, max_model_decisions: int = 0,
        max_network_attempts: int = 0, no_progress_limit: int = 3,
        retrieval_mode: str = "hybrid_local", retrieval_canary_percent: int = 10,
        decision_policy: str = "cloud_only", needle_canary_percent: int = 0,
        needle_read_confidence: float = 0.92, needle_write_confidence: float = 0.97,
        needle_sensitive_confidence: float = 0.99,
    ) -> dict[str, Any]:
        capabilities = validate_operator_capabilities(
            granted_capabilities if granted_capabilities is not None else DEFAULT_OPERATOR_CAPABILITIES
        )
        tenant_id = self._tenant_for_project(project_id)
        self._validate_bounds(
            max_steps, max_consecutive_tool_failures, max_model_failures,
            max_total_tokens, max_model_decisions, max_network_attempts, no_progress_limit,
        )
        self._validate_retrieval(retrieval_mode, retrieval_canary_percent)
        self._validate_decision_policy(
            decision_policy, needle_canary_percent, needle_read_confidence,
            needle_write_confidence, needle_sensitive_confidence,
        )
        if not str(name).strip():
            raise opstore.OperatorValidationError("Operator profile requires a name.")
        with self.store.connection() as db, db:
            row = opstore.create_profile(
                db, tenant_id=tenant_id, project_id=project_id, name=name,
                instructions=instructions, granted_capabilities=capabilities,
                enabled=enabled, max_steps=max_steps,
                max_consecutive_tool_failures=max_consecutive_tool_failures,
                max_model_failures=max_model_failures,
                max_total_tokens=max_total_tokens, max_model_decisions=max_model_decisions,
                max_network_attempts=max_network_attempts, no_progress_limit=no_progress_limit,
                retrieval_mode=retrieval_mode, retrieval_canary_percent=retrieval_canary_percent,
                decision_policy=decision_policy, needle_canary_percent=needle_canary_percent,
                needle_read_confidence=needle_read_confidence,
                needle_write_confidence=needle_write_confidence,
                needle_sensitive_confidence=needle_sensitive_confidence,
            )
            return opstore.profile_public(row)

    def list_profiles(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.connection() as db:
            if project_id:
                tenant_id = self._tenant_for_project(project_id)
                rows = opstore.list_profiles(db, tenant_id=tenant_id, project_id=project_id)
                return [opstore.profile_public(row) for row in rows]
            rows = opstore.list_profiles(db, tenant_id=self.tenant_id)
            return [opstore.profile_public(row) for row in rows]

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            row = opstore.get_profile(db, tenant_id=self.tenant_id, profile_id=profile_id)
            return opstore.profile_public(row)

    def delete_profile(self, profile_id: str) -> dict[str, Any]:
        """Archive the profile so existing operator run history stays linked."""
        with self.store.connection() as db, db:
            row = opstore.archive_profile(db, tenant_id=self.tenant_id, profile_id=profile_id)
            return opstore.profile_public(row)

    def decision_model_status(self) -> dict[str, Any]:
        """Return safe local-model health and redacted run telemetry."""
        from app.services.operator_runtime.needle_backend import (
            configured_needle_mode, installed_needle_version, needle_engine_checksum,
        )

        package_version = installed_needle_version()
        engine_checksum = needle_engine_checksum()
        global_mode = configured_needle_mode()
        with self.store.connection() as db:
            judge_row = db.execute(
                """SELECT provider,model,enabled FROM ai_task_providers
                   WHERE task_type='operator_judge' LIMIT 1""",
            ).fetchone()
            profile_rows = db.execute(
                "SELECT decision_policy,COUNT(*) AS count FROM operator_profiles WHERE tenant_id=? GROUP BY decision_policy",
                (self.tenant_id,),
            ).fetchall()
            attempts = db.execute(
                """SELECT provider,outcome,prompt_chars FROM operator_ai_attempts
                   WHERE tenant_id=? ORDER BY created_at DESC LIMIT 5000""",
                (self.tenant_id,),
            ).fetchall()
            latency_rows = db.execute(
                """SELECT needle_latency_ms FROM operator_run_steps
                   WHERE tenant_id=? AND needle_latency_ms>0 ORDER BY needle_latency_ms""",
                (self.tenant_id,),
            ).fetchall()
            reason_rows = db.execute(
                """SELECT fallback_reason,COUNT(*) AS count FROM operator_run_steps
                   WHERE tenant_id=? AND fallback_reason!='' GROUP BY fallback_reason
                   ORDER BY count DESC LIMIT 8""",
                (self.tenant_id,),
            ).fetchall()
        local = [row for row in attempts if str(row["provider"] or "") == "local_needle"]
        accepted = sum(1 for row in local if str(row["outcome"] or "") == "accepted")
        fallbacks = sum(1 for row in local if str(row["outcome"] or "") == "fallback")
        total_local = len(local)
        latencies = [int(row["needle_latency_ms"] or 0) for row in latency_rows]

        def percentile(values: list[int], fraction: float) -> int:
            if not values:
                return 0
            index = min(len(values) - 1, max(0, int((len(values) - 1) * fraction)))
            return values[index]

        judge_provider = str(judge_row["provider"] or "") if judge_row is not None else ""
        judge_model = str(judge_row["model"] or "") if judge_row is not None else ""
        judge_enabled = bool(judge_row["enabled"]) if judge_row is not None else False
        try:
            judge_settings = get_ai_settings_for_task("operator_judge")
            judge_has_key = has_configured_api_key(judge_settings)
        except Exception:
            judge_has_key = False
        # A missing explicit route must not silently inherit the ordinary
        # operator-agent provider. The administrator must configure this route
        # deliberately in the existing AI task settings UI.
        judge_route_ready = bool(
            judge_row is not None and judge_enabled and judge_provider not in {"", "default", "disabled", "mock"}
            and judge_model and judge_has_key
        )
        needle_installed = package_version not in {"uninstalled", "unknown"}
        engine_verified = engine_checksum not in {"", "unknown"}
        ready_for_shadow = needle_installed and engine_verified and judge_route_ready
        ready_for_control = ready_for_shadow and global_mode == "canary"
        deployment_safe = global_mode == "off" or (needle_installed and engine_verified and judge_route_ready)

        return {
            "installed": needle_installed,
            "healthy": deployment_safe,
            "deployment_safe": deployment_safe,
            "needle_installed": needle_installed,
            "engine_verified": engine_verified,
            "judge_route_ready": judge_route_ready,
            "ready_for_shadow": ready_for_shadow,
            "ready_for_control": ready_for_control,
            "judge_provider": judge_provider,
            "judge_model": judge_model,
            "package": "cactus-needle",
            "package_version": package_version,
            "generation": "2",
            "engine_checksum": engine_checksum,
            "global_mode": global_mode,
            "profile_policies": {
                str(row["decision_policy"] or "cloud_only"): int(row["count"] or 0)
                for row in profile_rows
            },
            "acceptance_rate": accepted / total_local if total_local else 0,
            "fallback_rate": fallbacks / total_local if total_local else 0,
            "p50_latency_ms": percentile(latencies, 0.50),
            "p95_latency_ms": percentile(latencies, 0.95),
            "cloud_tokens_avoided": sum(max(0, int(row["prompt_chars"] or 0) // 4) for row in local if str(row["outcome"] or "") == "accepted"),
            "fallback_reasons": {
                str(row["fallback_reason"]): int(row["count"] or 0)
                for row in reason_rows
            },
        }

    def update_profile(self, profile_id: str, **fields: Any) -> dict[str, Any]:
        if "granted_capabilities" in fields and fields["granted_capabilities"] is not None:
            fields["granted_capabilities"] = validate_operator_capabilities(
                fields["granted_capabilities"]
            )
        if any(key in fields and fields[key] is not None for key in (
            "max_steps", "max_consecutive_tool_failures", "max_model_failures",
            "max_total_tokens", "max_model_decisions", "max_network_attempts", "no_progress_limit",
        )):
            with self.store.connection() as db:
                current = db.execute(
                    """SELECT max_steps,max_consecutive_tool_failures,max_model_failures,
                              max_total_tokens,max_model_decisions,max_network_attempts,no_progress_limit
                         FROM operator_profiles WHERE id=? AND tenant_id=?""",
                    (profile_id, self.tenant_id),
                ).fetchone()
                if current is None:
                    raise opstore.OperatorNotFound("Operator profile not found.")
                self._validate_bounds(
                    fields.get("max_steps") or current["max_steps"],
                    fields.get("max_consecutive_tool_failures") or current["max_consecutive_tool_failures"],
                    fields.get("max_model_failures") or current["max_model_failures"],
                    fields.get("max_total_tokens") if fields.get("max_total_tokens") is not None else current["max_total_tokens"],
                    fields.get("max_model_decisions") if fields.get("max_model_decisions") is not None else current["max_model_decisions"],
                    fields.get("max_network_attempts") if fields.get("max_network_attempts") is not None else current["max_network_attempts"],
                    fields.get("no_progress_limit") if fields.get("no_progress_limit") is not None else current["no_progress_limit"],
                )
        if "retrieval_mode" in fields or "retrieval_canary_percent" in fields:
            with self.store.connection() as db:
                current = db.execute(
                    "SELECT retrieval_mode,retrieval_canary_percent FROM operator_profiles WHERE id=? AND tenant_id=?",
                    (profile_id, self.tenant_id),
                ).fetchone()
                if current is None:
                    raise opstore.OperatorNotFound("Operator profile not found.")
                self._validate_retrieval(
                    fields.get("retrieval_mode") if fields.get("retrieval_mode") is not None else current["retrieval_mode"],
                    fields.get("retrieval_canary_percent") if fields.get("retrieval_canary_percent") is not None else current["retrieval_canary_percent"],
                )
        if any(key in fields and fields[key] is not None for key in (
            "decision_policy", "needle_canary_percent", "needle_read_confidence",
            "needle_write_confidence", "needle_sensitive_confidence",
        )):
            with self.store.connection() as db:
                current = db.execute(
                    """SELECT decision_policy,needle_canary_percent,needle_read_confidence,
                              needle_write_confidence,needle_sensitive_confidence
                         FROM operator_profiles WHERE id=? AND tenant_id=?""",
                    (profile_id, self.tenant_id),
                ).fetchone()
                if current is None:
                    raise opstore.OperatorNotFound("Operator profile not found.")
                self._validate_decision_policy(
                    fields.get("decision_policy") if fields.get("decision_policy") is not None else current["decision_policy"],
                    fields.get("needle_canary_percent") if fields.get("needle_canary_percent") is not None else current["needle_canary_percent"],
                    fields.get("needle_read_confidence") if fields.get("needle_read_confidence") is not None else current["needle_read_confidence"],
                    fields.get("needle_write_confidence") if fields.get("needle_write_confidence") is not None else current["needle_write_confidence"],
                    fields.get("needle_sensitive_confidence") if fields.get("needle_sensitive_confidence") is not None else current["needle_sensitive_confidence"],
                )
        # Profile changes must outlive this request.  Without the transaction
        # context SQLite rolls the update back when the connection closes,
        # even though the response built on that same connection looks saved.
        with self.store.connection() as db, db:
            row = opstore.update_profile(
                db, tenant_id=self.tenant_id, profile_id=profile_id,
                fields=fields,
            )
            return opstore.profile_public(row)

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def start_run(
        self, profile_id: str, *, project_id: str, objective: str,
        idempotency_key: str = "", created_by: str = "",
        context: dict[str, Any] | None = None, auto_execute: bool = True,
    ) -> dict[str, Any]:
        """Create (or replay) a run and optionally execute it synchronously."""
        tenant_id = self._tenant_for_project(project_id)
        with self.store.connection() as db, db:
            profile_row = opstore.get_profile(db, tenant_id=tenant_id, profile_id=profile_id)
            if not bool(profile_row["enabled"]):
                raise opstore.OperatorConflict("The operator profile is disabled.")
            run_context = dict(context or {}) if isinstance(context, dict) else {}
            workflow_id = str(run_context.pop("workflow_id", "") or "").strip()
            if workflow_id:
                # Only an explicitly enabled, tenant/project-scoped draft may
                # seed a run. The caller cannot inject an arbitrary action plan
                # through the public context field.
                workflow = db.execute(
                    "SELECT * FROM operator_workflow_drafts WHERE tenant_id=? AND id=? AND project_id=?",
                    (tenant_id, workflow_id, project_id),
                ).fetchone()
                if workflow is None:
                    raise opstore.OperatorNotFound("Operator workflow draft was not found for this project.")
                if str(workflow["status"]) != "enabled":
                    raise opstore.OperatorConflict("The selected workflow must be reviewed and enabled before use.")
                try:
                    stored_plan = json.loads(str(workflow["plan_json"] or "{}"))
                    normalized_plan = validate_action_plan(stored_plan)
                except Exception as exc:
                    raise opstore.OperatorValidationError("The enabled workflow no longer has a valid action plan.") from exc
                run_context["workflow_id"] = workflow_id
                run_context["action_plan"] = {
                    "version": 1,
                    "steps": normalized_plan["steps"],
                    "index": 0,
                    "outputs": {},
                    "output_refs": {},
                    "message": normalized_plan.get("message") or "Enabled workflow completed.",
                    "invalidated_reason": "",
                }
            from app.services.operator_runtime.actor import build_operator_actor
            from app.services.operator_runtime.needle_backend import (
                needle_engine_checksum, operation_schema_fingerprint,
            )
            actor = build_operator_actor(profile_row)
            authorized_operations = [
                item for item in self.runtime.list_definitions(actor)
                if item.get("authorized")
            ]
            run_row, replayed = opstore.create_run(
                db, profile_row=profile_row, tenant_id=tenant_id, project_id=project_id,
                objective=objective, context=run_context,
                idempotency_key=idempotency_key.strip(), created_by=created_by,
                decision_model_checksum=needle_engine_checksum(),
                tool_schema_fingerprint=operation_schema_fingerprint(authorized_operations),
            )
            run_public = opstore.run_public(run_row)
        self._log_run(run_public, "operator.run.created" if not replayed else "operator.run.replayed")
        if not replayed:
            self._emit_event("operator.run.created", run_public)
        if auto_execute and not replayed:
            return self.execute_run(str(run_public["id"]))
        return run_public

    def execute_run(self, run_id: str) -> dict[str, Any]:
        """Drive an existing queued/running run until it waits or finishes."""
        current = self.get_run(run_id)
        if current["status"] in opstore.TERMINAL_RUN_STATES:
            return current
        result = self._loop.execute_bound(run_id)
        final_status = str(result.get("status"))
        events = {
            "completed": ("operator.run.completed", "info"),
            "failed": ("operator.run.failed", "error"),
            "cancelled": ("operator.run.cancelled", "warning"),
        }
        event_type, level = events.get(final_status, ("operator.run.progress", "info"))
        waiting_statuses = {"waiting_tool", "waiting_job", "waiting_approval", "sleeping"}
        if final_status in waiting_statuses:
            event_type = "operator.run.waiting"
        self._log_run(result, event_type, level=level)
        self._emit_event(event_type, result)
        return self.get_run(run_id, include_steps=True)

    def resume_run(
        self, run_id: str, *, reason: str = "manual", force: bool = False,
    ) -> dict[str, Any]:
        """Wake a waiting/sleeping/queued run and continue its loop.

        ``reason`` is persisted on a resume step so other subsystems (job
        workers, approval UIs, schedulers, channels) leave an audit trail.
        A run waiting for approval resumes only once that approval is resolved;
        pass ``force=True`` to re-check explicitly.
        """
        with self.store.connection() as db, db:
            run_row = self._scoped_run(db, run_id)
            status = str(run_row["status"])
            if status in opstore.TERMINAL_RUN_STATES:
                raise opstore.OperatorConflict(f"Run is already {status}.")
            if status == "waiting_approval":
                pending = opstore.latest_pending_approval_for_run(db, run_id)
                if pending is not None and not force:
                    raise opstore.OperatorConflict(
                        "Run is waiting for approval; resolve the approval first."
                    )
            model_free_poll = str(reason).startswith("integration-operation:poll")
            updated = opstore.transition_run(
                db, run_id, expected_revision=int(run_row["revision"]),
                target_status="running", wake_reason="",
                current_step=(None if model_free_poll else int(run_row["current_step"]) + 1),
            )
            approved_tool = opstore.approved_tool_decision_for_run(db, run_id)
            if approved_tool is not None:
                try:
                    context = json.loads(str(updated["context_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    context = {}
                if not isinstance(context, dict):
                    context = {}
                context["approved_resume"] = approved_tool
                db.execute(
                    "UPDATE operator_runs SET context_json=? WHERE id=?",
                    (opstore.bound_json_text(context), run_id),
                )
            if not model_free_poll:
                opstore.append_step(
                    db, updated, sequence=int(updated["current_step"]), step_type="resume",
                    inputs={"reason": opstore.redact_text(reason, limit=500)},
                    output={"resumed_from": status},
                )
        return self.execute_run(run_id)

    def cancel_run(self, run_id: str, *, reason: str = "") -> dict[str, Any]:
        """Cancel any non-terminal run; the loop stops before its next action."""
        with self.store.connection() as db, db:
            run_row = self._scoped_run(db, run_id)
            if str(run_row["status"]) in opstore.TERMINAL_RUN_STATES:
                raise opstore.OperatorConflict("Only a non-terminal run can be cancelled.")
            pending = opstore.latest_pending_approval_for_run(db, run_id)
            if pending is not None:
                db.execute(
                    "UPDATE operator_approvals SET status='cancelled',resolved_at=CURRENT_TIMESTAMP,resolution_note=? WHERE id=?",
                    (opstore.redact_text(reason or "run cancelled", limit=2000), str(pending["id"])),
                )
            updated = opstore.transition_run(
                db, run_id, expected_revision=int(run_row["revision"]),
                target_status="cancelled", error_summary=reason,
            )
            public = opstore.run_public(updated)
        self._log_run(public, "operator.run.cancelled", level="warning")
        self._emit_event("operator.run.cancelled", public)
        # Return the same complete shape as GET/LIST, including usage
        # telemetry.  The dashboard can immediately render a cancelled run
        # without briefly receiving a partial terminal payload.
        return self.get_run(run_id, include_steps=True)

    def get_run(self, run_id: str, *, include_steps: bool = False) -> dict[str, Any]:
        with self.store.connection() as db:
            run_row = self._scoped_run(db, run_id)
            steps = opstore.steps_for_run(db, run_id) if include_steps else None
            public = opstore.run_public(run_row, steps=steps)
            public["usage_summary"] = opstore.ai_usage_summary(db, run_id)
            return public

    def list_runs(
        self, *, project_id: str | None = None, status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in RUN_STATES:
            raise opstore.OperatorValidationError(f"Unknown run status: {status}")
        with self.store.connection() as db:
            if project_id:
                tenant_id = self._tenant_for_project(project_id)
                rows = opstore.list_runs(
                    db, tenant_id=tenant_id, project_id=project_id, status=status, limit=limit,
                )
                return [
                    {**opstore.run_public(row), "usage_summary": opstore.ai_usage_summary(db, str(row["id"]))}
                    for row in rows
                ]
            rows = db.execute(
                "SELECT * FROM operator_runs WHERE tenant_id=? ORDER BY updated_at DESC, id DESC LIMIT ?",
                (self.tenant_id, max(1, min(int(limit), 500))),
            ).fetchall()
            return [
                {**opstore.run_public(row), "usage_summary": opstore.ai_usage_summary(db, str(row["id"]))}
                for row in rows
            ]

    def clear_terminal_runs(self, *, project_id: str) -> int:
        """Delete terminal run history for one project.

        Live runs are intentionally excluded so a housekeeping action can
        never interrupt work or discard a pending approval. Steps and
        approvals cascade from their parent run in the operator schema.
        """
        tenant_id = self._tenant_for_project(project_id)
        with self.store.connection() as db, db:
            cursor = db.execute(
                """DELETE FROM operator_runs
                   WHERE tenant_id=? AND project_id=?
                     AND status IN ('completed', 'failed', 'cancelled')""",
                (tenant_id, project_id),
            )
            cleared = max(0, int(cursor.rowcount))
        if cleared:
            log_event(
                level="info", category="operator", source="operator.runtime",
                message="Operator terminal run history cleared.",
                details={"project_id": project_id, "cleared": cleared},
                project_id=project_id,
            )
        return cleared

    # ------------------------------------------------------------------
    # Approvals — the waiting_approval boundary for Sessions F/G/E
    # ------------------------------------------------------------------

    def request_approval(
        self, run_id: str, *, action: str, summary: str,
        payload_ref: dict[str, Any] | None = None, requested_by: str = "",
    ) -> dict[str, Any]:
        """Pause a running run behind a human approval boundary."""
        with self.store.connection() as db, db:
            run_row = self._scoped_run(db, run_id)
            if str(run_row["status"]) not in {"running", "queued"}:
                raise opstore.OperatorConflict("Only a queued or running run can request approval.")
            approval = opstore.create_approval(
                db, run_row, action=action, summary=summary,
                payload_ref=payload_ref or {}, requested_by=requested_by,
            )
            updated = opstore.transition_run(
                db, run_id, expected_revision=int(run_row["revision"]),
                target_status="waiting_approval", wake_reason="approval",
                pending_approval_id=str(approval["id"]),
            )
            public_run = opstore.run_public(updated)
        self._emit_event("operator.approval.requested", {
            "run_id": public_run["id"], "approval_id": approval["id"],
            "action": approval["action"], "project_id": public_run["project_id"],
        })
        return opstore.approval_public(approval)

    def resolve_approval(
        self, approval_id: str, *, decision: str, resolved_by: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Resolve one approval. The run stays paused until resume_run is called."""
        with self.store.connection() as db, db:
            approval_row = db.execute(
                "SELECT * FROM operator_approvals WHERE id=? AND tenant_id=?", (approval_id, self.tenant_id),
            ).fetchone()
            if approval_row is None:
                raise opstore.OperatorNotFound("Operator approval not found.")
            resolved = opstore.resolve_approval_in_store(
                db, tenant_id=self.tenant_id, approval_id=approval_id,
                decision=decision, resolved_by=resolved_by, note=note,
            )
            if decision == "denied":
                run_row = opstore.get_run_any_tenant(db, str(approval_row["run_id"]))
                if run_row is not None and str(run_row["status"]) not in opstore.TERMINAL_RUN_STATES:
                    opstore.transition_run(
                        db, str(approval_row["run_id"]),
                        expected_revision=int(run_row["revision"]), target_status="failed",
                        error_code="approval_denied",
                        error_summary=f"Approval {approval_id} was denied.",
                    )
            else:
                db.execute(
                    "UPDATE operator_runs SET pending_approval_id='' WHERE id=? AND pending_approval_id=?",
                    (str(approval_row["run_id"]), approval_id),
                )
            public = opstore.approval_public(resolved)
        self._emit_event("operator.approval.resolved", {
            "approval_id": public["id"], "run_id": public["run_id"],
            "decision": public["status"], "project_id": public["project_id"],
        })
        return public

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM operator_approvals WHERE id=? AND tenant_id=?", (approval_id, self.tenant_id),
            ).fetchone()
            if row is None:
                raise opstore.OperatorNotFound("Operator approval not found.")
            return opstore.approval_public(row)

    def list_pending_approvals(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.connection() as db:
            if project_id:
                tenant_id = self._tenant_for_project(project_id)
                rows = db.execute(
                    """SELECT * FROM operator_approvals WHERE tenant_id=? AND project_id=?
                       AND status='pending' ORDER BY created_at DESC""",
                    (tenant_id, project_id),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM operator_approvals WHERE tenant_id=? AND status='pending' ORDER BY created_at DESC",
                    (self.tenant_id,),
                ).fetchall()
            return [opstore.approval_public(row) for row in rows]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scoped_run(self, db: Any, run_id: str) -> Any:
        row = db.execute(
            "SELECT * FROM operator_runs WHERE id=? AND tenant_id=?",
            (run_id, self.tenant_id),
        ).fetchone()
        if row is None:
            raise opstore.OperatorNotFound("Operator run not found.")
        return row

    def _tenant_for_project(self, project_id: str) -> str:
        if not project_id:
            raise opstore.OperatorValidationError("A project_id is required for operator work.")
        with self.store.connection() as db:
            row = db.execute(
                "SELECT tenant_id FROM tenant_projects WHERE project_id=?", (project_id,)
            ).fetchone()
        if row is None or str(row["tenant_id"]) != self.tenant_id:
            raise opstore.OperatorNotFound("Project not found.")
        return str(row["tenant_id"])

    @staticmethod
    def _validate_bounds(
        max_steps: Any, tool_failures: Any, model_failures: Any,
        max_total_tokens: Any = 0, max_model_decisions: Any = 0,
        max_network_attempts: Any = 0, no_progress_limit: Any = 3,
    ) -> None:
        try:
            values = (
                int(max_steps), int(tool_failures), int(model_failures),
                int(max_total_tokens), int(max_model_decisions),
                int(max_network_attempts), int(no_progress_limit),
            )
        except (TypeError, ValueError) as exc:
            raise opstore.OperatorValidationError("Operator bounds must be integers.") from exc
        checks = (
            opstore.MAX_STEPS_BOUND, opstore.MAX_TOOL_FAILURES_BOUND, opstore.MAX_MODEL_FAILURES_BOUND,
            opstore.MAX_TOTAL_TOKENS_BOUND, opstore.MAX_MODEL_DECISIONS_BOUND,
            opstore.MAX_NETWORK_ATTEMPTS_BOUND, opstore.NO_PROGRESS_LIMIT_BOUND,
        )
        for value, (low, high) in zip(values, checks):
            if not low <= value <= high:
                raise opstore.OperatorValidationError(
                    f"Operator bound {value} is outside its allowed range {low}-{high}."
                )

    @staticmethod
    def _validate_retrieval(mode: Any, canary_percent: Any) -> None:
        if str(mode or "") not in opstore.RETRIEVAL_MODES:
            raise opstore.OperatorValidationError("retrieval_mode must be lexical or hybrid_local.")
        try:
            percent = int(canary_percent)
        except (TypeError, ValueError) as exc:
            raise opstore.OperatorValidationError("retrieval_canary_percent must be an integer.") from exc
        low, high = opstore.RETRIEVAL_CANARY_PERCENT_BOUND
        if not low <= percent <= high:
            raise opstore.OperatorValidationError(
                f"retrieval_canary_percent must be between {low} and {high}."
            )

    @staticmethod
    def _validate_decision_policy(
        policy: Any, canary_percent: Any, read_confidence: Any,
        write_confidence: Any, sensitive_confidence: Any,
    ) -> None:
        if str(policy or "") not in {"cloud_only", "needle_shadow", "needle_assisted", "needle_only"}:
            raise opstore.OperatorValidationError(
                "decision_policy must be cloud_only, needle_shadow, needle_assisted, or needle_only."
            )
        try:
            percent = int(canary_percent)
        except (TypeError, ValueError) as exc:
            raise opstore.OperatorValidationError("needle_canary_percent must be an integer.") from exc
        if not 0 <= percent <= 100:
            raise opstore.OperatorValidationError("needle_canary_percent must be between 0 and 100.")
        for name, value in (
            ("needle_read_confidence", read_confidence),
            ("needle_write_confidence", write_confidence),
            ("needle_sensitive_confidence", sensitive_confidence),
        ):
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise opstore.OperatorValidationError(f"{name} must be a number between 0 and 1.") from exc
            if not 0 <= number <= 1:
                raise opstore.OperatorValidationError(f"{name} must be between 0 and 1.")

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            value = _validated(EventCreateInput, {
                "type": event_type,
                "payload": {
                    key: payload.get(key) for key in (
                        "id", "run_id", "approval_id", "action", "decision", "status",
                        "wake_reason", "project_id", "error_code",
                    ) if payload.get(key) not in (None, "")
                } | {"operator_run_id": payload.get("id")},
                "project_id": payload.get("project_id") or None,
            })
            self.runtime.events.publish(value)
        except Exception:
            log_event(
                level="warning", category="operator", source="operator.service",
                message="Unable to publish operator platform event.",
                details={"event_type": event_type, "run_id": payload.get("id")},
            )

    @staticmethod
    def _log_run(payload: dict[str, Any], message: str, *, level: str = "info") -> None:
        try:
            log_event(
                level=level, category="operator", source="operator.runtime",
                message=message,
                details={
                    "run_id": payload.get("id"), "status": payload.get("status"),
                    "wake_reason": payload.get("wake_reason"),
                    "current_step": payload.get("current_step"),
                    "error_code": payload.get("error_code"),
                    "provider": payload.get("last_provider"),
                    "model": payload.get("last_model"),
                },
                correlation_id=str(payload.get("id") or ""),
                project_id=str(payload.get("project_id")) or None,
            )
        except Exception:
            return


def ensure_operator_schema(db: sqlite3.Connection) -> None:
    init_operator_schema(db)


__all__ = ["OperatorService", "ensure_operator_schema"]
