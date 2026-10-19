"""Lightweight active-tenant enumeration for trusted background loops.

The hosted operator tick (and any other frozen scheduler loop that needs to
work "for every tenant") must discover tenants from the existing ``tenants``
table rather than holding a registry of its own. This module is deliberately
tiny and read-only:

- only tenants whose ``status`` is ``active`` are returned;
- ordering is deterministic (by id) so ticks behave identically across
  restarts and processes;
- the result is bounded so a pathological tenants table cannot unbound a tick
  (the cap comes from ``LYNEWRIGHT_OPERATOR_MAX_TENANTS_PER_TICK``, default 200);
- a database failure never raises to the caller — the scheduler must keep
  ticking — so the default tenant is returned as a degraded fallback.

No user/session authorization happens here by design: background loops are
trusted callers, but every service they construct with these ids remains
tenant-scoped.
"""
from __future__ import annotations

from contextlib import closing

from app.database import get_connection, init_db
from app.services.application_logging import log_event
from app.services.tenant_schema import DEFAULT_TENANT_ID

# Hard ceiling independent of configuration so even a misconfigured env var
# cannot make one iteration scan an unbounded tenant table.
_ABSOLUTE_MAX_TENANTS = 1_000

_FALLBACK_WARNED_STREAK = {"count": 0}


def _configured_limit() -> int:
    try:
        from app.config import get_settings

        from app.services.operator_runtime_overrides import effective_int

        return max(1, effective_int(
            "platform.operator.max_tenants_per_tick",
            int(get_settings().operator_max_tenants_per_tick),
        ))
    except Exception:
        return 200


def list_active_tenant_ids(*, limit: int | None = None) -> list[str]:
    """Return active tenant ids, deterministically ordered and bounded.

    Never raises: if the tenants table cannot be read, the default tenant is
    returned (degraded but functional) and the failure is logged once per
    consecutive failure streak instead of spamming every tick.
    """
    bound = min(max(1, int(limit if limit is not None else _configured_limit())),
                _ABSOLUTE_MAX_TENANTS)
    try:
        init_db()
        with closing(get_connection()) as db:
            rows = db.execute(
                """SELECT id FROM tenants WHERE status='active'
                   ORDER BY id LIMIT ?""",
                (bound,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - scheduler isolation contract
        _FALLBACK_WARNED_STREAK["count"] += 1
        log_event(
            level="error" if _FALLBACK_WARNED_STREAK["count"] > 1 else "warning",
            category="operator_dispatch", source="tenant_enumerator",
            message="Active-tenant enumeration failed; falling back to the default tenant.",
            details={
                "consecutive_failures": _FALLBACK_WARNED_STREAK["count"],
                "fallback_tenant_id": DEFAULT_TENANT_ID,
            },
            exc=exc,
        )
        return [DEFAULT_TENANT_ID]
    if _FALLBACK_WARNED_STREAK["count"]:
        log_event(
            level="info", category="operator_dispatch", source="tenant_enumerator",
            message="Active-tenant enumeration recovered.",
            details={"recovered_after": _FALLBACK_WARNED_STREAK["count"]},
        )
        _FALLBACK_WARNED_STREAK["count"] = 0
    ids = [str(row["id"]) for row in rows]
    if DEFAULT_TENANT_ID not in ids:
        # The default workspace always ticks, even if a broken upgrade left it
        # missing or inactive: existing single-workspace data lives there.
        ids.append(DEFAULT_TENANT_ID)
    return sorted(ids)


__all__ = ["list_active_tenant_ids"]
