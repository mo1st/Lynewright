from __future__ import annotations

import sqlite3

from app.services.integration_automation import IntegrationAutomationBridge
from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService
from app.services.platform_services import PlatformStore


def test_generic_job_callback_event_and_schedule_bridges() -> None:
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db); integrations = IntegrationRegistry(store); integration_id = "lynewright.integration.automation-mock"
    integrations.register({"schema_version":"1.0","id":integration_id,"name":"Automation Mock","version":"0.1.0","type":"service","lynewright_version":"^0.1.0","sdk_version":"^1.0.0","requested_capabilities":["jobs:create"],"service":{"kind":"http","health_check":"/health"},"backend":"dist/backend.mjs"})
    integrations.enable(integration_id); auth = IntegrationAuthorizationService(integrations); auth.set_permissions(integration_id, permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager())
    contributions = ContributionRegistry(integrations); clients = ClientAuthorizationService(store); created = clients.create_client({"name":"Automation","permission_mode":"full"}, actor=CapabilityActor.platform_manager()); caller = clients.authenticate(created["access_token"])
    sdk = IntegrationSDKFactory(store, authorization=auth).create(caller=caller, integration_id=integration_id)
    seen: list[str] = []; contributions.register(integration_id=integration_id, local_id="remote", kind="job", handler=lambda _sdk, _job, _payload: {"remote_id":"r1"}); contributions.register(integration_id=integration_id, local_id="events", kind="event", metadata={"pattern":"project.*"}, handler=lambda _sdk, event: seen.append(event["type"])); contributions.register(integration_id=integration_id, local_id="poll", kind="scheduled", metadata={"interval_seconds":60}, handler=lambda _sdk: seen.append("poll"))
    now=[0.0]; bridge=IntegrationAutomationBridge(contributions, IntegrationSDKFactory(store, authorization=auth), now=lambda: now[0])
    started=bridge.start_remote_operation(sdk,"lynewright.integration.automation-mock:remote",job_type="integration.remote",payload={"x":1}); job_id=started["job"]["id"]; assert started["remote"]=={"remote_id":"r1"}; assert bridge.apply_callback(sdk,job_id=job_id,status="progress",progress=50)["progress"]==50; assert bridge.apply_callback(sdk,job_id=job_id,status="completed",result={"ok":True})["status"]=="completed"
    assert bridge.dispatch_event(caller,event_type="project.created",payload={})["delivered"]==1; assert bridge.run_scheduled(caller)["ran"]==["lynewright.integration.automation-mock:poll"]; assert bridge.run_scheduled(caller)["ran"]==[]; now[0]=60; assert bridge.run_scheduled(caller)["ran"]; assert seen==["project.created","poll","poll"]
    db.close()


def test_schedule_checkpoint_survives_process_reconstruction() -> None:
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integration_id = "lynewright.integration.schedule-recovery"
    integrations = IntegrationRegistry(store)
    integrations.register({"schema_version":"1.0","id":integration_id,"name":"Schedule Recovery","version":"0.1.0","type":"service","lynewright_version":"^0.1.0","sdk_version":"^1.0.0","requested_capabilities":[],"service":{"kind":"http","health_check":"/health"},"backend":"dist/backend.mjs"})
    integrations.enable(integration_id)
    auth = IntegrationAuthorizationService(integrations)
    auth.set_permissions(integration_id, permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager())
    clients = ClientAuthorizationService(store)
    caller = clients.authenticate(clients.create_client({"name":"Schedule Recovery","permission_mode":"full"}, actor=CapabilityActor.platform_manager())["access_token"])
    factory = IntegrationSDKFactory(store, authorization=auth)
    seen: list[str] = []

    first_contributions = ContributionRegistry(integrations)
    first_contributions.register(integration_id=integration_id, local_id="poll", kind="scheduled", metadata={"interval_seconds":60}, handler=lambda _sdk: seen.append("first"))
    first_clock = [100.0]
    assert IntegrationAutomationBridge(first_contributions, factory, now=lambda: first_clock[0]).run_scheduled(caller)["ran"] == [f"{integration_id}:poll"]

    # A fresh contribution registry and bridge model the restarted process.
    restarted_contributions = ContributionRegistry(IntegrationRegistry(store))
    restarted_contributions.register(integration_id=integration_id, local_id="poll", kind="scheduled", metadata={"interval_seconds":60}, handler=lambda _sdk: seen.append("restarted"))
    restart_clock = [120.0]
    restarted = IntegrationAutomationBridge(restarted_contributions, factory, now=lambda: restart_clock[0])
    assert restarted.run_scheduled(caller)["ran"] == []
    restart_clock[0] = 160.0
    assert restarted.run_scheduled(caller)["ran"] == [f"{integration_id}:poll"]
    assert seen == ["first", "restarted"]
    db.close()
