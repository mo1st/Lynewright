from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_errors import IntegrationErrorCategory, IntegrationExternalError, normalize_external_error
from app.services.integration_http import IntegrationHTTPAdapter
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService
from app.services.platform_services import CredentialService, PlatformOperationError, PlatformStore, PlatformValidationError


class FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_response: dict[str, Any] | Exception = {
            "status_code": 200, "content_type": "application/json",
            "headers": {"X-RateLimit-Remaining": "9", "Authorization": "never-return"}, "body": b'{"ok":true}',
        }

    def validate_public_url(self, url: str) -> dict[str, Any]:
        if "localhost" in url:
            raise PlatformValidationError("URL resolves to a private address.")
        return {"hostname": "api.example.test"}

    def request(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.next_response, Exception):
            raise self.next_response
        return self.next_response


@pytest.fixture()
def adapter_setup() -> tuple[IntegrationHTTPAdapter, IntegrationSDKFactory, ClientAuthorizationService, FakeHTTP]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    integrations = IntegrationRegistry(store)
    integrations.register({
        "schema_version": "1.0", "id": "lynewright.integration.http-mock", "name": "HTTP Mock", "version": "0.1.0",
        "type": "service", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0",
        "requested_capabilities": ["network:use", "credentials:use"],
        "service": {"kind": "http", "health_check": "/health"}, "backend": "dist/backend.mjs",
    })
    integrations.enable("lynewright.integration.http-mock")
    authorization = IntegrationAuthorizationService(integrations)
    authorization.set_permissions("lynewright.integration.http-mock", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager())
    fake = FakeHTTP()
    credentials = CredentialService(store)
    credentials.set({"name": "http.primary", "kind": "token", "secret": "credential-secret"})
    connections = ConnectionProfileService(store, registry=integrations, credentials=credentials, endpoint_validator=fake.validate_public_url)
    connections.create(
        "lynewright.integration.http-mock", connection_id="primary", name="Primary", endpoint_url="https://api.example.test",
        credential_refs=["http.primary"],
    )
    clients = ClientAuthorizationService(store)
    factory = IntegrationSDKFactory(store, authorization=authorization, connections=connections, http=fake)
    yield IntegrationHTTPAdapter(connections), factory, clients, fake
    connection.close()


def caller(clients: ClientAuthorizationService) -> CapabilityActor:
    provisioned = clients.create_client({"name": "HTTP caller", "permission_mode": "full"}, actor=CapabilityActor.platform_manager())
    return clients.authenticate(provisioned["access_token"])


def test_http_adapter_uses_connection_endpoint_safe_sdk_and_credential_binding(adapter_setup: tuple[IntegrationHTTPAdapter, IntegrationSDKFactory, ClientAuthorizationService, FakeHTTP]) -> None:
    adapter, factory, clients, fake = adapter_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.http-mock", connection_id="primary")

    response = adapter.request(sdk, method="GET", path="/v1/items", auth="bearer")
    assert fake.calls[0]["url"] == "https://api.example.test/v1/items"
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer credential-secret"
    assert "authorization" not in response["headers"]
    assert response["rate_limit"] == {"x-ratelimit-remaining": "9"}
    assert adapter.request_json(sdk, method="GET", path="/v1/items") == {"ok": True}


def test_http_adapter_blocks_origin_replacement_and_invalid_responses(adapter_setup: tuple[IntegrationHTTPAdapter, IntegrationSDKFactory, ClientAuthorizationService, FakeHTTP]) -> None:
    adapter, factory, clients, fake = adapter_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.http-mock", connection_id="primary")
    with pytest.raises(PlatformValidationError, match="cannot replace"):
        adapter.request(sdk, method="GET", path="//localhost/private")
    assert fake.calls == []

    fake.next_response = {"status_code": 200, "content_type": "application/json", "headers": {}, "body": b"not-json"}
    with pytest.raises(IntegrationExternalError) as malformed:
        adapter.request_json(sdk, method="GET", path="/items")
    assert malformed.value.category is IntegrationErrorCategory.INVALID_RESPONSE


@pytest.mark.parametrize(("status", "category"), [
    (401, IntegrationErrorCategory.AUTHENTICATION_FAILED),
    (403, IntegrationErrorCategory.AUTHORIZATION_FAILED),
    (429, IntegrationErrorCategory.RATE_LIMITED),
    (404, IntegrationErrorCategory.NOT_FOUND),
    (409, IntegrationErrorCategory.CONFLICT),
    (503, IntegrationErrorCategory.PROVIDER_UNAVAILABLE),
])
def test_http_adapter_normalizes_provider_statuses(adapter_setup: tuple[IntegrationHTTPAdapter, IntegrationSDKFactory, ClientAuthorizationService, FakeHTTP], status: int, category: IntegrationErrorCategory) -> None:
    adapter, factory, clients, fake = adapter_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.http-mock", connection_id="primary")
    fake.next_response = {"status_code": status, "content_type": "application/json", "headers": {"Authorization": "leak-me", "Retry-After": "30"}, "body": b'{"token":"leak-me"}'}

    with pytest.raises(IntegrationExternalError) as failure:
        adapter.require_success(adapter.request(sdk, method="GET", path="/items"), provider="example")
    assert failure.value.category is category
    assert "leak-me" not in repr(failure.value.as_dict())
    assert "authorization" not in failure.value.diagnostic.get("headers", {})


def test_http_adapter_normalizes_timeout_and_redacts_unknown_external_diagnostics(adapter_setup: tuple[IntegrationHTTPAdapter, IntegrationSDKFactory, ClientAuthorizationService, FakeHTTP]) -> None:
    adapter, factory, clients, fake = adapter_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.http-mock", connection_id="primary")
    fake.next_response = TimeoutError("timeout token=provider-secret")

    with pytest.raises(IntegrationExternalError) as timeout:
        adapter.request(sdk, method="GET", path="/items")
    assert timeout.value.category is IntegrationErrorCategory.TIMEOUT
    assert "provider-secret" not in repr(timeout.value.as_dict())

    unknown = normalize_external_error(RuntimeError("upstream token=provider-secret"), provider="example", secrets=["provider-secret"])
    assert unknown.category is IntegrationErrorCategory.UNKNOWN_EXTERNAL_ERROR
    assert "provider-secret" not in repr(unknown.as_dict())


def test_http_health_handles_auth_failure_timeout_and_recovery(adapter_setup: tuple[IntegrationHTTPAdapter, IntegrationSDKFactory, ClientAuthorizationService, FakeHTTP]) -> None:
    adapter, factory, clients, fake = adapter_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.http-mock", connection_id="primary")

    fake.next_response = {"status_code": 401, "content_type": "application/json", "headers": {}, "body": b"{}"}
    assert adapter.health(sdk)["state"] == "DEGRADED"

    fake.next_response = PlatformOperationError("timeout")
    unavailable = adapter.health(sdk)
    assert unavailable["state"] == "UNAVAILABLE"
    assert unavailable["connection"]["health"]["state"] == "UNAVAILABLE"

    fake.next_response = {"status_code": 200, "content_type": "application/json", "headers": {}, "body": b"{}"}
    recovered = adapter.health(sdk)
    assert recovered["state"] == "HEALTHY"
    assert recovered["connection"]["health"]["state"] == "HEALTHY"
