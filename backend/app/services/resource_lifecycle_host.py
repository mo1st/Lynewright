"""Hosted-tick adapter for resource lifecycle coordinators.

This module deliberately contains no loop. The existing per-tenant
``sync_scheduler`` tick calls it when a coordinator has been registered.
"""
from __future__ import annotations

import hashlib
from threading import RLock
from typing import Any

from app.services.resource_lifecycle import ResourceLifecycleCoordinator


_coordinators: dict[str, ResourceLifecycleCoordinator] = {}
_lock = RLock()

# Reconciliation is provider-facing polling, so every tenant reconciling on
# every tick would hammer a shared provider in lockstep and burn its rate
# limit on work that rarely changes. Each tenant instead reconciles on one of
# ``_RECONCILE_SLOTS`` ticks, chosen by a stable hash of its id: the phase is
# spread deterministically (no RNG, so a tick is still reproducible in tests),
# and no two ticks carry the whole estate.
_RECONCILE_SLOTS = 6


def _reconciles_on_tick(tenant_id: str, tick: int) -> bool:
    """Return whether this tenant owns the reconciliation slot for this tick."""
    digest = hashlib.sha256(tenant_id.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:4], "big") % _RECONCILE_SLOTS
    return tick % _RECONCILE_SLOTS == offset


def register_resource_lifecycle_coordinator(tenant_id: str, coordinator: ResourceLifecycleCoordinator) -> None:
    if not tenant_id:
        raise ValueError("A lifecycle coordinator requires a tenant id.")
    with _lock:
        _coordinators[tenant_id] = coordinator


def unregister_resource_lifecycle_coordinator(tenant_id: str) -> None:
    with _lock:
        _coordinators.pop(tenant_id, None)


def get_resource_lifecycle_coordinator(tenant_id: str) -> ResourceLifecycleCoordinator | None:
    with _lock:
        return _coordinators.get(tenant_id)


def run_resource_lifecycle_tick(
    tenant_id: str, *, max_items: int = 10, tick: int | None = None,
) -> dict[str, Any]:
    """Process bounded due work for one tenant from the existing hosted tick.

    Outbox delivery runs every tick because it is the latency-sensitive half.
    Reconciliation is deliberately phase-spread across ticks (see
    ``_RECONCILE_SLOTS``) so provider polling stays tenant-fair and rate-limit
    aware rather than stampeding on every pass.
    """
    if max_items < 1:
        return {"processed": 0, "items": []}
    with _lock:
        coordinator = _coordinators.get(tenant_id)
    if coordinator is None:
        return {"processed": 0, "items": []}
    items: list[dict[str, Any]] = []
    for _ in range(max_items):
        result = coordinator.process_one(worker_id=f"hosted-resource:{tenant_id}")
        if result is None:
            break
        items.append(result)
    if tick is not None and not _reconciles_on_tick(tenant_id, tick):
        return {"processed": len(items), "items": items, "reconciled": [], "attention": {}, "reconcile_skipped": True}
    reconciled = coordinator.reconcile_bindings(max_items=max(1, max_items // 5))
    attention = coordinator.reconcile_attention(limit=max(200, max_items * 10))
    return {"processed": len(items), "items": items, "reconciled": reconciled, "attention": attention}


__all__ = [
    "register_resource_lifecycle_coordinator",
    "get_resource_lifecycle_coordinator",
    "run_resource_lifecycle_tick",
    "unregister_resource_lifecycle_coordinator",
]
