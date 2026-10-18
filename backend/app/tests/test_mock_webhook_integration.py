"""A webhook-only mock proves the host is not coupled to outbound HTTP adapters."""
from __future__ import annotations

import hashlib
import hmac
import sqlite3

from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_gateway import IntegrationGateway
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import CredentialService, PlatformStore


def test_webhook_only_mock_integration_handles_verified_events_and_disable_cleanup() -> None:
    db=sqlite3.connect(":memory:"); db.row_factory=sqlite3.Row; store=PlatformStore(lambda:db); integration_id="lynewright.integration.mock-webhook"; integrations=IntegrationRegistry(store)
    integrations.register({"schema_version":"1.0","id":integration_id,"name":"Mock Webhook","version":"0.1.0","type":"service","lynewright_version":"^0.1.0","sdk_version":"^1.0.0","requested_capabilities":[],"service":{"kind":"http","health_check":"/health"},"backend":"dist/backend.mjs"}); integrations.enable(integration_id); auth=IntegrationAuthorizationService(integrations); auth.set_permissions(integration_id,permission_mode="full",granted_capabilities=[],actor=CapabilityActor.platform_manager())
    credentials=CredentialService(store); credentials.set({"name":"webhook.secret","kind":"webhook","secret":"webhook-secret"}); connections=ConnectionProfileService(store,registry=integrations,credentials=credentials,endpoint_validator=lambda _url:{}); connections.create(integration_id,connection_id="inbound",name="Inbound",endpoint_url="https://unused.example.test",credential_refs=["webhook.secret"])
    contributions=ContributionRegistry(integrations,connections=connections); messages=[]; contributions.register(integration_id=integration_id,connection_id="inbound",local_id="delivery",kind="webhook",metadata={"verification":{"mode":"hmac_sha256","credential_ref":"webhook.secret","signature_header":"x-signature"},"delivery_header":"x-delivery-id"},handler=lambda message:messages.append(message.json()))
    body=b'{"kind":"created"}'; signature=hmac.new(b"webhook-secret",body,hashlib.sha256).hexdigest(); gateway=IntegrationGateway(store,connections,contributions)
    first=gateway.receive("webhook",integration_id=integration_id,connection_id="inbound",handler_id="delivery",body=body,headers={"X-Signature":signature,"X-Delivery-Id":"delivery-1"}); duplicate=gateway.receive("webhook",integration_id=integration_id,connection_id="inbound",handler_id="delivery",body=body,headers={"X-Signature":signature,"X-Delivery-Id":"delivery-1"})
    assert first["accepted"] and not first["duplicate"] and duplicate["duplicate"] and messages==[{"kind":"created"}]
    contributions.disable_integration(integration_id); assert contributions.list(integration_id=integration_id)==[]; db.close()
