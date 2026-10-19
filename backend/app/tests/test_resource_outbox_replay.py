from __future__ import annotations

import sqlite3

import pytest

from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformStore, PlatformValidationError
from app.services.operation_trace import OperationTraceService
from app.services.resource_outbox import ResourceOutboxService


def test_operator_can_replay_dead_letter_once_with_reason() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-a")
    item = service.enqueue(project_id=None, aggregate_type="site", aggregate_id="site-a", aggregate_revision=1, event_type="resource.site.updated", payload={}, idempotency_key="replay-a")
    claimed = service.claim_next(worker_id="worker-a")
    service.dead_letter(outbox_id=item["id"], worker_id="worker-a", lease_token=claimed["lease_token"], error_code="provider_401", error_message="unauthorized")
    replayed = service.replay_dead_letter(outbox_id=item["id"], operator_id="operator-a", reason="Credentials were rotated; retry once.")
    assert replayed["status"] == "pending"
    assert replayed["attempts"] == 0
    assert replayed["replay_count"] == 1
    assert replayed["last_replayed_by"] == "operator-a"
    assert service.claim_next(worker_id="worker-b")["id"] == item["id"]
    with db:
        trace = db.execute("SELECT operation,tenant_id FROM platform_operation_traces ORDER BY created_at DESC LIMIT 1").fetchone()
    assert trace is not None and trace["operation"] == "resource.outbox.replay" and trace["tenant_id"] == "tenant-a"
    with pytest.raises(PlatformConflictError):
        service.replay_dead_letter(outbox_id=item["id"], operator_id="operator-a", reason="Replay the same item again.")


def test_operator_can_atomically_replay_a_bounded_batch_of_dead_letters() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-a")
    items = [
        service.enqueue(project_id=None, aggregate_type="site", aggregate_id=f"site-{index}", aggregate_revision=1, event_type="resource.site.updated", payload={}, idempotency_key=f"batch-{index}")
        for index in (1, 2)
    ]
    claimed_ids = []
    for _item in items:
        claimed = service.claim_next(worker_id="worker-a")
        claimed_ids.append(claimed["id"])
        service.dead_letter(outbox_id=claimed["id"], worker_id="worker-a", lease_token=claimed["lease_token"], error_code="provider_429", error_message="rate limited")

    replayed = service.replay_dead_letters(
        outbox_ids=claimed_ids, operator_id="operator-a", reason="Provider recovered; retry batch."
    )

    assert [item["status"] for item in replayed] == ["pending", "pending"]
    assert [item["replay_count"] for item in replayed] == [1, 1]
    db.close()


def test_batch_replay_is_all_or_nothing_when_one_item_is_not_dead_lettered() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-a")
    dead = service.enqueue(project_id=None, aggregate_type="site", aggregate_id="site-dead", aggregate_revision=1, event_type="resource.site.updated", payload={}, idempotency_key="batch-dead")
    claimed = service.claim_next(worker_id="worker-a")
    service.dead_letter(outbox_id=claimed["id"], worker_id="worker-a", lease_token=claimed["lease_token"], error_code="provider_401", error_message="unauthorized")
    assert claimed["id"] == dead["id"]
    pending = service.enqueue(project_id=None, aggregate_type="site", aggregate_id="site-pending", aggregate_revision=1, event_type="resource.site.updated", payload={}, idempotency_key="batch-pending")

    with pytest.raises(PlatformConflictError):
        service.replay_dead_letters(outbox_ids=[dead["id"], pending["id"]], operator_id="operator-a", reason="Do not partially replay.")
    assert service.get(dead["id"])["status"] == "dead_letter"
    assert service.get(pending["id"])["status"] == "pending"
    db.close()


def test_batch_replay_cannot_cross_tenant_boundaries() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    tenant_a = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-a")
    tenant_b = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-b")
    item = tenant_a.enqueue(project_id=None, aggregate_type="site", aggregate_id="site-a", aggregate_revision=1, event_type="resource.site.updated", payload={}, idempotency_key="cross-tenant")
    claimed = tenant_a.claim_next(worker_id="worker-a")
    tenant_a.dead_letter(outbox_id=item["id"], worker_id="worker-a", lease_token=claimed["lease_token"], error_code="provider_401", error_message="unauthorized")

    with pytest.raises(PlatformNotFoundError):
        tenant_b.replay_dead_letters(outbox_ids=[item["id"]], operator_id="operator-b", reason="must not cross tenant")
    assert tenant_a.get(item["id"])["status"] == "dead_letter"
    db.close()


def test_replay_rejects_non_dead_letters_and_invalid_reason() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-a")
    item = service.enqueue(project_id=None, aggregate_type="site", aggregate_id="site-a", aggregate_revision=1, event_type="resource.site.updated", payload={}, idempotency_key="replay-b")
    with pytest.raises(PlatformConflictError):
        service.replay_dead_letter(outbox_id=item["id"], operator_id="operator-a", reason="not dead letter")
    with pytest.raises(PlatformValidationError):
        service.replay_dead_letter(outbox_id=item["id"], operator_id="operator-a", reason=" ")
    with pytest.raises(PlatformValidationError):
        service.replay_dead_letter(outbox_id=item["id"], operator_id="   ", reason="operator id is blank")


def test_outbox_rejects_non_object_and_non_finite_payloads() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-a")
    with pytest.raises(PlatformValidationError, match="object"):
        service.enqueue(project_id=None, aggregate_type="site", aggregate_id="site-a", aggregate_revision=1, event_type="resource.site.updated", payload=["invalid"], idempotency_key="strict-list")  # type: ignore[arg-type]
    with pytest.raises(PlatformValidationError, match="strict JSON"):
        service.enqueue(project_id=None, aggregate_type="site", aggregate_id="site-a", aggregate_revision=1, event_type="resource.site.updated", payload={"value": float("nan")}, idempotency_key="strict-nan")


def test_outbox_claim_requires_a_bounded_nonblank_worker_lease() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-a")
    with pytest.raises(PlatformValidationError):
        service.claim_next(worker_id="   ")
    with pytest.raises(PlatformValidationError):
        service.claim_next(worker_id="worker-a", lease_seconds=3_601)
    with pytest.raises(PlatformValidationError):
        service.claim_next(worker_id="w" * 121)


def test_outbox_rejects_overlong_identifiers_and_unknown_status_filters() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-a")
    with pytest.raises(PlatformValidationError, match="identifiers"):
        service.enqueue(project_id=None, aggregate_type="site", aggregate_id="site-a", aggregate_revision=1, event_type="resource.site.updated", payload={}, idempotency_key="k" * 513)
    with pytest.raises(PlatformValidationError, match="status"):
        service.list(status="unknown")


def test_outbox_public_and_claimed_payloads_remain_objects_for_legacy_rows() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-a")
    item = service.enqueue(project_id=None, aggregate_type="site", aggregate_id="site-a", aggregate_revision=1, event_type="resource.site.updated", payload={}, idempotency_key="legacy-shape")
    with db:
        db.execute("UPDATE platform_resource_outbox SET payload_json='[]' WHERE id=?", (item["id"],))
    assert service.get(item["id"])["payload"] == {}
    claimed = service.claim_next(worker_id="worker-a")
    assert claimed is not None and claimed["payload"] == {}
