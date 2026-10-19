from __future__ import annotations

import json
import sqlite3

import pytest

from app.services.integration_capability_evidence import IntegrationCapabilityEvidenceService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_errors import IntegrationErrorCategory, IntegrationExternalError
from app.services.integration_provider_selection import IntegrationProviderSelectionService
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_service_contracts import IntegrationServiceContractRegistry
from app.services.platform_services import PlatformNotFoundError, PlatformStore


def _manifest(integration_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0", "id": integration_id, "name": integration_id, "version": "0.1.0",
        "type": "native", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0", "backend": "dist/backend.mjs",
    }


def test_capability_evidence_is_durable_and_definition_change_invalidates_verification() -> None:
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db); integration_id = "lynewright.integration.provider-a"
    registry = IntegrationRegistry(store); registry.register(_manifest(integration_id)); registry.enable(integration_id)
    evidence = IntegrationCapabilityEvidenceService(registry, now=lambda: 123.0)

    declared = evidence.declare(integration_id=integration_id, contract="lynewright.social.publisher", capability="posts.create")
    assert declared["state"] == "DECLARED" and declared["verified_at_epoch"] is None
    verified = evidence.set_state(integration_id=integration_id, contract="lynewright.social.publisher", capability="posts.create", state="VERIFIED")
    assert verified["verified"] is True and verified["verified_at_epoch"] == 123.0

    manifest = registry.get(integration_id)["manifest"]
    manifest["name"] = "Changed provider definition"
    db.execute("UPDATE platform_extensions SET manifest_json=? WHERE tenant_id=? AND id=?", (json.dumps(manifest), "ten_default", integration_id)); db.commit()
    invalidated = evidence.declare(integration_id=integration_id, contract="lynewright.social.publisher", capability="posts.create")
    assert invalidated["state"] == "DECLARED"
    assert invalidated["verified_at_epoch"] is None
    assert invalidated["reason"] == "definition_identity_changed"
    db.close()


def test_degraded_or_unavailable_capability_blocks_critical_operation_with_normalized_error() -> None:
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db); integration_id = "lynewright.integration.provider-a"
    registry = IntegrationRegistry(store); registry.register(_manifest(integration_id)); registry.enable(integration_id)
    evidence = IntegrationCapabilityEvidenceService(registry)
    evidence.declare(integration_id=integration_id, contract="lynewright.social.publisher", capability="posts.create")
    evidence.set_state(integration_id=integration_id, contract="lynewright.social.publisher", capability="posts.create", state="DEGRADED", reason="token expired")

    with pytest.raises(IntegrationExternalError) as failure:
        evidence.require_usable(integration_id=integration_id, contract="lynewright.social.publisher", capability="posts.create", require_verified=True)
    assert failure.value.category is IntegrationErrorCategory.PROVIDER_UNAVAILABLE
    assert failure.value.diagnostic["state"] == "DEGRADED"
    db.close()


def test_contract_invocation_can_require_verified_evidence_before_handler_execution() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db); integration_id = "lynewright.integration.provider-a"
    registry = IntegrationRegistry(store); registry.register(_manifest(integration_id)); registry.enable(integration_id)
    contributions = ContributionRegistry(registry)
    invoked: list[str] = []
    contributions.register(integration_id=integration_id, local_id="social.publisher", kind="service", metadata={"contract": "lynewright.social.publisher", "version": "1.0.0"}, handler=lambda operation, _payload: invoked.append(operation) or {})
    evidence = IntegrationCapabilityEvidenceService(registry)
    evidence.declare(integration_id=integration_id, contract="lynewright.social.publisher", capability="health")
    services = IntegrationServiceContractRegistry(contributions, evidence=evidence)

    with pytest.raises(IntegrationExternalError) as unavailable:
        services.invoke("lynewright.social.publisher", "health", require_verified=True)
    assert unavailable.value.category is IntegrationErrorCategory.PROVIDER_UNAVAILABLE
    assert invoked == []
    evidence.set_state(integration_id=integration_id, contract="lynewright.social.publisher", capability="health", state="VERIFIED")
    assert services.invoke("lynewright.social.publisher", "health", require_verified=True) == {}
    assert invoked == ["health"]
    db.close()


def test_single_active_provider_is_auto_resolved_without_redundant_selection() -> None:
    """A lone compatible integration is callable for every future contract."""
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integration_id = "lynewright.integration.provider-a"
    registry = IntegrationRegistry(store); registry.register(_manifest(integration_id)); registry.enable(integration_id)
    contributions = ContributionRegistry(registry)
    contributions.register(
        integration_id=integration_id, local_id="service", kind="service",
        metadata={"contract": "lynewright.future.service", "version": "0.1.0"},
        handler=lambda operation, _payload: {"provider": integration_id, "operation": operation},
    )
    selections = IntegrationProviderSelectionService(registry)
    services = IntegrationServiceContractRegistry(contributions, selections=selections)

    assert services.invoke("lynewright.future.service", "execute") == {
        "provider": integration_id, "operation": "execute",
    }
    with pytest.raises(PlatformNotFoundError):
        services.invoke("lynewright.future.service", "execute", version_range="^1.0.0")
    db.close()


def test_multiple_active_providers_without_selection_fail_closed() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    registry = IntegrationRegistry(store)
    for integration_id in ("lynewright.integration.provider-a", "lynewright.integration.provider-b"):
        registry.register(_manifest(integration_id)); registry.enable(integration_id)
    contributions = ContributionRegistry(registry)
    for integration_id in ("lynewright.integration.provider-a", "lynewright.integration.provider-b"):
        contributions.register(
            integration_id=integration_id, local_id="service", kind="service",
            metadata={"contract": "lynewright.future.service", "version": "1.0.0"},
            handler=lambda operation, _payload: {"provider": integration_id, "operation": operation},
        )
    services = IntegrationServiceContractRegistry(
        contributions, selections=IntegrationProviderSelectionService(registry),
    )

    with pytest.raises(PlatformNotFoundError, match="select one explicitly"):
        services.invoke("lynewright.future.service", "execute")
    db.close()


def test_explicit_provider_selection_audits_change_and_never_silently_falls_back() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db); first_id, second_id = "lynewright.integration.provider-a", "lynewright.integration.provider-b"
    registry = IntegrationRegistry(store)
    for integration_id in (first_id, second_id):
        registry.register(_manifest(integration_id)); registry.enable(integration_id)
    contributions = ContributionRegistry(registry)
    calls: list[str] = []
    for integration_id in (first_id, second_id):
        contributions.register(
            integration_id=integration_id, local_id="social.publisher", kind="service",
            metadata={"contract": "lynewright.social.publisher", "version": "1.0.0"},
            handler=lambda operation, _payload, owner=integration_id: calls.append(owner) or {"provider": owner, "operation": operation},
        )
    selections = IntegrationProviderSelectionService(registry)
    selected = selections.select(contract="lynewright.social.publisher", integration_id=first_id, reason="operator choice", actor_id="client-1")
    services = IntegrationServiceContractRegistry(contributions, selections=selections)
    assert services.invoke("lynewright.social.publisher", "health")["provider"] == first_id
    registry.disable(first_id)
    with pytest.raises(IntegrationExternalError) as unavailable:
        services.invoke("lynewright.social.publisher", "health")
    assert unavailable.value.category is IntegrationErrorCategory.PROVIDER_UNAVAILABLE
    assert calls == [first_id]

    changed = selections.select(contract="lynewright.social.publisher", integration_id=second_id, reason="explicit replacement", actor_id="client-1")
    assert services.invoke("lynewright.social.publisher", "health")["provider"] == second_id
    events = selections.events.list()
    event = next(item for item in events if item["id"] == changed["event_id"])
    assert event["type"] == "integration.provider.changed"
    assert event["payload"]["previous_integration_id"] == first_id
    db.close()


def test_provider_selection_inherits_tenant_default_without_health_failover() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    tenant_a = IntegrationRegistry(store, tenant_id="tenant-a")
    global_id = "lynewright.integration.global-provider"
    project_id = "lynewright.integration.project-provider"
    for integration_id in (global_id, project_id):
        tenant_a.register(_manifest(integration_id)); tenant_a.enable(integration_id)

    contributions = ContributionRegistry(tenant_a)
    calls: list[str] = []
    for integration_id in (global_id, project_id):
        contributions.register(
            integration_id=integration_id,
            local_id="analytics.publisher",
            kind="service",
            metadata={"contract": "lynewright.analytics.publisher", "version": "1.0.0"},
            handler=lambda operation, _payload, owner=integration_id: calls.append(owner) or {
                "provider": owner,
                "operation": operation,
            },
        )

    selections = IntegrationProviderSelectionService(tenant_a)
    selections.select(
        contract="lynewright.analytics.publisher",
        integration_id=global_id,
        reason="tenant default",
    )

    inherited = selections.get(
        contract="lynewright.analytics.publisher", project_id="project-1"
    )
    assert inherited["integration_id"] == global_id
    assert inherited["selection_scope"] == "tenant_default"
    assert inherited["source_project_id"] is None
    assert inherited["requested_project_id"] == "project-1"
    assert inherited["resolution_trace"] == {
        "contract": "lynewright.analytics.publisher",
        "requested_project_id": "project-1",
        "integration_id": global_id,
        "selection_scope": "tenant_default",
        "source_project_id": None,
    }
    assert "credential" not in str(inherited["resolution_trace"]).lower()

    services = IntegrationServiceContractRegistry(contributions, selections=selections)
    assert services.invoke(
        "lynewright.analytics.publisher", "publish", project_id="project-2"
    )["provider"] == global_id

    selections.select(
        contract="lynewright.analytics.publisher",
        integration_id=project_id,
        project_id="project-1",
        reason="project override",
    )
    override = selections.get(
        contract="lynewright.analytics.publisher", project_id="project-1"
    )
    assert override["integration_id"] == project_id
    assert override["selection_scope"] == "project"
    assert override["source_project_id"] == "project-1"

    assert services.invoke(
        "lynewright.analytics.publisher", "publish", project_id="project-1"
    )["provider"] == project_id
    tenant_a.disable(project_id)
    with pytest.raises(IntegrationExternalError) as unavailable:
        services.invoke("lynewright.analytics.publisher", "publish", project_id="project-1")
    assert unavailable.value.category is IntegrationErrorCategory.PROVIDER_UNAVAILABLE
    assert calls == [global_id, project_id]

    tenant_b = IntegrationRegistry(store, tenant_id="tenant-b")
    tenant_b_selections = IntegrationProviderSelectionService(tenant_b)
    with pytest.raises(PlatformNotFoundError):
        tenant_b_selections.get(
            contract="lynewright.analytics.publisher", project_id="project-1"
        )
    with pytest.raises(PlatformNotFoundError):
        tenant_b_selections.get(
            contract="lynewright.analytics.publisher", project_id="missing"
        )
    db.close()
