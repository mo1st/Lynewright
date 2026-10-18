"""Explicit, immutable context for capability invocations.

The integration runtime already has ``IntegrationExecutionContext`` for
timeouts and cooperative cancellation.  This module deliberately owns a
different concern: who is acting, for which tenant/resource, and why the call
can be traced.  Providers receive this context through the host boundary; they
cannot construct or mutate its security-sensitive fields.
"""
from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Iterator
from typing import Literal

from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformValidationError


ActorType = Literal[
    "human", "operator", "mcp", "telegram",
    "automation", "resource_lifecycle", "system",
]

_ACTOR_TYPES = frozenset({
    "human", "operator", "mcp", "telegram",
    "automation", "resource_lifecycle", "system",
})
_MAX_FIELD_LENGTH = 256
_current_context: contextvars.ContextVar["CapabilityInvocationContext | None"] = contextvars.ContextVar(
    "capability_invocation_context", default=None,
)


def new_trace_id() -> str:
    """Create an opaque trace identifier without embedding tenant/user data."""
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class CapabilityInvocationContext:
    """Host-created identity and scope for one capability invocation."""

    tenant_id: str
    actor_type: ActorType
    actor_id: str | None
    client_id: str | None
    project_id: str | None
    resource_type: str | None
    resource_id: str | None
    integration_id: str | None
    connection_id: str | None
    trace_id: str
    parent_span_id: str | None
    idempotency_key: str | None
    entry_point: str

    def __post_init__(self) -> None:
        if not self.tenant_id or len(self.tenant_id) > _MAX_FIELD_LENGTH:
            raise PlatformValidationError("Capability invocation requires a valid tenant.")
        if self.actor_type not in _ACTOR_TYPES:
            raise PlatformValidationError("Capability invocation actor type is invalid.")
        if not self.trace_id or len(self.trace_id) > _MAX_FIELD_LENGTH:
            raise PlatformValidationError("Capability invocation requires a valid trace id.")
        if not self.entry_point or len(self.entry_point) > _MAX_FIELD_LENGTH:
            raise PlatformValidationError("Capability invocation requires a valid entry point.")
        for label, value in (
            ("actor_id", self.actor_id), ("client_id", self.client_id),
            ("project_id", self.project_id), ("resource_type", self.resource_type),
            ("resource_id", self.resource_id), ("integration_id", self.integration_id),
            ("connection_id", self.connection_id), ("parent_span_id", self.parent_span_id),
            ("idempotency_key", self.idempotency_key),
        ):
            if value is not None and len(str(value)) > _MAX_FIELD_LENGTH:
                raise PlatformValidationError(f"Capability invocation {label} is too long.")
        if self.resource_id is not None and self.resource_type is None:
            raise PlatformValidationError("A resource id requires a resource type.")
        if self.connection_id is not None and self.integration_id is None:
            raise PlatformValidationError("A connection id requires an integration id.")

    def require_project(self) -> str:
        """The project this invocation is scoped to, or a refusal.

        `project_id` is optional on the context because tenant-global
        invocations exist. An operation on project-scoped data has no safe
        interpretation without one, so it asks rather than defaulting.
        """
        if not self.project_id:
            raise PlatformValidationError(
                "This capability invocation requires a project-scoped context."
            )
        return self.project_id

    @classmethod
    def from_actor(
        cls,
        actor: CapabilityActor | None = None,
        *,
        tenant_id: str,
        actor_type: ActorType,
        entry_point: str,
        actor_id: str | None = None,
        project_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        integration_id: str | None = None,
        connection_id: str | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> "CapabilityInvocationContext":
        """Build a context from an authenticated actor at a host boundary.

        `actor` is a registered capability client. A human arriving through the
        authenticated HTTP API is not one, so that boundary passes `actor_id`
        directly and leaves `client_id` unset -- the two fields stop being
        interchangeable, and "which registered client did this" keeps a
        truthful answer of "none".

        The caller supplies `tenant_id`, `project_id`, and `actor_id` from
        material it has already authenticated. Nothing here validates them,
        because a boundary that accepted unauthenticated values would make the
        context a laundering step rather than an authority.
        """
        if actor is not None and actor_id is not None and actor_id != actor.client_id:
            raise PlatformValidationError(
                "A capability invocation cannot name an actor and a different actor id."
            )
        return cls(
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor.client_id if actor is not None else actor_id,
            client_id=actor.client_id if actor is not None else None,
            project_id=project_id,
            resource_type=resource_type,
            resource_id=resource_id,
            integration_id=integration_id,
            connection_id=connection_id,
            trace_id=trace_id or new_trace_id(),
            parent_span_id=parent_span_id,
            idempotency_key=idempotency_key,
            entry_point=entry_point,
        )


# The architecture plan uses this name for the identity/scope context. Keep a
# discoverable alias while ``IntegrationExecutionContext`` remains the
# timeout/cancellation context in integration_execution.py.
CapabilityExecutionContext = CapabilityInvocationContext


def current_capability_context() -> CapabilityInvocationContext | None:
    """Return the current context for logging/diagnostics only."""
    return _current_context.get()


@contextmanager
def bind_capability_context(context: CapabilityInvocationContext) -> Iterator[CapabilityInvocationContext]:
    """Temporarily bind a host-created context around a callback."""
    token = _current_context.set(context)
    try:
        yield context
    finally:
        _current_context.reset(token)


__all__ = [
    "ActorType", "CapabilityExecutionContext", "CapabilityInvocationContext",
    "bind_capability_context", "current_capability_context", "new_trace_id",
]
