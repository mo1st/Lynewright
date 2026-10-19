from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from app.services.activation_scope import IntegrationActivationScope
from app.services.capability_context import (
    CapabilityInvocationContext,
    bind_capability_context,
    current_capability_context,
)
from app.services.dependency_readiness import Dependency, DependencyReadinessGraph
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_registry import IntegrationRegistry
from app.services.operation_trace import OperationTraceService
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformStore, PlatformValidationError
from app.services.resource_bindings import ResourceBindingService
from app.services.resource_lifecycle import ExternalResource, ResourceLifecycleCoordinator
from app.services.resource_repair import ResourceRepairPolicy
from app.services.resource_lifecycle_host import (
    register_resource_lifecycle_coordinator,
    run_resource_lifecycle_tick,
    unregister_resource_lifecycle_coordinator,
)
from app.services.resource_outbox import ResourceOutboxService
from app.services.resource_setup_requirements import ResourceSetupRequirementService


def _context(*, trace_id: str = "trace-foundation") -> CapabilityInvocationContext:
    return CapabilityInvocationContext.from_actor(
        CapabilityActor.platform_manager(),
        tenant_id="tenant-a",
        actor_type="operator",
        entry_point="test",
        project_id="project-a",
        resource_type="site",
        resource_id="site-a",
        integration_id="lynewright.integration.test",
        connection_id="connection-a",
        trace_id=trace_id,
        idempotency_key="idem-a",
    )


def test_capability_context_is_immutable_and_bound_only_around_callback() -> None:
    context = _context()
    with pytest.raises(Exception):
        context.tenant_id = "tenant-b"  # type: ignore[misc]
    assert current_capability_context() is None
    with bind_capability_context(context):
        assert current_capability_context() == context
    assert current_capability_context() is None


def test_contribution_invocation_propagates_explicit_context_without_changing_handler_signature() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    integration_id = "lynewright.integration.context"
    integrations.register({
        "schema_version": "1.0", "id": integration_id, "name": "Context", "version": "0.1.0",
        "type": "native", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0",
        "backend": "dist/backend.mjs",
    })
    integrations.enable(integration_id)
    seen: list[object] = []
    contributions = ContributionRegistry(integrations)
    contributions.register(
        integration_id=integration_id, local_id="service.context", kind="service",
        handler=lambda: seen.append(current_capability_context()),
    )
    context = _context(trace_id="trace-contribution")
    contributions.invoke(f"{integration_id}:service.context", invocation_context=context)
    assert seen == [context]
    db.close()


def test_operation_trace_is_ordered_redacted_tenant_scoped_and_retryable() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = OperationTraceService(PlatformStore(lambda: db), tenant_id="tenant-a")
    context = _context()
    service.start(context, operation="site.create", metadata={"api_token": "secret", "attempt": 1})
    service.record(context, stage="provider", category="external", status="retrying", code="429", metadata={"safe": True})
    result = service.finish(context.trace_id, tenant_id="tenant-a", status="completed")
    assert result["status"] == "completed"
    assert [event["sequence"] for event in result["events"]] == [1, 2]
    assert result["events"][0]["metadata"]["api_token"] == "[REDACTED]"
    with pytest.raises(PlatformValidationError):
        service.get(context.trace_id, tenant_id="tenant-b")
    db.close()


def test_activation_scope_drains_and_disposes_in_reverse_order_once() -> None:
    scope = IntegrationActivationScope("lynewright.integration.scope")
    order: list[str] = []
    scope.register_disposer(lambda: order.append("first"))
    scope.register_disposer(lambda: order.append("second"))
    entered = threading.Event()
    release = threading.Event()

    def work() -> None:
        with scope.admit():
            entered.set()
            release.wait(1)

    worker = threading.Thread(target=work)
    worker.start()
    assert entered.wait(1)
    scope.stop_accepting()
    assert not scope.drain(timeout_seconds=0.01)
    release.set()
    worker.join(1)
    assert scope.dispose() == ()
    assert order == ["second", "first"]
    assert scope.dispose() == ()
    with pytest.raises(Exception):
        scope.admit().__enter__()


def test_dependency_graph_reports_waiting_ready_and_cycle_path() -> None:
    graph = DependencyReadinessGraph()
    credential = Dependency("credential", "umami")
    graph.register("analytics", [credential])
    assert graph.diagnostics("analytics")["state"] == "WAITING"
    graph.set_available(credential)
    assert graph.diagnostics("analytics")["state"] == "READY"

    graph.register("a", [Dependency("integration", "b")])
    with pytest.raises(PlatformValidationError, match="DEPENDENCY_CYCLE"):
        graph.register("b", [Dependency("integration", "a")])
    with pytest.raises(PlatformValidationError):
        graph.node("b")


def test_dependency_graph_recovers_after_dependency_loss() -> None:
    graph = DependencyReadinessGraph()
    credential = Dependency("credential", "umami")
    connection = Dependency("connection", "primary")
    graph.register("analytics", [credential, connection])
    graph.set_available(credential)
    graph.set_available(connection)
    assert graph.diagnostics("analytics") == {
        "node_id": "analytics",
        "state": "READY",
        "ready_dependencies": ["connection:primary", "credential:umami"],
        "waiting_on": [],
    }
    graph.set_available(connection, available=False)
    lost = graph.diagnostics("analytics")
    assert lost["state"] == "WAITING"
    assert lost["waiting_on"] == ["connection:primary"]
    graph.set_available(connection)
    assert graph.diagnostics("analytics")["state"] == "READY"


def test_resource_binding_revision_is_tenant_scoped_and_fenced() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceBindingService(PlatformStore(lambda: db), tenant_id="tenant-a")
    binding = service.create(
        project_id="project-a", resource_type="site", resource_id="site-a", role="website.analytics",
        integration_id="umami", connection_id="conn-a", metadata={"token": "secret"},
    )
    assert binding["state"] == "PENDING"
    assert binding["metadata"]["token"] == "[REDACTED]"
    ready = service.transition(binding["id"], state="READY", expected_revision=1, external_resource_id="website-a")
    assert ready["revision"] == 2
    with pytest.raises(Exception):
        service.transition(binding["id"], state="DEGRADED", expected_revision=1)
    assert service.find(
        resource_type="site", resource_id="site-a", role="website.analytics",
        integration_id="umami", connection_id="conn-a",
    )["id"] == binding["id"]
    db.close()


def test_resource_binding_rejects_overlong_optional_fields_and_non_finite_metadata() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceBindingService(PlatformStore(lambda: db), tenant_id="tenant-a")
    base = {
        "project_id": "project-a", "resource_type": "site", "resource_id": "site-a",
        "role": "website.analytics", "integration_id": "umami",
    }
    with pytest.raises(PlatformValidationError, match="optional fields"):
        service.create(**base, external_resource_id="x" * 257)
    with pytest.raises(PlatformValidationError, match="metadata"):
        service.create(**base, metadata={"value": float("nan")})
    binding = service.create(**base)
    with pytest.raises(PlatformValidationError, match="transition fields"):
        service.transition(binding["id"], state="READY", expected_revision=1, external_resource_id="x" * 257)
    with pytest.raises(PlatformValidationError, match="filter"):
        service.list(resource_id="x" * 257)
    with pytest.raises(PlatformValidationError, match="id"):
        service.get("x" * 257)
    db.close()


def test_setup_requirements_reject_non_object_data_and_overlong_transition_fields() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceSetupRequirementService(PlatformStore(lambda: db), tenant_id="tenant-a")
    with pytest.raises(PlatformValidationError, match="object"):
        service.ensure(binding_id="binding-a", requirement_type="manual_instruction", instructions=["invalid"])  # type: ignore[arg-type]
    with pytest.raises(PlatformValidationError, match="JSON serializable"):
        service.ensure(binding_id="binding-a", requirement_type="manual_instruction", instructions={"value": float("nan")})
    requirement = service.ensure(binding_id="binding-a", requirement_type="manual_instruction")
    with pytest.raises(PlatformValidationError, match="transition fields"):
        service.transition(requirement["id"], state="FAILED", expected_revision=1, error_message="x" * 2_001)
    with db:
        db.execute("UPDATE platform_resource_setup_requirements SET instructions_json='[]' WHERE id=?", (requirement["id"],))
    assert service.get(requirement["id"])["instructions"] == {}
    db.close()


def test_resource_outbox_is_idempotent_ordered_and_fenced() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    service = ResourceOutboxService(PlatformStore(lambda: db), tenant_id="tenant-a")
    first = service.enqueue(
        project_id="project-a", aggregate_type="site", aggregate_id="1", aggregate_revision=1,
        event_type="resource.site.created", payload={"api_token": "secret", "site_id": 1},
        idempotency_key="site:1:created:1",
    )
    duplicate = service.enqueue(
        project_id="project-a", aggregate_type="site", aggregate_id="1", aggregate_revision=1,
        event_type="resource.site.created", payload={"site_id": 1}, idempotency_key="site:1:created:1",
    )
    assert duplicate["id"] == first["id"]
    later = service.enqueue(
        project_id="project-a", aggregate_type="site", aggregate_id="1", aggregate_revision=2,
        event_type="resource.site.updated", payload={"site_id": 1}, idempotency_key="site:1:updated:2",
    )
    claimed = service.claim_next(worker_id="worker-a", lease_seconds=30)
    assert claimed is not None and claimed["id"] == first["id"]
    assert claimed["payload"]["api_token"] == "[REDACTED]"
    assert service.claim_next(worker_id="worker-b", lease_seconds=30) is None
    with pytest.raises(Exception):
        service.complete(outbox_id=first["id"], worker_id="worker-b", lease_token=claimed["lease_token"])
    service.complete(outbox_id=first["id"], worker_id="worker-a", lease_token=claimed["lease_token"])
    next_claim = service.claim_next(worker_id="worker-a", lease_seconds=30)
    assert next_claim is not None and next_claim["id"] == later["id"]
    db.close()


class _FakeLifecycleProvider:
    integration_id = "umami"
    role = "website.analytics"

    def __init__(self, *, fail_first_provision: bool = False, setup_required: bool = False) -> None:
        self.fail_first_provision = fail_first_provision
        self.setup_required = setup_required
        self.provisions = 0
        self.discoveries = 0
        self.retirements = 0
        self.created: ExternalResource | None = None

    def discover(self, *, payload: dict[str, object], idempotency_key: str) -> ExternalResource | None:
        self.discoveries += 1
        return self.created

    def provision(self, *, payload: dict[str, object], idempotency_key: str) -> ExternalResource:
        self.provisions += 1
        self.created = ExternalResource(
            "external-site-a", "https://analytics.example.test/site-a",
            setup_required=(
                ({"type": "manual_instruction", "instructions": {"text": "Paste tracker"}},)
                if self.setup_required else ()
            ),
        )
        if self.fail_first_provision and self.provisions == 1:
            from app.services.platform_services import PlatformOperationError
            raise PlatformOperationError("provider timeout after create")
        return self.created

    def verify(self, *, external: ExternalResource, payload: dict[str, object]) -> bool:
        return True

    def retire(self, *, external: ExternalResource, idempotency_key: str) -> None:
        self.retirements += 1


def _coordinator(provider: _FakeLifecycleProvider) -> tuple[ResourceLifecycleCoordinator, ResourceOutboxService, ResourceBindingService, sqlite3.Connection]:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    outbox = ResourceOutboxService(store, tenant_id="tenant-a")
    bindings = ResourceBindingService(store, tenant_id="tenant-a")
    setup = ResourceSetupRequirementService(store, tenant_id="tenant-a")
    coordinator = ResourceLifecycleCoordinator(outbox, bindings, setup, provider_resolver=lambda _item: provider)
    return coordinator, outbox, bindings, db


def test_resource_lifecycle_coordinator_discovers_provisions_verifies_and_is_idempotent() -> None:
    provider = _FakeLifecycleProvider()
    coordinator, outbox, bindings, db = _coordinator(provider)
    outbox.enqueue(
        project_id="project-a", aggregate_type="site", aggregate_id="site-a", aggregate_revision=1,
        event_type="resource.site.created", payload={"site_id": "site-a"}, idempotency_key="site-a:create",
    )
    result = coordinator.process_one(worker_id="worker-a")
    assert result["status"] == "processed" and result["action"] == "ready"
    binding = bindings.find(
        resource_type="site", resource_id="site-a", role="website.analytics", integration_id="umami",
    )
    assert binding is not None and binding["state"] == "READY"
    assert provider.provisions == 1
    db.close()


def test_resource_lifecycle_retries_after_provider_timeout_and_discovery_prevents_duplicate() -> None:
    provider = _FakeLifecycleProvider(fail_first_provision=True)
    coordinator, outbox, bindings, db = _coordinator(provider)
    item = outbox.enqueue(
        project_id="project-a", aggregate_type="site", aggregate_id="site-a", aggregate_revision=1,
        event_type="resource.site.created", payload={"site_id": "site-a"}, idempotency_key="site-a:crash",
    )
    first = coordinator.process_one(worker_id="worker-a")
    assert first["status"] == "retrying" and provider.provisions == 1
    with db:
        db.execute("UPDATE platform_resource_outbox SET available_at=CURRENT_TIMESTAMP WHERE id=?", (item["id"],))
    second = coordinator.process_one(worker_id="worker-a")
    assert second["status"] == "processed"
    assert provider.provisions == 1  # the retry discovers the externally-created resource
    assert bindings.find(resource_type="site", resource_id="site-a", role="website.analytics", integration_id="umami")["state"] == "READY"
    db.close()


def test_resource_lifecycle_retries_provider_auth_and_rate_limit_failures() -> None:
    from app.services.platform_services import PlatformOperationError

    for error_code in ("provider_401", "provider_429"):
        provider = _FakeLifecycleProvider()
        failure = type("ProviderFailure", (PlatformOperationError,), {"code": error_code})
        provider.provision = lambda **_kwargs: (_ for _ in ()).throw(failure("provider rejected request"))  # type: ignore[method-assign]
        coordinator, outbox, _bindings, db = _coordinator(provider)
        item = outbox.enqueue(
            project_id="project-a", aggregate_type="site", aggregate_id="site-a", aggregate_revision=1,
            event_type="resource.site.created", payload={}, idempotency_key=f"site-a:{error_code}",
        )
        result = coordinator.process_one(worker_id="worker-a")
        assert result["status"] == "retrying"
        stored = outbox.get(item["id"])
        assert stored["status"] == "pending"
        assert stored["last_error_code"] == error_code
        db.close()


def test_resource_lifecycle_dead_letters_unexpected_provider_crash_without_leaking_error() -> None:
    provider = _FakeLifecycleProvider()
    provider.provision = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("secret-token must not be persisted"))  # type: ignore[method-assign]
    coordinator, outbox, _bindings, db = _coordinator(provider)
    item = outbox.enqueue(
        project_id="project-a", aggregate_type="site", aggregate_id="site-a", aggregate_revision=1,
        event_type="resource.site.created", payload={}, idempotency_key="site-a:unexpected-crash",
    )
    result = coordinator.process_one(worker_id="worker-a")
    assert result == {"outbox_id": item["id"], "status": "dead_letter", "error": "RuntimeError"}
    stored = outbox.get(item["id"])
    assert stored["status"] == "dead_letter"
    assert stored["last_error_code"] == "lifecycle_unexpected"
    assert "secret-token" not in stored["last_error_message"]
    db.close()


def test_setup_required_keeps_binding_out_of_ready_state() -> None:
    provider = _FakeLifecycleProvider(setup_required=True)
    coordinator, outbox, bindings, db = _coordinator(provider)
    outbox.enqueue(
        project_id="project-a", aggregate_type="site", aggregate_id="site-a", aggregate_revision=1,
        event_type="resource.site.created", payload={"site_id": "site-a"}, idempotency_key="site-a:setup",
    )
    result = coordinator.process_one(worker_id="worker-a")
    assert result["action"] == "setup_required"
    binding = bindings.find(resource_type="site", resource_id="site-a", role="website.analytics", integration_id="umami")
    assert binding["state"] == "SETUP_REQUIRED"
    requirements = ResourceSetupRequirementService(outbox.store, tenant_id="tenant-a")
    assert requirements.get(requirements.ensure(binding_id=binding["id"], requirement_type="manual_instruction")["id"])["state"] == "MANUAL_REQUIRED"
    db.close()


def test_setup_verification_promotes_binding_only_after_provider_verifies() -> None:
    provider = _FakeLifecycleProvider(setup_required=True)
    coordinator, outbox, bindings, db = _coordinator(provider)
    outbox.enqueue(project_id="project-a", aggregate_type="site", aggregate_id="site-a", aggregate_revision=1, event_type="resource.site.created", payload={"site_id": "site-a"}, idempotency_key="site-a:verify")
    assert coordinator.process_one(worker_id="worker-a")["action"] == "setup_required"
    binding = bindings.find(resource_type="site", resource_id="site-a", role="website.analytics", integration_id="umami")
    requirement = ResourceSetupRequirementService(outbox.store, tenant_id="tenant-a").list(binding_id=binding["id"])[0]
    result = coordinator.verify_setup(binding_id=binding["id"], requirement_id=requirement["id"], evidence={"method": "manual_check"})
    assert result["action"] == "ready"
    assert bindings.get(binding["id"])["state"] == "READY"
    db.close()


def test_resource_lifecycle_host_is_a_bounded_hook_not_a_second_scheduler() -> None:
    provider = _FakeLifecycleProvider()
    coordinator, outbox, bindings, db = _coordinator(provider)
    outbox.enqueue(
        project_id="project-a", aggregate_type="site", aggregate_id="site-a", aggregate_revision=1,
        event_type="resource.site.created", payload={"site_id": "site-a"}, idempotency_key="site-a:host",
    )
    register_resource_lifecycle_coordinator("tenant-a", coordinator)
    try:
        tick = run_resource_lifecycle_tick("tenant-a", max_items=1)
        assert tick["processed"] == 1
        assert "attention" in tick
        assert bindings.find(resource_type="site", resource_id="site-a", role="website.analytics", integration_id="umami")["state"] == "READY"
        assert run_resource_lifecycle_tick("tenant-a", max_items=1)["processed"] == 0
    finally:
        unregister_resource_lifecycle_coordinator("tenant-a")
        db.close()


def test_resource_lifecycle_reconciliation_recovers_and_flags_drift() -> None:
    provider = _FakeLifecycleProvider()
    coordinator, _outbox, bindings, db = _coordinator(provider)
    provider.created = ExternalResource("external-site-a", observed_fingerprint="desired")
    binding = bindings.create(project_id="project-a", resource_type="site", resource_id="site-a", role="website.analytics", integration_id="umami", state="DEGRADED", external_resource_id="external-site-a", desired_fingerprint="desired")
    bindings.transition(binding["id"], state="DEGRADED", expected_revision=1, observed_fingerprint="old")
    result = coordinator.reconcile_bindings()
    assert result[0]["action"] == "ready"
    assert bindings.get(binding["id"])["state"] == "READY"

    provider.created = ExternalResource("external-site-a", observed_fingerprint="different")
    current = bindings.get(binding["id"])
    result = coordinator.reconcile_bindings()
    assert result[0]["action"] == "drift_detected"
    assert bindings.get(current["id"])["state"] == "DEGRADED"
    db.close()


def test_repair_policy_only_allows_explicit_managed_provider_repair() -> None:
    class RepairProvider:
        def repair(self, **_kwargs):
            return ExternalResource("external-site-a", observed_fingerprint="desired")

    allowed = ResourceRepairPolicy.decide(
        binding={"state": "DEGRADED", "ownership_mode": "managed", "desired_fingerprint": "desired"},
        provider=RepairProvider(),
        observed_fingerprint="different",
    )
    refused = ResourceRepairPolicy.decide(
        binding={"state": "DEGRADED", "ownership_mode": "linked", "desired_fingerprint": "desired"},
        provider=RepairProvider(),
        observed_fingerprint="different",
    )
    assert allowed.allowed and allowed.code == "safe_managed_drift"
    assert not refused.allowed and refused.code == "ownership_not_managed"


def test_repair_policy_caps_repeated_automatic_repairs() -> None:
    class RepairProvider:
        def repair(self, **_kwargs):
            return ExternalResource("external-site-a", observed_fingerprint="desired")

    decision = ResourceRepairPolicy.decide(
        binding={
            "state": "DEGRADED", "ownership_mode": "managed", "desired_fingerprint": "desired",
            "metadata": {"automatic_repair_count": ResourceRepairPolicy.MAX_AUTOMATIC_REPAIRS},
        },
        provider=RepairProvider(),
        observed_fingerprint="different",
    )
    assert not decision.allowed and decision.code == "repair_limit_reached"


def test_resource_lifecycle_auto_repairs_safe_managed_drift() -> None:
    class RepairProvider(_FakeLifecycleProvider):
        def repair(self, **_kwargs):
            return ExternalResource("external-site-a", observed_fingerprint="desired")

    provider = RepairProvider()
    provider.created = ExternalResource("external-site-a", observed_fingerprint="different")
    coordinator, _outbox, bindings, db = _coordinator(provider)
    binding = bindings.create(project_id="project-a", resource_type="site", resource_id="site-a", role="website.analytics", integration_id="umami", state="DEGRADED", external_resource_id="external-site-a", desired_fingerprint="desired")
    bindings.transition(binding["id"], state="DEGRADED", expected_revision=1, observed_fingerprint="old")
    result = coordinator.reconcile_bindings()
    assert result[0]["action"] == "auto_repaired"
    assert bindings.get(binding["id"])["state"] == "READY"
    assert bindings.get(binding["id"])["metadata"]["repair"] == "automatic"
    assert bindings.get(binding["id"])["metadata"]["automatic_repair_count"] == 1
    db.close()


def test_resource_lifecycle_supports_explicit_operator_repair_for_linked_bindings() -> None:
    class RepairProvider(_FakeLifecycleProvider):
        def repair(self, **_kwargs):
            return ExternalResource("external-site-a", observed_fingerprint="desired")

    provider = RepairProvider()
    provider.created = ExternalResource("external-site-a", observed_fingerprint="different")
    coordinator, _outbox, bindings, db = _coordinator(provider)
    binding = bindings.create(
        project_id="project-a", resource_type="site", resource_id="site-a",
        role="website.analytics", integration_id="umami", state="DEGRADED",
        ownership_mode="linked", external_resource_id="external-site-a",
        desired_fingerprint="desired",
    )

    result = coordinator.repair_binding(
        binding_id=binding["id"], operator_id="operator-a", reason="Confirmed the linked resource is ours."
    )

    assert result["action"] == "operator_repaired"
    changed = bindings.get(binding["id"])
    assert changed["state"] == "READY"
    assert changed["metadata"]["repair"] == "operator"
    assert changed["metadata"]["repair_operator"] == "operator-a"
    db.close()


def test_resource_lifecycle_reconciliation_reports_provider_unavailable_without_mutating_binding() -> None:
    provider = _FakeLifecycleProvider()
    coordinator, _outbox, bindings, db = _coordinator(provider)
    binding = bindings.create(
        project_id="project-a", resource_type="site", resource_id="site-a",
        role="website.analytics", integration_id="umami", state="READY",
        external_resource_id="external-site-a",
    )
    coordinator.provider_resolver = lambda _item: None

    result = coordinator.reconcile_bindings()

    assert result == [{"binding_id": binding["id"], "action": "provider_unavailable"}]
    assert bindings.get(binding["id"])["state"] == "READY"
    db.close()


def test_resource_lifecycle_reconciliation_marks_missing_external_resource_degraded() -> None:
    provider = _FakeLifecycleProvider()
    coordinator, _outbox, bindings, db = _coordinator(provider)
    binding = bindings.create(
        project_id="project-a", resource_type="site", resource_id="site-a",
        role="website.analytics", integration_id="umami", state="READY",
        external_resource_id="external-site-a",
    )

    result = coordinator.reconcile_bindings()

    assert result == [{"binding_id": binding["id"], "action": "external_missing"}]
    changed = bindings.get(binding["id"])
    assert changed["state"] == "DEGRADED"
    assert changed["last_error_code"] == "external_missing"
    db.close()


def test_resource_lifecycle_reconciliation_marks_provider_verification_failure_degraded() -> None:
    provider = _FakeLifecycleProvider()
    provider.created = ExternalResource("external-site-a", observed_fingerprint="desired")
    provider.verify = lambda **_kwargs: False  # type: ignore[method-assign]
    coordinator, _outbox, bindings, db = _coordinator(provider)
    binding = bindings.create(
        project_id="project-a", resource_type="site", resource_id="site-a",
        role="website.analytics", integration_id="umami", state="READY",
        external_resource_id="external-site-a", desired_fingerprint="desired",
    )

    result = coordinator.reconcile_bindings()

    assert result == [{"binding_id": binding["id"], "action": "verification_failed"}]
    changed = bindings.get(binding["id"])
    assert changed["state"] == "DEGRADED"
    assert changed["last_error_code"] == "verification_failed"
    db.close()


def test_resource_lifecycle_reconciliation_contains_provider_operation_errors() -> None:
    from app.services.platform_services import PlatformOperationError

    provider = _FakeLifecycleProvider()
    provider.discover = lambda **_kwargs: (_ for _ in ()).throw(PlatformOperationError("provider unavailable"))  # type: ignore[method-assign]
    coordinator, _outbox, bindings, db = _coordinator(provider)
    binding = bindings.create(
        project_id="project-a", resource_type="site", resource_id="site-a",
        role="website.analytics", integration_id="umami", state="READY",
        external_resource_id="external-site-a",
    )

    result = coordinator.reconcile_bindings()

    assert result == [{"binding_id": binding["id"], "action": "provider_error"}]
    assert bindings.get(binding["id"])["state"] == "READY"
    db.close()
