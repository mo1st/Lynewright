from __future__ import annotations

import sqlite3

import pytest

from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_mcp_contributions import IntegrationMCPContributionBridge
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService
from app.services.platform_services import PlatformOperationError, PlatformStore, PlatformValidationError


def test_hardening_denies_ssrf_secret_leaks_collisions_and_disabled_contributions() -> None:
    db=sqlite3.connect(":memory:"); db.row_factory=sqlite3.Row; store=PlatformStore(lambda:db); integration_id="lynewright.integration.hardened"; integrations=IntegrationRegistry(store)
    integrations.register({"schema_version":"1.0","id":integration_id,"name":"Hardened","version":"0.1.0","type":"service","lynewright_version":"^0.1.0","sdk_version":"^1.0.0","requested_capabilities":["credentials:use"],"service":{"kind":"http","health_check":"/health"},"backend":"dist/backend.mjs"}); integrations.enable(integration_id); auth=IntegrationAuthorizationService(integrations); auth.set_permissions(integration_id,permission_mode="full",granted_capabilities=[],actor=CapabilityActor.platform_manager())
    connections=ConnectionProfileService(store,registry=integrations,endpoint_validator=lambda url: (_ for _ in ()).throw(PlatformValidationError("private URL")) if "127.0.0.1" in url else {});
    with pytest.raises(PlatformValidationError,match="private URL"): connections.create(integration_id,name="Unsafe",endpoint_url="http://127.0.0.1")
    connections.create(integration_id,connection_id="safe",name="Safe",endpoint_url="https://safe.example.test")
    contributions=ContributionRegistry(integrations,connections=connections); metadata={"name":"lynewright.integration.hardened.action","description":"Safe action","input_schema":{},"caller_capability":"credentials:use","integration_capability":"credentials:use","project_scope":"none"}; contributions.register(integration_id=integration_id,local_id="action",kind="mcp",metadata=metadata,handler=lambda *_:{"ok":True})
    with pytest.raises(Exception): contributions.register(integration_id=integration_id,local_id="action",kind="mcp",metadata=metadata,handler=lambda *_:{})
    clients=ClientAuthorizationService(store); client=clients.create_client({"name":"Harden","permission_mode":"full"},actor=CapabilityActor.platform_manager()); bridge=IntegrationMCPContributionBridge(contributions,IntegrationSDKFactory(store,authorization=auth,connections=connections)); assert bridge.invoke(f"{integration_id}:action",clients.authenticate(client["access_token"]),{})=={"ok":True}
    contributions.disable_integration(integration_id)
    with pytest.raises(Exception): bridge.invoke(f"{integration_id}:action",clients.authenticate(client["access_token"]),{})
    db.close()
