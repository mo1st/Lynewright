from __future__ import annotations

import sqlite3
import threading
import time

from app.services.capability_context import CapabilityInvocationContext
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_worker_invocations import (
    DatabaseWorkerInvocationBridge,
    IntegrationWorkerInvocationStore,
)
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformStore


def _context(key: str = "worker-idempotency-1") -> CapabilityInvocationContext:
    return CapabilityInvocationContext.from_actor(
        CapabilityActor(
            client_id="agent-1", name="Agent", client_type="telegram",
            permission_mode="full", granted_capabilities=frozenset(),
        ),
        tenant_id="ten_default", actor_type="telegram", project_id="project-1",
        integration_id="lynewright.integration.fixture", idempotency_key=key,
        entry_point="test.worker",
    )


def test_worker_queue_claims_and_completes_one_invocation() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    integration_id = "lynewright.integration.fixture"
    integrations.register({
        "schema_version": "1.0", "id": integration_id, "name": "Fixture",
        "version": "1.0.0", "type": "service", "lynewright_version": "0.1.0",
        "sdk_version": "1.0.0", "requested_capabilities": [],
        "service": {"kind": "http", "health_check": "/health"},
        "backend": "dist/backend.mjs",
    })
    integrations.enable(integration_id)
    queue = IntegrationWorkerInvocationStore(store)
    request = queue.enqueue(
        contribution_id=f"{integration_id}:service",
        operation="run", args=("run", {"value": 3}), kwargs={},
        invocation_context=_context(),
    )
    claimed = queue.claim_next(worker_id="worker-1")
    assert claimed and claimed["id"] == request["id"] and claimed["status"] == "running"
    assert claimed["_args"] == ["run", {"value": 3}]
    queue.complete(request["id"], worker_id="worker-1", result={"value": 6})
    assert queue.get(request["id"])["result"] == {"value": 6}
    db.close()


def test_bridge_waits_for_worker_and_reuses_idempotency(monkeypatch) -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    platform_store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(platform_store)
    integration_id = "lynewright.integration.fixture"
    integrations.register({
        "schema_version": "1.0", "id": integration_id, "name": "Fixture",
        "version": "1.0.0", "type": "service", "lynewright_version": "0.1.0",
        "sdk_version": "1.0.0", "requested_capabilities": [],
        "service": {"kind": "http", "health_check": "/health"},
        "backend": "dist/backend.mjs",
    })
    integrations.enable(integration_id)
    contributions = ContributionRegistry(integrations)
    contributions.register(
        integration_id=integration_id, local_id="service", kind="service",
        metadata={"contract": "lynewright.fixture", "version": "1.0.0", "operations": ["run"], "execution_plane": "worker"},
        handler=lambda operation, payload: {"operation": operation, "value": payload["value"] * 2},
    )
    bridge = DatabaseWorkerInvocationBridge(platform_store)

    def worker() -> None:
        queue = IntegrationWorkerInvocationStore(platform_store)
        claimed = None
        for _ in range(20):
            claimed = queue.claim_next(worker_id="worker-1")
            if claimed:
                break
            time.sleep(0.01)
        assert claimed is not None
        result = contributions.invoke(
            claimed["contribution_id"], *tuple(claimed["_args"]),
            invocation_context=None, **dict(claimed["_kwargs"]),
        )
        queue.complete(claimed["id"], worker_id="worker-1", result=result)

    thread = threading.Thread(target=worker)
    thread.start()
    first = bridge.invoke(
        contributions.get(f"{integration_id}:service"), ("run", {"value": 4}), {},
        invocation_context=_context("same-key"), timeout_seconds=5,
    )
    thread.join(timeout=2)
    second = bridge.invoke(
        contributions.get(f"{integration_id}:service"), ("run", {"value": 4}), {},
        invocation_context=_context("same-key"), timeout_seconds=5,
    )
    assert first == {"operation": "run", "value": 8}
    assert second == first
    db.close()
