from __future__ import annotations

import hashlib
import hmac
import sqlite3

import pytest

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_gateway import IntegrationGateway
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import CredentialService, PlatformOperationError, PlatformStore


def manifest(integration_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0", "id": integration_id, "name": integration_id,
        "version": "0.1.0", "type": "native", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0",
        "backend": "dist/backend.mjs",
    }


@pytest.fixture()
def gateway() -> tuple[IntegrationGateway, ContributionRegistry, list[dict[str, object]], list[int]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    integrations = IntegrationRegistry(store)
    integrations.register(manifest("lynewright.integration.inbound"))
    integrations.enable("lynewright.integration.inbound")
    credentials = CredentialService(store)
    credentials.set({"name": "inbound.secret", "kind": "token", "secret": "shared-secret"})
    connections = ConnectionProfileService(store, registry=integrations, credentials=credentials, endpoint_validator=lambda _: {})
    connections.create("lynewright.integration.inbound", connection_id="primary", name="Primary")
    contributions = ContributionRegistry(integrations, connections=connections)
    received: list[dict[str, object]] = []
    now = [1_000]
    result = IntegrationGateway(store, connections, contributions, now=lambda: now[0])
    yield result, contributions, received, now
    connection.close()


def test_hmac_webhook_validation_timestamp_and_replay_protection(gateway: tuple[IntegrationGateway, ContributionRegistry, list[dict[str, object]], list[int]]) -> None:
    service, contributions, received, now = gateway
    contributions.register(
        integration_id="lynewright.integration.inbound", connection_id="primary", local_id="webhooks.order", kind="webhook",
        handler=lambda message: received.append(message.json()),
        metadata={"verification": {"mode": "hmac_sha256", "credential_ref": "inbound.secret", "signature_header": "x-signature", "timestamp_header": "x-timestamp", "delivery_header": "x-delivery", "tolerance_seconds": 60}},
    )
    body = b'{"order":"one"}'
    signed = b"1000." + body
    signature = hmac.new(b"shared-secret", signed, hashlib.sha256).hexdigest()
    headers = {"X-Signature": f"sha256={signature}", "X-Timestamp": "1000", "X-Delivery": "delivery-1"}
    first = service.receive_webhook(integration_id="lynewright.integration.inbound", connection_id="primary", handler_id="webhooks.order", body=body, headers=headers)
    assert first == {"accepted": True, "duplicate": False, "delivery_id": "delivery-1"}
    assert received == [{"order": "one"}]
    assert service.receive_webhook(integration_id="lynewright.integration.inbound", connection_id="primary", handler_id="webhooks.order", body=body, headers=headers)["duplicate"] is True
    assert received == [{"order": "one"}]

    with pytest.raises(PlatformOperationError, match="timestamp"):
        service.receive_webhook(integration_id="lynewright.integration.inbound", connection_id="primary", handler_id="webhooks.order", body=body, headers={**headers, "X-Timestamp": str(now[0] - 61), "X-Delivery": "late"})
    with pytest.raises(PlatformOperationError, match="signature is invalid"):
        service.receive_webhook(integration_id="lynewright.integration.inbound", connection_id="primary", handler_id="webhooks.order", body=body, headers={**headers, "X-Signature": "bad", "X-Delivery": "bad"})


def test_callback_routes_are_kind_scoped_and_require_declared_verification(gateway: tuple[IntegrationGateway, ContributionRegistry, list[dict[str, object]], list[int]]) -> None:
    service, contributions, received, _ = gateway
    contributions.register(
        integration_id="lynewright.integration.inbound", connection_id="primary", local_id="callbacks.oauth", kind="callback",
        handler=lambda message: received.append({"callback": message.delivery_id}),
        metadata={"verification": {"mode": "shared_secret", "credential_ref": "inbound.secret", "signature_header": "x-callback-secret"}},
    )
    accepted = service.receive_callback(
        integration_id="lynewright.integration.inbound", connection_id="primary", handler_id="callbacks.oauth",
        body=b"callback", headers={"X-Callback-Secret": "shared-secret"},
    )
    assert accepted["accepted"] is True
    assert received[0]["callback"] == accepted["delivery_id"]
    with pytest.raises(PlatformOperationError, match="does not accept"):
        service.receive_webhook(integration_id="lynewright.integration.inbound", connection_id="primary", handler_id="callbacks.oauth", body=b"callback", headers={"X-Callback-Secret": "shared-secret"})

    contributions.register(
        integration_id="lynewright.integration.inbound", connection_id="primary", local_id="webhooks.unverified", kind="webhook", handler=lambda _: None,
    )
    with pytest.raises(PlatformOperationError, match="does not declare"):
        service.receive_webhook(integration_id="lynewright.integration.inbound", connection_id="primary", handler_id="webhooks.unverified", body=b"{}")
