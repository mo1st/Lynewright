from __future__ import annotations

import sqlite3

from app.services.integration_automation import IntegrationAutomationBridge
from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_lifecycle import IntegrationLifecycleRuntime, IntegrationRuntimeDefinition
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService
from app.services.platform_services import JobService, PlatformStore


def _manifest(integration_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0", "id": integration_id, "name": "Active jobs", "version": "0.1.0",
        "type": "service", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0",
        "requested_capabilities": ["jobs:create"], "service": {"kind": "http", "health_check": "/health"},
        "backend": "dist/backend.mjs",
    }


def _setup() -> tuple[PlatformStore, IntegrationRegistry, ContributionRegistry, IntegrationLifecycleRuntime, IntegrationAutomationBridge, object, IntegrationSDKFactory]:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integration_id = "lynewright.integration.active-jobs"
    integrations = IntegrationRegistry(store)
    integrations.register(_manifest(integration_id))
    authorization = IntegrationAuthorizationService(integrations)
    authorization.set_permissions(integration_id, permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager())
    contributions = ContributionRegistry(integrations)
    runtime = IntegrationLifecycleRuntime(integrations, contributions)
    runtime.register_definition(IntegrationRuntimeDefinition(integration_id))
    runtime.enable(integration_id)
    clients = ClientAuthorizationService(store)
    client = clients.create_client({"name": "Active jobs", "permission_mode": "full"}, actor=CapabilityActor.platform_manager())
    caller = clients.authenticate(client["access_token"])
    factory = IntegrationSDKFactory(store, authorization=authorization)
    sdk = factory.create(caller=caller, integration_id=integration_id)
    return store, integrations, contributions, runtime, IntegrationAutomationBridge(contributions, factory), sdk, factory


def test_disable_applies_cancel_drain_and_detach_and_callbacks_are_idempotent() -> None:
    store, _integrations, contributions, runtime, bridge, sdk, _factory = _setup()
    integration_id = sdk.integration_id
    for local_id, policy in (("cancel", "cancel"), ("drain", "drain"), ("detach", "detach")):
        contributions.register(
            integration_id=integration_id,
            local_id=local_id,
            kind="job",
            metadata={"active_job_policy": policy},
            handler=lambda *_: {"started": True},
        )
    ids = {
        local_id: bridge.start_remote_operation(sdk, f"{integration_id}:{local_id}", job_type=f"test.{local_id}", payload={})["job"]["id"]
        for local_id in ("cancel", "drain", "detach")
    }

    runtime.disable(integration_id)
    jobs = JobService(store)
    assert jobs.get(ids["cancel"])["status"] == "cancelled"
    assert jobs.get(ids["drain"])["status"] == "running"
    assert jobs.get(ids["detach"])["status"] == "running"

    assert bridge.apply_callback(sdk, job_id=ids["cancel"], status="completed") == {
        "id": ids["cancel"], "ignored": True, "reason": "cancelled_or_terminal",
    }
    assert bridge.apply_callback(sdk, job_id=ids["drain"], status="completed", result={"ok": True})["status"] == "completed"
    assert bridge.apply_callback(sdk, job_id=ids["detach"], status="completed", result={"ok": True})["status"] == "completed"
    # Duplicate remote completion and any callback after local cancellation are safe no-ops.
    assert bridge.apply_callback(sdk, job_id=ids["drain"], status="completed")["ignored"] is True


def test_cancelled_remote_job_retries_after_integration_is_reenabled() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integration_id = "lynewright.integration.active-job-retry"
    integrations = IntegrationRegistry(store)
    integrations.register(_manifest(integration_id))
    authorization = IntegrationAuthorizationService(integrations)
    authorization.set_permissions(integration_id, permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager())
    contributions = ContributionRegistry(integrations)
    calls: list[str] = []

    def activate(context):
        context.contribute(
            local_id="remote", kind="job", metadata={"active_job_policy": "cancel"},
            handler=lambda _sdk, _job, _payload: calls.append("remote") or {"started": True},
        )

    runtime = IntegrationLifecycleRuntime(integrations, contributions)
    runtime.register_definition(IntegrationRuntimeDefinition(integration_id, activate=activate))
    runtime.enable(integration_id)
    clients = ClientAuthorizationService(store)
    caller = clients.authenticate(clients.create_client({"name": "Retry", "permission_mode": "full"}, actor=CapabilityActor.platform_manager())["access_token"])
    factory = IntegrationSDKFactory(store, authorization=authorization)
    bridge = IntegrationAutomationBridge(contributions, factory)
    first_sdk = factory.create(caller=caller, integration_id=integration_id)
    job_id = bridge.start_remote_operation(first_sdk, f"{integration_id}:remote", job_type="test.retry", payload={"attempt": 1})["job"]["id"]

    runtime.disable(integration_id)
    runtime.enable(integration_id)
    recovered_sdk = factory.create(caller=caller, integration_id=integration_id)
    retried = bridge.retry_remote_operation(recovered_sdk, job_id)
    assert retried["remote"] == {"started": True}
    assert retried["job"]["status"] == "running"
    assert calls == ["remote", "remote"]


def test_reload_applies_the_same_active_job_policy() -> None:
    store, _integrations, contributions, runtime, bridge, sdk, _factory = _setup()
    integration_id = sdk.integration_id
    contributions.register(
        integration_id=integration_id, local_id="reload", kind="job",
        metadata={"active_job_policy": "cancel"}, handler=lambda *_: {"started": True},
    )
    job_id = bridge.start_remote_operation(sdk, f"{integration_id}:reload", job_type="test.reload", payload={})["job"]["id"]

    runtime.reload(integration_id)
    assert JobService(store).get(job_id)["status"] == "cancelled"
    assert bridge.apply_callback(sdk, job_id=job_id, status="completed")["ignored"] is True
