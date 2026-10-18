"""Operator remediation: change plans, safe execution, verification, escalation.

Implements the required flow:

    diagnose -> change plan -> policy decision -> apply if allowed -> verify
        ├─ success: record resolution
        └─ failure: rollback if safe, else escalate (and stop auto-retrying)

Everything here is callable independently of any LLM: a script, a scheduler,
or a future Operator runtime drives it through the same public methods.

Design rules enforced by code, not convention:

* Every mutation is preceded by a persisted change plan (problem, evidence,
  action, risk, old/new state, rollback, verification).
* Authorization goes exclusively through :class:`OperatorPolicyEngine.decide`.
* Attempt budgets per correlation id prevent infinite autonomous remediation.
* Builder Agent source repair is invoked only through its existing
  generate/apply/verify/rollback API — apply/verify/rollback logic is never
  duplicated here.
* All outcomes are auditable by plan/correlation id and emitted as durable
  ``operator.*`` events for the future notification layer.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from app.services.operator_diagnostics import default_component_registry, describe_component, explain_system
from app.services.operator_builder_hooks import get_builder_agent_hook
from app.services.operator_policy import ActionDescriptor, OperatorPolicyEngine, build_default_registry
from app.services.operator_settings_bounds import BOUNDED_SETTINGS, BoundedSettingRegistry
from app.services.platform_services import (
    EventService,
    PlatformNotFoundError,
    PlatformStore,
    PlatformValidationError,
    _redact,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID


PLAN_STATUSES: tuple[str, ...] = (
    "proposed",            # created, not yet authorized
    "awaiting_approval",   # policy requires a human decision
    "approved",            # human approved; ready to execute
    "rejected",            # forbidden by policy or denied by human
    "applied",             # mutation performed, verification pending
    "verified",            # applied and verification passed (resolution recorded)
    "apply_failed",        # mutation raised; rollback attempted
    "rolled_back",         # verification failed and rollback succeeded
    "escalated",           # rollback failed or budget exhausted; human needed
    "blocked_attempt_limit",
)

HandlerResult = dict[str, Any]
ActionHandler = Callable[["OperatorRemediationService", dict[str, Any]], HandlerResult]
ActionVerifier = Callable[["OperatorRemediationService", dict[str, Any]], HandlerResult]
ActionRollback = Callable[["OperatorRemediationService", dict[str, Any], dict[str, Any]], HandlerResult]

_UNSET = object()


class ChangePlanService:
    """Durable, tenant-scoped remediation/change plans."""

    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS operator_change_plans (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT,
                correlation_id TEXT NOT NULL DEFAULT '',
                problem TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                action_name TEXT NOT NULL,
                risk_class TEXT NOT NULL DEFAULT '',
                old_state_json TEXT,
                new_state_json TEXT,
                expected_effect TEXT NOT NULL DEFAULT '',
                rollback_strategy TEXT NOT NULL DEFAULT '',
                verification_strategy TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'proposed',
                policy_decision_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                builder_change_set_id INTEGER,
                decided_by TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_operator_change_plans_scope
                ON operator_change_plans(tenant_id, project_id, status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_operator_change_plans_correlation
                ON operator_change_plans(tenant_id, correlation_id, created_at DESC);
            """
        )

    def create(
        self,
        *,
        problem: str,
        action_name: str,
        project_id: str | None = None,
        correlation_id: str = "",
        evidence: dict[str, Any] | None = None,
        expected_effect: str = "",
        rollback_strategy: str = "",
        verification_strategy: str = "",
        risk_class: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = str(problem or "").strip()
        if not text or len(text) > 2_000:
            raise PlatformValidationError("Change plans require a problem statement up to 2,000 characters.")
        action = str(action_name or "").strip()
        if not action or len(action) > 120:
            raise PlatformValidationError("Change plans require an action name.")
        encoded_evidence = _safe_json(_redact(evidence or {}))
        encoded_payload = _safe_json(payload or {})
        plan_id = f"ocp_{secrets.token_hex(12)}"
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO operator_change_plans
                (id,tenant_id,project_id,correlation_id,problem,evidence_json,action_name,risk_class,
                 expected_effect,rollback_strategy,verification_strategy,payload_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id, self.tenant_id, project_id, str(correlation_id)[:160], text,
                    encoded_evidence, action, str(risk_class)[:40],
                    str(expected_effect or "")[:1_000], str(rollback_strategy or "")[:1_000],
                    str(verification_strategy or "")[:1_000], encoded_payload,
                ),
            )
        return self.get(plan_id)

    def get(self, plan_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM operator_change_plans WHERE id=? AND tenant_id=?",
                (plan_id, self.tenant_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Change plan not found.")
        return self._public(row)

    def list(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        correlation_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in PLAN_STATUSES:
            raise PlatformValidationError(f"Unknown plan status: {status!r}")
        query = "SELECT * FROM operator_change_plans WHERE tenant_id=?"
        params: list[Any] = [self.tenant_id]
        if project_id is not None:
            query += " AND project_id=?"
            params.append(project_id)
        if status is not None:
            query += " AND status=?"
            params.append(status)
        if correlation_id is not None:
            query += " AND correlation_id=?"
            params.append(correlation_id)
        limit = max(1, min(int(limit), 200))
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        return [self._public(row) for row in rows]

    def count_executed_for_correlation(self, correlation_id: str) -> int:
        """How many plans for one incident already consumed an application attempt."""
        with self._connection() as db:
            row = db.execute(
                """SELECT COUNT(*) AS total FROM operator_change_plans
                WHERE tenant_id=? AND correlation_id=? AND attempts>0""",
                (self.tenant_id, correlation_id),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for column, target in (
            ("evidence_json", "evidence"), ("policy_decision_json", "policy_decision"),
            ("payload_json", "payload"), ("result_json", "result"),
        ):
            raw = value.pop(column, "{}") or "{}"
            try:
                value[target] = json.loads(raw)
            except (TypeError, ValueError):
                value[target] = {}
        old_state, new_state = value.pop("old_state_json", None), value.pop("new_state_json", None)
        value["old_state"] = _loads_optional(old_state)
        value["new_state"] = _loads_optional(new_state)
        return value


class OperatorRemediationService:
    """Executes change plans under policy: apply → verify → rollback/escalate."""

    def __init__(
        self,
        store: PlatformStore | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        policy_engine: OperatorPolicyEngine | None = None,
        plans: ChangePlanService | None = None,
        bounded_settings: BoundedSettingRegistry | None = None,
        component_registry: Any | None = None,
        events: EventService | None = None,
        max_attempts_per_incident: int = 3,
        job_service_factory: Callable[[], Any] | None = None,
        settings_service_factory: Callable[[], Any] | None = None,
        ai_service_factory: Callable[[], Any] | None = None,
        runtime_queue_factory: Callable[[], Any] | None = None,
        objective_request_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id
        self.policy = policy_engine or OperatorPolicyEngine(build_default_registry())
        self.plans = plans or ChangePlanService(self.store, tenant_id=self.tenant_id)
        self.bounded_settings = bounded_settings if bounded_settings is not None else BOUNDED_SETTINGS
        self.registry = component_registry if component_registry is not None else default_component_registry()
        self.events = events or EventService(self.store, tenant_id=self.tenant_id)
        self.max_attempts_per_incident = max(1, int(max_attempts_per_incident))
        self._job_service_factory = job_service_factory
        self._settings_service_factory = settings_service_factory
        self._ai_service_factory = ai_service_factory
        self._runtime_queue_factory = runtime_queue_factory
        self._handlers: dict[str, tuple[ActionHandler, ActionVerifier | None, ActionRollback | None]] = {}
        self._register_default_handlers()

    # ------------------------------------------------------------ diagnostics

    def inspect_health(self) -> dict[str, Any]:
        return self._system().get_health()

    def run_diagnostics(self) -> dict[str, Any]:
        return self._system().get_diagnostics()

    def explain_components(self, *, include_health: bool = True) -> dict[str, Any]:
        return explain_system(self.registry, include_health=include_health)

    def get_component(self, component_id: str, *, include_health: bool = True) -> dict[str, Any]:
        descriptor = self.registry.get(component_id)
        if descriptor is None:
            raise PlatformNotFoundError(f"Unknown system component: {component_id!r}")
        return describe_component(descriptor, include_health=include_health)

    def inspect_job(self, job_id: str) -> dict[str, Any]:
        return self._jobs().get(job_id)

    # ------------------------------------------------------------- plan flow

    def propose_plan(self, **kwargs: Any) -> dict[str, Any]:
        plan = self.plans.create(**kwargs)
        self._emit("operator.change_plan_created", plan)
        return plan

    def execute_plan(self, plan_id: str, *, actor_capabilities: set[str] | frozenset[str]) -> dict[str, Any]:
        """Authorize and (if allowed) apply → verify → rollback/escalate."""
        plan = self.get_plan(plan_id)
        if plan["status"] not in {"proposed", "approved"}:
            raise PlatformValidationError(f"Plan is {plan['status']}; only proposed/approved plans can execute.")
        decision = self.policy.decide(
            plan["action_name"],
            actor_capabilities=set(actor_capabilities),
            proposed_value=(plan.get("new_state") or {}).get("value") if isinstance(plan.get("new_state"), dict) else None,
        )
        plan = self._update_plan(plan_id, status="rejected" if decision.decision == "forbidden" else plan["status"],
                                 policy_decision=decision.as_dict())
        if decision.decision == "forbidden":
            plan["result"] = {"outcome": "rejected", "reasons": decision.reasons}
            self._persist_result(plan_id, plan["result"])
            self._emit("operator.remediation_rejected", plan)
            return self.get_plan(plan_id)
        if decision.decision == "approval_required":
            plan = self._update_plan(plan_id, status="awaiting_approval")
            self._emit("operator.approval_required", {
                "plan_id": plan_id, "action": plan["action_name"], "risk_class": plan["risk_class"],
                "reasons": decision.reasons, "project_id": plan.get("project_id"),
                "correlation_id": plan.get("correlation_id"),
            })
            return self.get_plan(plan_id)
        return self._apply_verified_path(plan_id, actor_capabilities=set(actor_capabilities))

    def resolve_approval(self, plan_id: str, *, approve: bool, decided_by: str, note: str = "") -> dict[str, Any]:
        """Human decision hook (notification/approval layers call this)."""
        plan = self.get_plan(plan_id)
        if plan["status"] != "awaiting_approval":
            raise PlatformValidationError(f"Plan is {plan['status']}; no approval is pending.")
        who = str(decided_by or "")[:160]
        if not approve:
            plan = self._update_plan(plan_id, status="rejected",
                                     policy_decision={**plan.get("policy_decision", {}), "denied_by": who, "note": str(note)[:500]})
            self._emit("operator.remediation_rejected", plan)
            return plan
        plan = self._update_plan(plan_id, status="approved", decided_by=who)
        self._emit("operator.remediation_approved", plan)
        return plan

    def _apply_verified_path(self, plan_id: str, *, actor_capabilities: set[str]) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        correlation = str(plan.get("correlation_id") or "")
        executed = self.plans.count_executed_for_correlation(correlation) if correlation else 0
        if executed >= self.max_attempts_per_incident:
            plan = self._update_plan(plan_id, status="blocked_attempt_limit")
            self._emit("operator.remediation_escalated", {**plan, "reason": "attempt_limit"})
            return plan
        entry = self._handlers.get(plan["action_name"])
        if entry is None:
            plan = self._update_plan(plan_id, status="rejected")
            plan["result"] = {"outcome": "rejected", "reasons": ["no_registered_handler"]}
            self._persist_result(plan_id, plan["result"])
            return plan
        handler, verifier, rollback = entry
        try:
            applied = handler(self, plan)
        except Exception as exc:
            plan = self._update_plan(plan_id, status="apply_failed", bump_attempts=True)
            plan["result"] = {"outcome": "apply_failed", "error": type(exc).__name__, "message": str(exc)[:500]}
            self._persist_result(plan_id, plan["result"])
            self._emit("operator.remediation_apply_failed", plan)
            return self._after_failure(plan_id, plan, None, actor_capabilities=actor_capabilities)
        old_state = applied.get("old_state")
        plan = self._update_plan(plan_id, status="applied", old_state=old_state,
                                 new_state=applied.get("new_state"), bump_attempts=True)
        if not applied.get("ok", True):
            plan["result"] = {"outcome": "apply_failed", "details": _redact(applied.get("details", {}))}
            self._persist_result(plan_id, plan["result"])
            self._persist_states(plan_id, old_state=old_state, new_state=applied.get("new_state"))
            self._emit("operator.remediation_apply_failed", plan)
            return self._after_failure(plan_id, self.get_plan(plan_id), applied, actor_capabilities=actor_capabilities)
        self._emit("operator.remediation_applied", plan)
        if verifier is not None:
            try:
                verification = verifier(self, self.get_plan(plan_id))
            except Exception as exc:
                verification = {"verified": False, "error": type(exc).__name__, "message": str(exc)[:300]}
        else:
            verification = {"verified": True, "note": "no_verifier_registered"}
        verification = {**verification, "requires_verification": True}
        if verification.get("verified"):
            plan = self._update_plan(plan_id, status="verified")
            result = {"outcome": "verified", "verification": _redact(verification)}
            self._persist_result(plan_id, result)
            plan = self.get_plan(plan_id)
            plan["result"] = result
            self._emit("operator.remediation_verified", plan)
            return plan
        # Verification failed: roll back if we captured state and can.
        result = {"outcome": "verification_failed", "verification": _redact(verification)}
        self._persist_result(plan_id, result)
        return self._after_failure(plan_id, self.get_plan(plan_id), applied,
                                   verification=result, actor_capabilities=actor_capabilities)

    def _after_failure(
        self,
        plan_id: str,
        plan: dict[str, Any],
        applied: dict[str, Any] | None,
        *,
        verification: dict[str, Any] | None = None,
        actor_capabilities: set[str] | None = None,
    ) -> dict[str, Any]:
        old_state = plan.get("old_state")
        entry = self._handlers.get(plan["action_name"])
        rollback = entry[2] if entry else None
        if old_state is None or rollback is None:
            plan = self._update_plan(plan_id, status="escalated")
            result = {**plan.get("result", {}), "outcome": "escalated",
                      "escalation_reason": "not_reversible_or_no_old_state"}
            self._persist_result(plan_id, result)
            plan = self.get_plan(plan_id)
            plan["result"] = result
            self._emit("operator.remediation_escalated", plan)
            return plan
        try:
            rolled = rollback(self, plan, old_state)
        except Exception as exc:
            rolled = {"rolled_back": False, "error": type(exc).__name__, "message": str(exc)[:300]}
        if rolled.get("rolled_back"):
            plan = self._update_plan(plan_id, status="rolled_back")
            result = {**plan.get("result", {}),
                      "outcome": "rolled_back",
                      "rollback_details": _redact({k: v for k, v in rolled.items() if k != "rolled_back"}),
                      **({"verification": verification} if verification else {})}
            self._persist_result(plan_id, result)
            plan = self.get_plan(plan_id)
            plan["result"] = result
            self._emit("operator.remediation_rolled_back", plan)
            return plan
        plan = self._update_plan(plan_id, status="escalated")
        result = {**plan.get("result", {}), "outcome": "escalated",
                  "escalation_reason": "rollback_failed",
                  "rollback_details": _redact({k: v for k, v in rolled.items() if k != "rolled_back"})}
        self._persist_result(plan_id, result)
        plan = self.get_plan(plan_id)
        plan["result"] = result
        self._emit("operator.remediation_escalated", plan)
        return plan

    # ------------------------------------------------------- convenience API

    def remediate(
        self,
        *,
        problem: str,
        action_name: str,
        actor_capabilities: set[str] | frozenset[str],
        evidence: dict[str, Any] | None = None,
        expected_effect: str = "",
        rollback_strategy: str = "",
        verification_strategy: str = "",
        project_id: str | None = None,
        correlation_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """diagnose→plan→decide→apply→verify in one call; returns the final plan."""
        # The plan is the audit record, so it always carries the action's
        # shared-vocabulary risk class, even when the caller omits it.
        descriptor = self.policy.registry.get(action_name)
        resolved_risk = descriptor.risk_class if descriptor is not None else ""
        plan = self.propose_plan(
            problem=problem, action_name=action_name, evidence=evidence,
            expected_effect=expected_effect, rollback_strategy=rollback_strategy,
            verification_strategy=verification_strategy,
            project_id=project_id, correlation_id=correlation_id,
            payload=payload, risk_class=resolved_risk,
        )
        return self.execute_plan(plan["id"], actor_capabilities=actor_capabilities)

    # ------------------------------------------------------------- accessors

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self.plans.get(plan_id)

    def list_plans(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.plans.list(**kwargs)

    def register_handler(
        self,
        action_name: str,
        handler: ActionHandler,
        verifier: ActionVerifier | None = None,
        rollback: ActionRollback | None = None,
    ) -> None:
        self._handlers[action_name] = (handler, verifier, rollback)

    # ------------------------------------------------------ default handlers

    def _register_default_handlers(self) -> None:
        # Registered as plain class-level functions: the execution framework
        # invokes handlers as ``handler(service, plan)`` / ``verifier(service,
        # plan)`` / ``rollback(service, plan, old_state)``. Registering bound
        # methods here would double-pass ``self`` and crash every built-in
        # action on first use.
        svc = OperatorRemediationService
        self.register_handler("jobs.retry_failed", svc._h_retry_job, svc._v_retry_job)
        self.register_handler("workers.recover_stale", svc._h_recover_workers, svc._v_recover_workers)
        self.register_handler("providers.refresh_health", svc._h_refresh_provider, svc._v_refresh_provider)
        self.register_handler(
            "settings.bounded_update", svc._h_bounded_setting, svc._v_bounded_setting, svc._rb_bounded_setting,
        )
        self.register_handler("builder.generate_change_set", svc._h_builder_generate)
        self.register_handler("builder.apply_change_set", svc._h_builder_apply, svc._v_builder_verify, svc._rb_builder_rollback)
        self.register_handler("builder.verify_change_set", svc._h_builder_verify_only, svc._v_builder_verify)
        self.register_handler("builder.rollback_change_set", svc._h_builder_rollback_direct, svc._v_builder_rolled_back)

    # -- jobs -----------------------------------------------------------------

    def _h_retry_job(self, plan: dict[str, Any]) -> HandlerResult:
        job_id = str((plan.get("payload") or {}).get("job_id") or "")
        if not job_id:
            return {"ok": False, "details": {"reason": "missing_job_id"}}
        jobs = self._jobs()
        before = jobs.get(job_id)
        if before["status"] in {"queued", "running"}:
            return {"ok": True, "old_state": {"job_id": job_id, "status": before["status"]},
                    "new_state": {"job_id": job_id, "status": before["status"]},
                    "details": {"note": "already_active_retry_skipped"}}
        if before["status"] not in {"failed", "cancelled"}:
            return {"ok": False, "details": {"reason": f"job_status_{before['status']}_not_retryable"}}
        retried = jobs.retry(job_id)
        return {"ok": True, "old_state": {"job_id": job_id, "status": before["status"]},
                "new_state": {"job_id": job_id, "status": retried["status"]},
                "details": {"attempts": retried.get("attempts")}}

    def _v_retry_job(self, plan: dict[str, Any]) -> HandlerResult:
        job_id = str(((plan.get("new_state") or {}).get("job_id")) or (plan.get("payload") or {}).get("job_id") or "")
        job = self._jobs().get(job_id)
        return {"verified": job["status"] in {"queued", "running"}, "status": job["status"]}

    # -- workers ---------------------------------------------------------------

    def _h_recover_workers(self, plan: dict[str, Any]) -> HandlerResult:
        queue = self._queue()
        recovered = queue.recover()
        return {"ok": True, "old_state": None, "new_state": {"recover_report": _compact(recovered)},
                "details": {"recovered": _compact(recovered)}}

    def _v_recover_workers(self, plan: dict[str, Any]) -> HandlerResult:
        health = self._queue().health()
        return {"verified": isinstance(health, dict), "health_summary": _compact(health)}

    # -- providers --------------------------------------------------------------

    def _h_refresh_provider(self, plan: dict[str, Any]) -> HandlerResult:
        provider_id = (plan.get("payload") or {}).get("provider_id")
        result = self._ai().test_provider(provider_id)
        return {"ok": True, "old_state": None, "new_state": _redact(result), "details": _redact(result)}

    def _v_refresh_provider(self, plan: dict[str, Any]) -> HandlerResult:
        result = plan.get("new_state") or {}
        return {"verified": isinstance(result, dict) and bool(result), "provider_result_keys": sorted(result.keys())}

    # -- bounded settings ---------------------------------------------------------

    def _setting_target(self, plan: dict[str, Any]) -> tuple[str, Any]:
        payload = plan.get("payload") or {}
        key = str(payload.get("key") or "")
        has_new = "value" in payload
        return key, payload.get("value") if has_new else None

    def _h_bounded_setting(self, plan: dict[str, Any]) -> HandlerResult:
        key, new_value = self._setting_target(plan)
        if not key:
            return {"ok": False, "details": {"reason": "missing_setting_key"}}
        bounds_ok, reason = self.bounded_settings.validate_value(key, new_value)
        if not bounds_ok:
            return {"ok": False, "details": {"reason": reason}}
        normalized = self.bounded_settings.normalize(key, new_value)
        settings = self._settings()
        try:
            current = settings.get(key)
            old_value = current.get("value")
        except PlatformNotFoundError:
            old_value = None
        settings.set({"key": key, "value": normalized})
        return {"ok": True, "old_state": {"key": key, "value": old_value},
                "new_state": {"key": key, "value": normalized}}

    def _v_bounded_setting(self, plan: dict[str, Any]) -> HandlerResult:
        key, new_value = self._setting_target(plan)
        expected = ((plan.get("new_state") or {}).get("value"))
        actual = self._settings().get(key)
        return {"verified": actual.get("value") == expected, "actual": _redact(actual.get("value")),
                "expected": _redact(expected)}

    def _rb_bounded_setting(self, plan: dict[str, Any], old_state: dict[str, Any]) -> HandlerResult:
        key = str(old_state.get("key") or "")
        old_value = old_state.get("value")
        settings = self._settings()
        if old_value is None:
            return {"rolled_back": False, "reason": "no_previous_value_to_restore"}
        settings.set({"key": key, "value": old_value})
        return {"rolled_back": True, "restored_value": _redact(old_value)}

    # -- Builder Agent (existing controlled interface; logic never duplicated) ----

    def _builder_payload(self, plan: dict[str, Any]) -> dict[str, Any]:
        payload = plan.get("payload") or {}
        return {
            "project_id": payload.get("project_id") or plan.get("project_id") or "",
            "user_request": str(payload.get("user_request") or ""),
            "change_set_id": payload.get("change_set_id"),
            "instruction": payload.get("instruction"),
        }

    def _h_builder_generate(self, plan: dict[str, Any]) -> HandlerResult:
        builder_agent = get_builder_agent_hook()
        if builder_agent is None:
            return {"ok": False, "details": {"reason": "not_available"}}
        target = self._builder_payload(plan)
        if not target["project_id"] or not target["user_request"]:
            return {"ok": False, "details": {"reason": "missing_project_or_request"}}
        change_set = builder_agent.generate_change_set(
            project_id=target["project_id"], user_request=target["user_request"]
        )
        change_set_id = change_set.get("id")
        self._link_builder_change_set(plan["id"], change_set_id)
        return {"ok": True, "old_state": None,
                "new_state": {"change_set_id": change_set_id, "status": change_set.get("status")},
                "details": {"change_set_id": change_set_id, "provider_used": change_set.get("provider_used")}}

    def _h_builder_apply(self, plan: dict[str, Any]) -> HandlerResult:
        builder_agent = get_builder_agent_hook()
        if builder_agent is None:
            return {"ok": False, "details": {"reason": "not_available"}}
        change_set_id = self._require_builder_change_set(plan)
        if change_set_id is None:
            return {"ok": False, "details": {"reason": "missing_builder_change_set_id"}}
        before = builder_agent.get_change_set(int(change_set_id))
        result = builder_agent.apply_change_set(int(change_set_id))
        return {"ok": True, "old_state": {"change_set_id": change_set_id, "status": (before or {}).get("status")},
                "new_state": {"change_set_id": change_set_id, "status": result.get("status")},
                "details": {"applied_files": len(result.get("diffs", []) or [])}}

    def _h_builder_verify_only(self, plan: dict[str, Any]) -> HandlerResult:
        builder_agent = get_builder_agent_hook()
        if builder_agent is None:
            return {"ok": False, "details": {"reason": "not_available"}}
        change_set_id = self._require_builder_change_set(plan)
        if change_set_id is None:
            return {"ok": False, "details": {"reason": "missing_builder_change_set_id"}}
        result = builder_agent.verify_change_set(int(change_set_id))
        return {"ok": True, "old_state": None, "new_state": {"change_set_id": change_set_id, "status": result.get("status")},
                "details": {"checks": result.get("checks")}}

    def _h_builder_rollback_direct(self, plan: dict[str, Any]) -> HandlerResult:
        builder_agent = get_builder_agent_hook()
        if builder_agent is None:
            return {"ok": False, "details": {"reason": "not_available"}}
        change_set_id = self._require_builder_change_set(plan)
        if change_set_id is None:
            return {"ok": False, "details": {"reason": "missing_builder_change_set_id"}}
        result = builder_agent.rollback_change_set(int(change_set_id))
        return {"ok": True, "old_state": None, "new_state": {"change_set_id": change_set_id, "status": result.get("status")}}

    def _v_builder_verify(self, plan: dict[str, Any]) -> HandlerResult:
        builder_agent = get_builder_agent_hook()
        if builder_agent is None:
            return {"verified": False, "reason": "not_available"}
        change_set_id = self._require_builder_change_set(plan)
        if change_set_id is None:
            return {"verified": False, "reason": "missing_builder_change_set_id"}
        record = builder_agent.get_change_set(int(change_set_id)) or {}
        return {"verified": record.get("status") == "verified", "builder_status": record.get("status")}

    def _v_builder_rolled_back(self, plan: dict[str, Any]) -> HandlerResult:
        builder_agent = get_builder_agent_hook()
        if builder_agent is None:
            return {"verified": False, "reason": "not_available"}
        change_set_id = self._require_builder_change_set(plan)
        if change_set_id is None:
            return {"verified": False, "reason": "missing_builder_change_set_id"}
        record = builder_agent.get_change_set(int(change_set_id)) or {}
        return {"verified": record.get("status") == "rolled_back", "builder_status": record.get("status")}

    def _rb_builder_rollback(self, plan: dict[str, Any], old_state: dict[str, Any]) -> HandlerResult:
        builder_agent = get_builder_agent_hook()
        if builder_agent is None:
            return {"rolled_back": False, "reason": "not_available"}
        change_set_id = old_state.get("change_set_id") or self._require_builder_change_set(plan)
        if change_set_id is None:
            return {"rolled_back": False, "reason": "missing_builder_change_set_id"}
        try:
            result = builder_agent.rollback_change_set(int(change_set_id))
            return {"rolled_back": result.get("status") == "rolled_back", "builder_status": result.get("status")}
        except Exception as exc:
            return {"rolled_back": False, "error": type(exc).__name__}

    def _require_builder_change_set(self, plan: dict[str, Any]) -> int | None:
        candidates = (
            (plan.get("payload") or {}).get("change_set_id"),
            plan.get("builder_change_set_id"),
            ((plan.get("new_state") or {}) if isinstance(plan.get("new_state"), dict) else {}).get("change_set_id"),
        )
        for candidate in candidates:
            if candidate is not None:
                return int(candidate)
        return None

    def _link_builder_change_set(self, plan_id: str, change_set_id: Any) -> None:
        if change_set_id is None:
            return
        with self.plans._connection() as db, db:
            db.execute(
                "UPDATE operator_change_plans SET builder_change_set_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                (int(change_set_id), plan_id, self.tenant_id),
            )

    # ---------------------------------------------------------------- internals

    def _update_plan(
        self,
        plan_id: str,
        *,
        status: str | None = None,
        policy_decision: dict[str, Any] | None = None,
        old_state: Any = _UNSET,
        new_state: Any = _UNSET,
        decided_by: str | None = None,
        bump_attempts: bool = False,
    ) -> dict[str, Any]:
        sets: list[str] = ["updated_at=CURRENT_TIMESTAMP"]
        params: list[Any] = []
        if status is not None:
            sets.append("status=?"); params.append(status)
        if policy_decision is not None:
            sets.append("policy_decision_json=?"); params.append(json.dumps(policy_decision))
        if old_state is not _UNSET:
            sets.append("old_state_json=?"); params.append(None if old_state is None else json.dumps(_redact(old_state), default=str))
        if new_state is not _UNSET:
            sets.append("new_state_json=?"); params.append(None if new_state is None else json.dumps(_redact(new_state), default=str))
        if decided_by is not None:
            sets.append("decided_by=?"); params.append(decided_by)
        if bump_attempts:
            sets.append("attempts=attempts+1")
        params.extend([plan_id, self.tenant_id])
        with self.plans._connection() as db, db:
            db.execute(
                f"""UPDATE operator_change_plans SET {", ".join(sets)}
                WHERE id=? AND tenant_id=?""",
                params,
            )
        return self.plans.get(plan_id)

    def _persist_result(self, plan_id: str, result: dict[str, Any]) -> None:
        with self.plans._connection() as db, db:
            db.execute(
                "UPDATE operator_change_plans SET result_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                (_safe_json(_redact(result)), plan_id, self.tenant_id),
            )

    def _persist_states(self, plan_id: str, *, old_state: Any, new_state: Any) -> None:
        with self.plans._connection() as db, db:
            db.execute(
                """UPDATE operator_change_plans SET old_state_json=?,new_state_json=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=?""",
                (
                    None if old_state is None else json.dumps(_redact(old_state), default=str),
                    None if new_state is None else json.dumps(_redact(new_state), default=str),
                    plan_id, self.tenant_id,
                ),
            )

    def _emit(self, event_type: str, payload: Any) -> None:
        body = payload if isinstance(payload, dict) else {"detail": str(payload)}
        try:
            self.events.publish({
                "type": event_type,
                "payload": {
                    "plan_id": body.get("id") or body.get("plan_id"),
                    "action": body.get("action_name") or body.get("action"),
                    "risk_class": body.get("risk_class"),
                    "status": body.get("status"),
                    "correlation_id": body.get("correlation_id"),
                    "project_id": body.get("project_id"),
                    **{key: value for key, value in body.items() if key.startswith("escalation_reason") or key == "reason"},
                },
            })
        except Exception:
            pass

    def _system(self) -> Any:
        from app.services.platform_services import SystemService
        return SystemService()

    def _jobs(self) -> Any:
        if self._job_service_factory is not None:
            return self._job_service_factory()
        from app.services.platform_services import JobService
        return JobService(self.store, tenant_id=self.tenant_id, event_bus=None)

    def _settings(self) -> Any:
        if self._settings_service_factory is not None:
            return self._settings_service_factory()
        from app.services.platform_services import SettingsService
        return SettingsService(self.store)

    def _ai(self) -> Any:
        if self._ai_service_factory is not None:
            return self._ai_service_factory()
        from app.services.platform_services import AIService
        return AIService()

    def _queue(self) -> Any:
        if self._runtime_queue_factory is not None:
            return self._runtime_queue_factory()
        from app.services.job_queue import DatabaseRuntimeJobQueue
        return DatabaseRuntimeJobQueue()


def _compact(value: Any, *, limit: int = 400) -> Any:
    try:
        encoded = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)[:limit]
    return encoded[:limit]


def _safe_json(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        encoded = "{}"
    return encoded[:65_536]


def _loads_optional(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else {"value": value}


__all__ = [
    "ChangePlanService",
    "OperatorRemediationService",
    "PLAN_STATUSES",
]
