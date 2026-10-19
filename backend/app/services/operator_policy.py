"""Operator action risk classes, action registry, and the approval decision engine.

Session 4 is the single owner of risk/approval decision logic. Every automated
actor — the future internal Operator, an external MCP agent, a scheduled run —
must pass through :class:`OperatorPolicyEngine.decide` before mutating anything.
Lynewright, not the model, decides what is permitted.

The risk vocabulary is shared across all sessions and must not be paraphrased:

    READ_ONLY SAFE_OPERATION BOUNDED_CONFIG PUBLICATION
    MONEY_MOVING DESTRUCTIVE SECURITY_SENSITIVE

Default decisions (master plan):

    READ_ONLY          automatic if capability permits
    SAFE_OPERATION     automatic if explicitly allowlisted and reversible/idempotent
    BOUNDED_CONFIG     automatic only inside declared bounds and with verification
    PUBLICATION        approval by default; configurable via overrides
    MONEY_MOVING       approval required (never automatic)
    DESTRUCTIVE        approval required (never automatic)
    SECURITY_SENSITIVE approval required, never exposed through untrusted channel identity

Unknown actions are forbidden by default. Overrides can relax PUBLICATION (and
tighten anything), but never the three hard-approval classes above.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from app.services.platform_services import PlatformValidationError


RISK_CLASSES: tuple[str, ...] = (
    "READ_ONLY",
    "SAFE_OPERATION",
    "BOUNDED_CONFIG",
    "PUBLICATION",
    "MONEY_MOVING",
    "DESTRUCTIVE",
    "SECURITY_SENSITIVE",
)

ApprovalMode = Literal["automatic", "approval_required", "forbidden"]

# Risk classes whose approval requirement cannot be relaxed by configuration.
HARD_APPROVAL_CLASSES: frozenset[str] = frozenset({
    "MONEY_MOVING",
    "DESTRUCTIVE",
    "SECURITY_SENSITIVE",
})

DECISIONS: tuple[str, ...] = ("allow", "approval_required", "forbidden")

# Risk classes whose master-plan default requires a human approval before any
# execution: PUBLICATION is configurable via overrides; the hard three never
# relax. This constant is derived from the shared vocabulary so the Operator
# loop and the shared tool runtime cannot drift from the policy layer.
APPROVAL_REQUIRED_RISK_CLASSES: frozenset[str] = frozenset(
    {"PUBLICATION", *HARD_APPROVAL_CLASSES}
)

# The internal Operator identifies itself with this CapabilityActor client
# type (see operator_runtime.actor). Declared here so the shared tool runtime
# can recognize autonomous-operator calls without importing that package.
OPERATOR_ACTOR_CLIENT_TYPE = "internal-operator"

# Core tools whose semantics correspond exactly to one registry action. When a
# mapping exists the gate routes through the engine, so overrides set for the
# action govern every transport that invokes the tool. Tools without a
# mapping are governed by their risk class alone.
TOOL_ACTION_MAP: dict[str, str] = {
    "lynewright.jobs.retry": "jobs.retry_failed",
}


@dataclass(frozen=True)
class ActionDescriptor:
    """Metadata for one operator-visible action; handlers live elsewhere."""

    name: str
    risk_class: str
    required_capability: str
    reversible: bool = False
    idempotent: bool = False
    requires_verification: bool = False
    approval_policy: ApprovalMode = "approval_required"
    timeout_seconds: int = 60
    description: str = ""
    bounds: dict[str, Any] | None = None

    def with_overrides(self, **changes: Any) -> "ActionDescriptor":
        return replace(self, **changes)


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    decision: str  # allow | approval_required | forbidden
    reasons: list[str] = field(default_factory=list)
    requires_verification: bool = False
    risk_class: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "requires_verification": self.requires_verification,
            "risk_class": self.risk_class,
        }


def _validate_descriptor(descriptor: ActionDescriptor) -> None:
    if not descriptor.name or len(descriptor.name) > 120:
        raise PlatformValidationError("Action names must be 1-120 characters.")
    if descriptor.risk_class not in RISK_CLASSES:
        raise PlatformValidationError(
            f"Unknown risk class {descriptor.risk_class!r}; use the shared vocabulary."
        )
    if descriptor.approval_policy not in {"automatic", "approval_required", "forbidden"}:
        raise PlatformValidationError("approval_policy must be automatic|approval_required|forbidden.")
    if descriptor.risk_class in HARD_APPROVAL_CLASSES and descriptor.approval_policy == "automatic":
        raise PlatformValidationError(
            f"{descriptor.risk_class} actions can never be registered as automatic."
        )
    if descriptor.risk_class == "BOUNDED_CONFIG" and descriptor.bounds is None:
        raise PlatformValidationError("BOUNDED_CONFIG actions must declare bounds.")
    if not descriptor.required_capability:
        raise PlatformValidationError("Every action declares a required capability.")


def default_decision_for_risk(
    descriptor: ActionDescriptor,
    *,
    capabilities: set[str],
    proposed_value: Any = None,
) -> PolicyDecision:
    """The master plan's default mapping from risk class to decision."""
    reasons: list[str] = []
    if descriptor.risk_class == "READ_ONLY":
        if descriptor.required_capability in capabilities:
            return PolicyDecision(action=descriptor.name, decision="allow",
                                  reasons=["read_only_and_capable"], risk_class=descriptor.risk_class)
        return PolicyDecision(action=descriptor.name, decision="forbidden",
                              reasons=[f"missing_capability:{descriptor.required_capability}"],
                              risk_class=descriptor.risk_class)
    if descriptor.risk_class == "SAFE_OPERATION":
        if descriptor.approval_policy == "forbidden":
            return PolicyDecision(action=descriptor.name, decision="forbidden",
                                  reasons=["action_forbidden_by_policy"], risk_class=descriptor.risk_class)
        if descriptor.approval_policy != "automatic":
            return PolicyDecision(action=descriptor.name, decision="approval_required",
                                  reasons=["not_allowlisted_for_automatic"], risk_class=descriptor.risk_class)
        if not (descriptor.reversible or descriptor.idempotent):
            return PolicyDecision(action=descriptor.name, decision="approval_required",
                                  reasons=["safe_operation_must_be_reversible_or_idempotent"],
                                  risk_class=descriptor.risk_class)
        if descriptor.required_capability not in capabilities:
            return PolicyDecision(action=descriptor.name, decision="forbidden",
                                  reasons=[f"missing_capability:{descriptor.required_capability}"],
                                  risk_class=descriptor.risk_class)
        return PolicyDecision(action=descriptor.name, decision="allow",
                              reasons=["allowlisted_safe_operation"],
                              requires_verification=descriptor.requires_verification,
                              risk_class=descriptor.risk_class)
    if descriptor.risk_class == "BOUNDED_CONFIG":
        if descriptor.required_capability not in capabilities:
            return PolicyDecision(action=descriptor.name, decision="forbidden",
                                  reasons=[f"missing_capability:{descriptor.required_capability}"],
                                  risk_class=descriptor.risk_class)
        # Bounds themselves are enforced by the bounded-setting layer before a
        # write happens; policy here only demands verification be configured.
        if not descriptor.requires_verification and not descriptor.bounds:
            return PolicyDecision(action=descriptor.name, decision="approval_required",
                                  reasons=["bounded_config_requires_bounds_and_verification"],
                                  risk_class=descriptor.risk_class)
        return PolicyDecision(action=descriptor.name, decision="allow",
                              reasons=["inside_declared_bounds"],
                              requires_verification=True,
                              risk_class=descriptor.risk_class)
    if descriptor.risk_class == "PUBLICATION":
        if descriptor.approval_policy == "forbidden":
            return PolicyDecision(action=descriptor.name, decision="forbidden",
                                  reasons=["publication_forbidden_by_policy"], risk_class=descriptor.risk_class)
        if descriptor.approval_policy == "automatic" and descriptor.required_capability in capabilities:
            return PolicyDecision(action=descriptor.name, decision="allow",
                                  reasons=["publication_allowlisted_by_policy"],
                                  requires_verification=descriptor.requires_verification,
                                  risk_class=descriptor.risk_class)
        return PolicyDecision(action=descriptor.name, decision="approval_required",
                              reasons=["publication_requires_approval"], risk_class=descriptor.risk_class)
    # MONEY_MOVING / DESTRUCTIVE / SECURITY_SENSITIVE: hard approval, unless
    # the action itself is outright forbidden (e.g. credential reveal).
    if descriptor.approval_policy == "forbidden":
        return PolicyDecision(action=descriptor.name, decision="forbidden",
                              reasons=["action_forbidden_by_policy"], risk_class=descriptor.risk_class)
    return PolicyDecision(action=descriptor.name, decision="approval_required",
                          reasons=[f"{descriptor.risk_class.lower()}_requires_approval"],
                          risk_class=descriptor.risk_class)


class OperatorActionRegistry:
    """Registry of operator-visible action metadata (no second tool runtime)."""

    def __init__(self) -> None:
        self._actions: dict[str, ActionDescriptor] = {}

    def register(self, descriptor: ActionDescriptor) -> ActionDescriptor:
        _validate_descriptor(descriptor)
        self._actions[descriptor.name] = descriptor
        return descriptor

    def get(self, name: str) -> ActionDescriptor | None:
        return self._actions.get(str(name))

    def require(self, name: str) -> ActionDescriptor:
        descriptor = self._actions.get(str(name))
        if descriptor is None:
            raise PlatformValidationError(f"Unknown operator action: {name!r}")
        return descriptor

    def list(self, risk_class: str | None = None) -> list[ActionDescriptor]:
        values = sorted(self._actions.values(), key=lambda item: item.name)
        if risk_class is None:
            return values
        if risk_class not in RISK_CLASSES:
            raise PlatformValidationError(f"Unknown risk class: {risk_class!r}")
        return [item for item in values if item.risk_class == risk_class]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._actions


class OperatorPolicyEngine:
    """Decides allow / approval_required / forbidden for operator actions."""

    OVERRIDE_MODES: tuple[ApprovalMode, ...] = ("automatic", "approval_required", "forbidden")

    def __init__(
        self,
        registry: OperatorActionRegistry | None = None,
        *,
        store: Any = None,
        tenant_id: str | None = None,
    ) -> None:
        self.registry = registry if registry is not None else build_default_registry()
        # Optional durable overrides table; without a store, overrides live in
        # process memory only (tests, ephemeral tooling).
        self._store = store
        self._overrides: dict[str, ApprovalMode] = {}
        if store is not None:
            self._load_overrides()

    # -------------------------------------------------------------- decisions

    def decide(
        self,
        action_name: str,
        *,
        actor_capabilities: set[str] | frozenset[str],
        proposed_value: Any = None,
        context: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """The single authorization decision point for operator actions."""
        descriptor = self.registry.get(action_name)
        if descriptor is None:
            return PolicyDecision(
                action=str(action_name), decision="forbidden",
                reasons=["unknown_actions_are_denied_by_default"], risk_class="",
            )
        override = self.get_override(descriptor.name)
        effective = (
            descriptor.with_overrides(approval_policy=override)
            if override is not None else descriptor
        )
        decision = default_decision_for_risk(
            effective,
            capabilities=set(actor_capabilities),
            proposed_value=proposed_value,
        )
        if (
            context and context.get("untrusted_channel")
            and effective.risk_class == "SECURITY_SENSITIVE"
            and decision.decision != "forbidden"
        ):
            # Security-sensitive work is never exposed through untrusted
            # channel identity, even for a human approver on that channel.
            # Never weakens a forbidden verdict above.
            return PolicyDecision(
                action=effective.name, decision="approval_required",
                reasons=["security_sensitive_never_via_untrusted_channel"],
                risk_class=effective.risk_class,
            )
        return decision

    def explain(self, action_name: str) -> dict[str, Any]:
        descriptor = self.registry.require(action_name)
        return {
            "name": descriptor.name,
            "risk_class": descriptor.risk_class,
            "required_capability": descriptor.required_capability,
            "reversible": descriptor.reversible,
            "idempotent": descriptor.idempotent,
            "requires_verification": descriptor.requires_verification,
            "approval_policy": descriptor.approval_policy,
            "timeout_seconds": descriptor.timeout_seconds,
            "description": descriptor.description,
            "bounds": descriptor.bounds,
            "override": self.get_override(descriptor.name),
        }

    # -------------------------------------------------------------- overrides

    def set_override(self, action_name: str, mode: ApprovalMode, *, reason: str = "") -> PolicyDecision:
        """Persist an approval-mode override for one action.

        Hard-approval risk classes refuse relaxation to ``automatic``; the call
        returns the resulting decision instead of raising so callers can report
        it like any other outcome.
        """
        descriptor = self.registry.require(action_name)
        if mode not in self.OVERRIDE_MODES:
            raise PlatformValidationError("Override mode must be automatic|approval_required|forbidden.")
        if descriptor.risk_class in HARD_APPROVAL_CLASSES and mode == "automatic":
            return PolicyDecision(
                action=action_name, decision="approval_required",
                reasons=[f"{descriptor.risk_class.lower()}_approval_cannot_be_reconfigured"],
                risk_class=descriptor.risk_class,
            )
        self._overrides[action_name] = mode
        if self._store is not None:
            self._persist_override(action_name, mode, reason)
        # Report the resulting policy *shape* for a caller that holds the
        # action's required capability; capability checks stay per-request.
        return self.decide(action_name, actor_capabilities={descriptor.required_capability})

    def clear_override(self, action_name: str) -> None:
        self._overrides.pop(action_name, None)
        if self._store is not None:
            self._delete_override(action_name)

    def get_override(self, action_name: str) -> ApprovalMode | None:
        return self._overrides.get(action_name)

    # -------------------------------------------------------------- storage

    @staticmethod
    def ensure_schema(db: Any) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS operator_action_overrides (
                action_name TEXT PRIMARY KEY,
                approval_mode TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    def _load_overrides(self) -> None:
        try:
            with self._store.connection() as db:  # type: ignore[attr-defined]
                self.ensure_schema(db)
                rows = db.execute("SELECT action_name, approval_mode FROM operator_action_overrides").fetchall()
            self._overrides = {str(row["action_name"]): row["approval_mode"] for row in rows}
        except Exception:
            self._overrides = {}

    def _persist_override(self, action_name: str, mode: ApprovalMode, reason: str) -> None:
        try:
            with self._store.connection() as db, db:  # type: ignore[attr-defined]
                self.ensure_schema(db)
                db.execute(
                    """INSERT INTO operator_action_overrides (action_name,approval_mode,reason)
                    VALUES (?,?,?)
                    ON CONFLICT(action_name) DO UPDATE SET approval_mode=excluded.approval_mode,
                        reason=excluded.reason,updated_at=CURRENT_TIMESTAMP""",
                    (action_name, mode, str(reason)[:500]),
                )
        except Exception:
            pass

    def _delete_override(self, action_name: str) -> None:
        try:
            with self._store.connection() as db, db:  # type: ignore[attr-defined]
                self.ensure_schema(db)
                db.execute("DELETE FROM operator_action_overrides WHERE action_name=?", (action_name,))
        except Exception:
            pass


# ------------------------------------------------------------------ defaults

class ToolMutationGate:
    """The single authoritative mutation boundary for shared-tool calls.

    ``CoreToolRuntime.invoke`` consults this gate after capability
    authorization and before any handler runs, so every transport — the
    internal Operator loop and external MCP clients — shares one policy
    source instead of maintaining private risk rules.

    Decision rules, in order:

    * READ_ONLY / SAFE_OPERATION / BOUNDED_CONFIG tools are allowed once the
      capability check has passed. Bounded configuration additionally
      enforces its declared bounds inside the shared handler before any
      write, identically for every caller.
    * Approval-classed tools (PUBLICATION, MONEY_MOVING, DESTRUCTIVE,
      SECURITY_SENSITIVE) are never executed inline by an autonomous actor:
      the caller receives ``approval_required`` and must present proof of a
      resolved durable approval. Interactive authenticated management
      clients keep their existing capability-authorized, audited behavior —
      operator-run approvals do not exist on that transport, and plan §3.7
      forbids silently breaking the external MCP API.
    * Overrides set through :class:`OperatorPolicyEngine` apply to mapped
      tools for every caller; hard-approval classes refuse relaxation to
      automatic at the engine, so no override can escalate privilege past
      this gate.
    """

    def __init__(self, engine: OperatorPolicyEngine | None = None) -> None:
        self.engine = engine if engine is not None else OperatorPolicyEngine(build_default_registry())

    def decide_tool(
        self,
        *,
        tool_name: str,
        risk_class: str,
        capability: str,
        actor_capabilities: set[str] | frozenset[str],
        autonomous_actor: bool = False,
        approved: bool = False,
        context: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        if risk_class not in RISK_CLASSES:
            return PolicyDecision(
                action=str(tool_name), decision="forbidden",
                reasons=[f"unknown_risk_class:{risk_class}"], risk_class="",
            )
        if risk_class not in APPROVAL_REQUIRED_RISK_CLASSES:
            action = TOOL_ACTION_MAP.get(str(tool_name))
            if action is None:
                return PolicyDecision(
                    action=str(tool_name), decision="allow",
                    reasons=["capability_authorized"], risk_class=risk_class,
                )
            # Mapped non-approval tool: overrides stay authoritative, and a
            # resolved durable operator approval satisfies an override that
            # demands one.
            decision = self.engine.decide(
                action, actor_capabilities=actor_capabilities, context=context,
            )
            if decision.decision == "approval_required" and approved:
                return PolicyDecision(
                    action=str(tool_name), decision="allow",
                    reasons=[f"{action}:durable_approval_resolved"],
                    requires_verification=decision.requires_verification,
                    risk_class=risk_class,
                )
            if decision.decision != "allow":
                return PolicyDecision(
                    action=str(tool_name), decision=decision.decision,
                    reasons=[f"{action}:{reason}" for reason in decision.reasons],
                    requires_verification=decision.requires_verification,
                    risk_class=risk_class,
                )
            return PolicyDecision(
                action=str(tool_name), decision=decision.decision,
                reasons=[f"{action}:{reason}" for reason in decision.reasons],
                requires_verification=decision.requires_verification,
                risk_class=risk_class,
            )
        descriptor = self.engine.registry.get(TOOL_ACTION_MAP.get(str(tool_name), ""))
        if descriptor is not None:
            # A registry action exists for this tool; its explicit forbidden
            # registration wins regardless of actor or approval state.
            override = self.engine.get_override(descriptor.name)
            effective_policy = override or descriptor.approval_policy
            if effective_policy == "forbidden":
                return PolicyDecision(
                    action=str(tool_name), decision="forbidden",
                    reasons=["action_forbidden_by_policy"], risk_class=risk_class,
                )
        if not autonomous_actor:
            return PolicyDecision(
                action=str(tool_name), decision="allow",
                reasons=["interactive_capability_authorized_client"], risk_class=risk_class,
            )
        if approved:
            return PolicyDecision(
                action=str(tool_name), decision="allow",
                reasons=["durable_approval_resolved"], risk_class=risk_class,
            )
        return PolicyDecision(
            action=str(tool_name), decision="approval_required",
            reasons=[f"{risk_class.lower()}_requires_approval"],
            risk_class=risk_class,
        )


def build_default_registry() -> OperatorActionRegistry:
    """The shipped safe-action registry, annotated against existing services.

    Automatic entries are limited to operations the current codebase already
    supports safely; everything sensitive defaults to approval.
    """
    registry = OperatorActionRegistry()
    # Capabilities use the platform's public capability tokens so decisions
    # consume the same actor grant sets the shared runtime authorizes with.
    read_only = [
        ("system.inspect_health", "system:read", "Read platform health status."),
        ("system.run_diagnostics", "system:diagnostics", "Run structured system diagnostics."),
        ("system.explain_components", "system:read", "Explain components, dependencies, and safe actions."),
        ("jobs.inspect", "jobs:read", "Inspect a job's lifecycle state."),
        ("providers.view_status", "ai:read-config", "View AI provider status metadata."),
        ("credentials.view_metadata", "credentials:list", "View credential metadata, never secret values."),
    ]
    for name, capability, description in read_only:
        registry.register(ActionDescriptor(
            name=name, risk_class="READ_ONLY", required_capability=capability,
            reversible=True, idempotent=True, description=description,
        ))
    registry.register(ActionDescriptor(
        name="jobs.retry_failed", risk_class="SAFE_OPERATION", required_capability="jobs:retry",
        reversible=True, idempotent=True, requires_verification=True,
        timeout_seconds=30, approval_policy="automatic",
        description="Requeue a failed/cancelled platform job through JobService.retry.",
    ))
    registry.register(ActionDescriptor(
        name="workers.recover_stale", risk_class="SAFE_OPERATION", required_capability="system:diagnostics",
        reversible=True, idempotent=True, requires_verification=True,
        timeout_seconds=60, approval_policy="automatic",
        description="Recover stale runtime worker leases through the existing queue recovery service.",
    ))
    registry.register(ActionDescriptor(
        name="providers.refresh_health", risk_class="SAFE_OPERATION", required_capability="ai:test",
        reversible=True, idempotent=True, requires_verification=False,
        timeout_seconds=45, approval_policy="automatic",
        description="Re-run provider connectivity checks.",
    ))
    registry.register(ActionDescriptor(
        name="settings.bounded_update", risk_class="BOUNDED_CONFIG", required_capability="settings:write",
        reversible=True, idempotent=True, requires_verification=True,
        timeout_seconds=30, approval_policy="automatic",
        description="Update an explicitly registered setting inside its declared bounds.",
        bounds={"registry": "operator_settings_bounds"},
    ))
    registry.register(ActionDescriptor(
        name="builder.generate_change_set", risk_class="SAFE_OPERATION", required_capability="system:diagnostics",
        reversible=True, idempotent=False, requires_verification=False,
        timeout_seconds=600, approval_policy="approval_required",
        description="Draft a Builder Agent change set; never applied automatically at this step.",
    ))
    registry.register(ActionDescriptor(
        name="builder.apply_change_set", risk_class="DESTRUCTIVE", required_capability="admin",
        reversible=True, idempotent=False, requires_verification=True,
        timeout_seconds=900, approval_policy="approval_required",
        description="Apply a Builder change set to source files; approval always required.",
    ))
    registry.register(ActionDescriptor(
        name="builder.verify_change_set", risk_class="READ_ONLY", required_capability="system:diagnostics",
        reversible=True, idempotent=True, description="Run Builder verification checks for an applied change set.",
    ))
    registry.register(ActionDescriptor(
        name="builder.rollback_change_set", risk_class="SAFE_OPERATION", required_capability="system:diagnostics",
        reversible=True, idempotent=False, requires_verification=True,
        timeout_seconds=300, approval_policy="approval_required",
        description="Roll back an applied Builder change set via its snapshot mechanism.",
    ))
    forbidden_names = {
        "credentials.reveal_secret": "Raw credential secrets are never exposed to any agent.",
        "shell.execute": "No shell access exists for agents.",
        "filesystem.raw_write": "File writes go through the Builder Agent only.",
        "database.raw_query": "Agents use existing services, never raw database access.",
        "settings.unbounded_write": "Generic settings.write is not an operator primitive.",
    }
    for name, description in forbidden_names.items():
        registry.register(ActionDescriptor(
            name=name, risk_class="SECURITY_SENSITIVE", required_capability="admin",
            reversible=False, idempotent=False, approval_policy="forbidden",
            description=description,
        ))
    return registry


__all__ = [
    "APPROVAL_REQUIRED_RISK_CLASSES",
    "ActionDescriptor",
    "ApprovalMode",
    "DECISIONS",
    "HARD_APPROVAL_CLASSES",
    "OPERATOR_ACTOR_CLIENT_TYPE",
    "TOOL_ACTION_MAP",
    "OperatorActionRegistry",
    "OperatorPolicyEngine",
    "PolicyDecision",
    "RISK_CLASSES",
    "ToolMutationGate",
    "build_default_registry",
    "default_decision_for_risk",
]
