from __future__ import annotations

import sqlite3

from app.services.platform_services import PlatformStore
from app.services.resource_bindings import ResourceBindingService
from app.services.resource_outbox import ResourceOutboxService
from app.services.resource_reconciliation import ResourceReconciliationService


def test_reconciliation_projects_drift_and_dead_letter_attention(tmp_path) -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    bindings = ResourceBindingService(store, tenant_id="tenant-a")
    outbox = ResourceOutboxService(store, tenant_id="tenant-a")
    binding = bindings.create(project_id="project-a", resource_type="site", resource_id="site-a", role="website.analytics", integration_id="umami", desired_fingerprint="desired", state="DEGRADED")
    bindings.transition(binding["id"], state="DEGRADED", expected_revision=1, observed_fingerprint="observed")
    item = outbox.enqueue(project_id="project-a", aggregate_type="site", aggregate_id="site-a", aggregate_revision=2, event_type="resource.site.updated", payload={}, idempotency_key="event-a")
    with store.connection() as db:
        db.execute("UPDATE platform_resource_outbox SET status='dead_letter',last_error_code='provider_401' WHERE id=?", (item["id"],))
    result = ResourceReconciliationService(store, tenant_id="tenant-a").reconcile(bindings=bindings, outbox=outbox)
    assert result["open"] == 3
    codes = {item["code"] for item in ResourceReconciliationService(store, tenant_id="tenant-a").list_open()}
    assert codes == {"binding_attention", "binding_drift", "outbox_dead_letter"}


def test_reconciliation_is_idempotent_and_detects_tombstone_backlog(tmp_path) -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    bindings = ResourceBindingService(store, tenant_id="tenant-a")
    outbox = ResourceOutboxService(store, tenant_id="tenant-a")
    outbox.enqueue(project_id=None, aggregate_type="site", aggregate_id="site-a", aggregate_revision=1, event_type="resource.site.deleted", payload={"tombstone": True}, idempotency_key="delete-a")
    service = ResourceReconciliationService(store, tenant_id="tenant-a")
    assert service.reconcile(bindings=bindings, outbox=outbox)["open"] == 1
    assert service.reconcile(bindings=bindings, outbox=outbox)["open"] == 1
    assert len(service.list_open()) == 1
