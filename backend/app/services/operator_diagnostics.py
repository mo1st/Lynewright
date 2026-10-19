"""Machine-readable system explanation for the future Operator (Session G).

A model should not have to infer Lynewright's architecture from raw logs. This
module exposes a small component registry:

    component_id, status, summary, dependencies, recent_failure_refs,
    safe_actions, relevant_setting_keys, health_check refs

and one entry point, :func:`explain_system`, that renders every registered
component with its current health. It deliberately does NOT model the whole
source tree — only operational components with real health surfaces.

Built-ins are wired to existing infrastructure (platform health, runtime job
queue health, AI provider status) through lazy callables so importing this
module never touches a database and failing probes degrade to ``unknown``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.services.platform_services import PlatformValidationError


COMPONENT_STATUSES: tuple[str, ...] = ("healthy", "degraded", "unhealthy", "unknown", "disabled")
MAX_DEPENDENCIES = 24


@dataclass(frozen=True)
class ComponentDescriptor:
    component_id: str
    summary: str
    dependencies: tuple[str, ...] = ()
    health_check: Callable[[], dict[str, Any]] | None = None
    safe_actions: tuple[str, ...] = ()  # operator action names from the policy registry
    relevant_setting_keys: tuple[str, ...] = ()
    remediation_notes: str = ""
    tags: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.component_id or len(self.component_id) > 96:
            raise PlatformValidationError("Component ids must be 1-96 characters.")
        if len(self.dependencies) > MAX_DEPENDENCIES:
            raise PlatformValidationError(f"Components declare at most {MAX_DEPENDENCIES} dependencies.")


def status_from_health(health: dict[str, Any]) -> str:
    """Map an existing health payload onto the shared status vocabulary."""
    if not isinstance(health, dict):
        return "unknown"
    state = str(health.get("status") or health.get("state") or "").strip().lower()
    if state in {"ok", "healthy", "online", "ready", "active"}:
        return "healthy"
    if state in {"degraded", "needs_attention", "half_open", "pending"}:
        return "degraded"
    if state in {"error", "failed", "unavailable", "offline", "open"}:
        return "unhealthy"
    return "unknown"


class ComponentRegistry:
    def __init__(self) -> None:
        self._components: dict[str, ComponentDescriptor] = {}

    def register(self, descriptor: ComponentDescriptor) -> ComponentDescriptor:
        self._components[str(descriptor.component_id)] = descriptor
        return descriptor

    def get(self, component_id: str) -> ComponentDescriptor | None:
        return self._components.get(str(component_id))

    def list(self) -> list[ComponentDescriptor]:
        return sorted(self._components.values(), key=lambda item: item.component_id)


def describe_component(descriptor: ComponentDescriptor, *, include_health: bool = True) -> dict[str, Any]:
    record: dict[str, Any] = {
        "component_id": descriptor.component_id,
        "summary": descriptor.summary,
        "dependencies": list(descriptor.dependencies),
        "safe_actions": list(descriptor.safe_actions),
        "relevant_setting_keys": list(descriptor.relevant_setting_keys),
        "remediation_notes": descriptor.remediation_notes,
        "tags": list(descriptor.tags),
        "health_check": "registered" if descriptor.health_check is not None else None,
    }
    health: dict[str, Any] | None = None
    if include_health and descriptor.health_check is not None:
        try:
            health = descriptor.health_check()
            if not isinstance(health, dict):
                health = {"status": "unknown", "detail": str(health)}
        except Exception as exc:
            health = {"status": "unknown", "error": type(exc).__name__}
    if health is not None:
        record["health"] = health
        record["status"] = status_from_health(health)
    else:
        record["status"] = "unknown"
    return record


def explain_system(registry: ComponentRegistry, *, include_health: bool = True) -> dict[str, Any]:
    """The ``lynewright.system.components`` equivalent at service level."""
    components = [describe_component(item, include_health=include_health) for item in registry.list()]
    counts: dict[str, int] = {}
    for item in components:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    overall = "healthy"
    if counts.get("unhealthy"):
        overall = "unhealthy"
    elif counts.get("degraded"):
        overall = "degraded"
    elif counts.get("unknown") and len(components) == counts.get("unknown", 0):
        overall = "unknown"
    return {
        "overall_status": overall,
        "component_count": len(components),
        "status_counts": counts,
        "components": components,
    }


# ------------------------------------------------------------------ built-ins

def default_component_registry() -> ComponentRegistry:
    """Components backed by existing Lynewright health surfaces.

    Probes are lazy so construction never performs I/O; failures degrade to
    ``unknown`` rather than raising into callers.
    """
    registry = ComponentRegistry()

    def database_health() -> dict[str, Any]:
        from app.services.platform_services import SystemService
        return SystemService().get_health()

    def job_queue_health() -> dict[str, Any]:
        from app.services.job_queue import DatabaseRuntimeJobQueue
        return DatabaseRuntimeJobQueue().health()

    def ai_provider_health() -> dict[str, Any]:
        from app.services.platform_services import AIService
        status = AIService().get_provider_status()
        configured = bool(status.get("configured"))
        return {"status": "healthy" if configured else "degraded", **status}

    def builder_agent_health() -> dict[str, Any]:
        from app.services.operator_builder_hooks import get_builder_agent_hook
        hook = get_builder_agent_hook()
        if hook is None:
            return {"status": "unknown", "detail": "not available"}
        status = hook.status()
        return {
            "status": "healthy" if status.get("enabled") else "disabled",
            "details": {key: status.get(key) for key in ("enabled", "ai_provider_configured", "blocking_reasons")},
        }

    registry.register(ComponentDescriptor(
        component_id="database",
        summary="SQLite platform storage backing jobs, events, settings, media, clients.",
        dependencies=(),
        health_check=database_health,
        safe_actions=("system.inspect_health", "system.run_diagnostics"),
        remediation_notes="Database errors are environmental; escalate rather than mutate.",
        tags=("storage", "critical"),
    ))
    registry.register(ComponentDescriptor(
        component_id="runtime_job_queue",
        summary="Durable runtime_jobs queue with leases, retries, circuit breakers.",
        dependencies=("database",),
        health_check=job_queue_health,
        safe_actions=("workers.recover_stale", "jobs.inspect", "jobs.retry_failed"),
        relevant_setting_keys=(),
        remediation_notes="Stale leases recover via workers.recover_stale; open circuits need human review.",
        tags=("workers", "reliability"),
    ))
    registry.register(ComponentDescriptor(
        component_id="ai_providers",
        summary="BYOK AI provider configuration used by task-specific AI and the Builder Agent.",
        dependencies=("credentials",),
        health_check=ai_provider_health,
        safe_actions=("providers.view_status", "providers.refresh_health"),
        remediation_notes="Provider failures usually mean missing/expired keys; never log or move secrets.",
        tags=("ai",),
    ))
    registry.register(ComponentDescriptor(
        component_id="builder_agent",
        summary="Controlled source-code change primitive: generate/apply/verify/rollback change sets.",
        dependencies=("ai_providers",),
        health_check=builder_agent_health,
        safe_actions=("builder.generate_change_set", "builder.verify_change_set",
                      "builder.apply_change_set", "builder.rollback_change_set"),
        remediation_notes="Source repair goes exclusively through Builder change sets under approval policy.",
        tags=("code",),
    ))
    return registry


__all__ = [
    "COMPONENT_STATUSES",
    "ComponentRegistry",
    "ComponentDescriptor",
    "default_component_registry",
    "describe_component",
    "explain_system",
    "status_from_health",
]
