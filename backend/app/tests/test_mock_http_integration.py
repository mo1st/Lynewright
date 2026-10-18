"""End-to-end generic HTTP adapter fixture; deliberately not a real provider."""
from __future__ import annotations

import sqlite3
from typing import Any

from app.services.integration_automation import IntegrationAutomationBridge
from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_gateway import IntegrationGateway
from app.services.integration_http import IntegrationHTTPAdapter
from app.services.integration_media_transfer import IntegrationMediaTransferService
from app.services.integration_mcp_contributions import IntegrationMCPContributionBridge
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService
from app.services.platform_services import CredentialService, PlatformStore


class MockHTTP:
    def validate_public_url(self, url: str) -> dict[str, str]: return {"url": url}
    def get(self, url: str, **_kwargs: Any) -> dict[str, Any]: return {"status_code":200,"headers":{},"content_type":"application/octet-stream","body":b"mock-media"}
    def request(self, url: str, **_kwargs: Any) -> dict[str, Any]: return {"status_code":200,"headers":{},"content_type":"application/json","body":b'{"ok":true}'}


def test_mock_http_integration_exercises_generic_host_surface() -> None:
    db=sqlite3.connect(":memory:"); db.row_factory=sqlite3.Row; store=PlatformStore(lambda:db); integration_id="lynewright.integration.mock-http"; http=MockHTTP(); integrations=IntegrationRegistry(store)
    integrations.register({"schema_version":"1.0","id":integration_id,"name":"Mock HTTP","version":"0.1.0","type":"service","lynewright_version":"^0.1.0","sdk_version":"^1.0.0","requested_capabilities":["network:use","credentials:use","jobs:create","events:publish","settings:read","settings:write","mappings:read","mappings:write","sync:read","sync:write","media:read","media:create"],"service":{"kind":"http","health_check":"/health"},"backend":"dist/backend.mjs"}); integrations.enable(integration_id)
    auth=IntegrationAuthorizationService(integrations); auth.set_permissions(integration_id,permission_mode="full",granted_capabilities=[],actor=CapabilityActor.platform_manager()); credentials=CredentialService(store); credentials.set({"name":"mock.token","kind":"token","secret":"test-token"}); connections=ConnectionProfileService(store,registry=integrations,credentials=credentials,endpoint_validator=http.validate_public_url); connections.create(integration_id,connection_id="primary",name="Primary",endpoint_url="https://mock.example.test",credential_refs=["mock.token"],configuration={"mcp":{"approved_tools":[]}})
    contributions=ContributionRegistry(integrations,connections=connections); received=[]; contributions.register(integration_id=integration_id,connection_id="primary",local_id="hook",kind="webhook",metadata={"verification":{"mode":"none"}},handler=lambda message:received.append(message.json())); contributions.register(integration_id=integration_id,local_id="domain",kind="mcp",metadata={"name":"lynewright.integration.mock.publish","description":"Mock domain operation","input_schema":{},"caller_capability":"jobs:create","integration_capability":"jobs:create","project_scope":"none"},handler=lambda _sdk,_args:{"ok":True}); contributions.register(integration_id=integration_id,local_id="job",kind="job",handler=lambda *_:{"remote":"mock"})
    clients=ClientAuthorizationService(store); created=clients.create_client({"name":"Mock caller","permission_mode":"full"},actor=CapabilityActor.platform_manager()); caller=clients.authenticate(created["access_token"]); factory=IntegrationSDKFactory(store,authorization=auth,connections=connections,http=http); sdk=factory.create(caller=caller,integration_id=integration_id,connection_id="primary")
    assert IntegrationHTTPAdapter(connections).request_json(sdk,method="GET",path="/items",auth="bearer")=={"ok":True}; sdk.settings.set("mock.enabled",True); sdk.mappings.put(external_type="mock.item",external_id="1",lynewright_type="media",lynewright_id="x"); sdk.sync_state.checkpoint(cursor="c1",etag="e1",revision="1"); media=IntegrationMediaTransferService().download_to_media(sdk,url="https://example.com/media",name="mock.bin"); assert media["metadata"]["bytes"]==10
    job=IntegrationAutomationBridge(contributions,factory).start_remote_operation(sdk,f"{integration_id}:job",job_type="mock.remote",payload={}); assert job["remote"]=={"remote":"mock"}; assert IntegrationMCPContributionBridge(contributions,factory).invoke(f"{integration_id}:domain",caller,{})=={"ok":True}; assert IntegrationGateway(store,connections,contributions).receive("webhook",integration_id=integration_id,connection_id="primary",handler_id="hook",body=b'{"event":"ok"}')["accepted"]; assert received==[{"event":"ok"}]
    contributions.disable_integration(integration_id); assert contributions.list(integration_id=integration_id)==[]; db.close()
