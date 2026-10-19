"""The explicit operator CapabilityActor.

Internal Operator access must never imply raw internal/full platform access,
so the actor is derived only from the profile's explicitly granted public
capabilities. ``CapabilityActor.platform_manager()`` is deliberately not used:
that identity exists for internal capability administration and would
over-privilege the Operator.
"""
from __future__ import annotations

from app.services.platform_capabilities import (
    FULL_ACCESS_CAPABILITIES,
    PUBLIC_CAPABILITIES,
    CapabilityActor,
    ClientAuthorizationService,
)
from app.services.operator_runtime.store import OperatorValidationError

OPERATOR_CLIENT_TYPE = "internal-operator"
OPERATOR_CLIENT_ID_PREFIX = "internal-operator"

# The workspace owner explicitly opted this Operator into the complete public
# capability surface.  Risky integration actions still pause for a durable
# approval; this grant only makes those actions discoverable and eligible for
# the approval flow. Reserved internal capabilities remain impossible to grant.
DEFAULT_OPERATOR_CAPABILITIES = FULL_ACCESS_CAPABILITIES


def validate_operator_capabilities(values: list[str] | set[str] | frozenset[str]) -> frozenset[str]:
    """Operator profiles may only grant known public capabilities."""
    candidate = frozenset(str(item).strip() for item in values if str(item).strip())
    unknown = candidate - PUBLIC_CAPABILITIES
    if unknown:
        raise OperatorValidationError(
            "Only known public capabilities may be granted to an operator profile: "
            + ", ".join(sorted(unknown))
        )
    return candidate


def build_operator_actor(profile_row: Any) -> CapabilityActor:
    """Build the conservative internal actor for one profile.

    ``internal=True`` means the shared runtime skips the external per-client
    rate limiter (the run loop enforces its own bounds); it does NOT widen
    capabilities — the granted set stays exactly what the profile declares.
    """
    from app.services.operator_runtime.store import profile_public

    public = profile_public(profile_row)
    granted = validate_operator_capabilities(public["granted_capabilities"])
    return CapabilityActor(
        client_id=f"{OPERATOR_CLIENT_ID_PREFIX}:{public['id']}",
        name=f"Operator: {public['name']}",
        client_type=OPERATOR_CLIENT_TYPE,
        permission_mode="custom",
        granted_capabilities=granted,
        enabled=bool(public["enabled"]),
        internal=True,
    )


__all__ = [
    "DEFAULT_OPERATOR_CAPABILITIES",
    "OPERATOR_CLIENT_ID_PREFIX",
    "OPERATOR_CLIENT_TYPE",
    "build_operator_actor",
    "validate_operator_capabilities",
    # Re-exported for callers that want the same grant-normalization rules
    # the external client system uses.
    "ClientAuthorizationService",
]
