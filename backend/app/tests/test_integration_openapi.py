from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_errors import IntegrationErrorCategory, IntegrationExternalError
from app.services.integration_http import IntegrationHTTPAdapter
from app.services.integration_openapi import IntegrationOpenAPIClient
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService
from app.services.platform_services import CredentialService, PlatformStore, PlatformValidationError


DOCUMENT = """
openapi: 3.0.3
servers:
  - url: https://untrusted.example/v1
components:
  securitySchemes:
    bearerAuth: {type: http, scheme: bearer}
    basicAuth: {type: http, scheme: basic}
    keyAuth: {type: apiKey, in: header, name: X-Client-Key}
  schemas:
    Resource:
      type: object
      required: [id, name]
      properties:
        id: {type: string}
        name: {type: string}
paths:
  /resources/{resource_id}:
    get:
      operationId: getResource
      security: [{bearerAuth: []}]
      parameters:
        - {name: resource_id, in: path, required: true, schema: {type: string}}
        - {name: limit, in: query, required: true, schema: {type: integer}}
        - {name: X-Trace, in: header, required: true, schema: {type: string}}
      responses:
        '200':
          content: {application/json: {schema: {$ref: '#/components/schemas/Resource'}}}
  /resources:
    post:
      operationId: createResource
      security: [{keyAuth: []}]
      requestBody:
        required: true
        content: {application/json: {schema: {$ref: '#/components/schemas/Resource'}}}
      responses:
        '200':
          content: {application/json: {schema: {$ref: '#/components/schemas/Resource'}}}
  /basic:
    get:
      operationId: basicHealth
      security: [{basicAuth: []}]
      responses:
        '200': {content: {application/json: {schema: {type: object}}}}
"""


class FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.body: Any = {"id": "resource-1", "name": "Resource"}
        self.status = 200

    def validate_public_url(self, _: str) -> dict[str, str]:
        return {"hostname": "api.safe.test"}

    def request(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"url": url, **kwargs})
        return {"status_code": self.status, "content_type": "application/json", "headers": {"Authorization": "do-not-return"}, "body": json.dumps(self.body).encode()}


@pytest.fixture()
def openapi() -> tuple[IntegrationOpenAPIClient, object, FakeHTTP]:
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db); integration_id = "lynewright.integration.openapi-test"
    integrations = IntegrationRegistry(store)
    integrations.register({"schema_version":"1.0","id":integration_id,"name":"OpenAPI test","version":"0.1.0","type":"service","lynewright_version":"^0.1.0","sdk_version":"^1.0.0","requested_capabilities":["network:use","credentials:use"],"service":{"kind":"http","health_check":"/health"},"backend":"dist/backend.mjs"})
    integrations.enable(integration_id)
    authorization = IntegrationAuthorizationService(integrations); authorization.set_permissions(integration_id, permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager())
    fake = FakeHTTP(); credentials = CredentialService(store)
    for name, secret in (("bearer", "bearer-secret"), ("key", "key-secret"), ("basic", "encoded-basic"), ("raw", "Token raw-secret")):
        credentials.set({"name": name, "kind": "token", "secret": secret})
    connections = ConnectionProfileService(store, registry=integrations, credentials=credentials, endpoint_validator=fake.validate_public_url)
    connections.create(integration_id, connection_id="primary", name="OpenAPI", endpoint_url="https://api.safe.test/base", credential_refs=["bearer"])
    clients = ClientAuthorizationService(store); client = clients.create_client({"name":"OpenAPI caller","permission_mode":"full"}, actor=CapabilityActor.platform_manager())
    sdk = IntegrationSDKFactory(store, authorization=authorization, connections=connections, http=fake).create(caller=clients.authenticate(client["access_token"]), integration_id=integration_id, connection_id="primary")
    yield IntegrationOpenAPIClient(DOCUMENT, IntegrationHTTPAdapter(connections)), sdk, fake
    db.close()


def test_openapi_executes_bound_safe_http_operations_and_supported_auth(openapi: tuple[IntegrationOpenAPIClient, object, FakeHTTP]) -> None:
    client, sdk, fake = openapi
    assert client.execute(sdk, "getResource", {"path":{"resource_id":"a/b"}, "query":{"limit":2}, "headers":{"X-Trace":"trace-1"}}) == {"id":"resource-1","name":"Resource"}
    get = fake.calls[-1]
    assert get["url"] == "https://api.safe.test/base/resources/a%2Fb"  # Document servers cannot replace the approved connection.
    assert get["params"] == {"limit":"2"} and get["headers"] == {"X-Trace":"trace-1", "Authorization":"Bearer bearer-secret"}

    client.execute(sdk, "createResource", {"body":{"id":"resource-2","name":"Two"}}, credential_ref="key")
    assert fake.calls[-1]["headers"] == {"X-Client-Key":"key-secret"}
    client.execute(sdk, "basicHealth", {}, credential_ref="basic")
    assert fake.calls[-1]["headers"] == {"Authorization":"Basic encoded-basic"}
    client.execute(sdk, "basicHealth", {}, auth="authorization_header", credential_ref="raw")
    assert fake.calls[-1]["headers"] == {"Authorization":"Token raw-secret"}


def test_openapi_validates_arguments_and_normalizes_provider_responses(openapi: tuple[IntegrationOpenAPIClient, object, FakeHTTP]) -> None:
    client, sdk, fake = openapi
    with pytest.raises(PlatformValidationError, match="resource_id"):
        client.execute(sdk, "getResource", {"query":{"limit":2}, "headers":{"X-Trace":"trace"}})
    assert fake.calls == []

    fake.body = {"id": "resource-1"}
    with pytest.raises(IntegrationExternalError) as invalid_response:
        client.execute(sdk, "getResource", {"path":{"resource_id":"a"}, "query":{"limit":2}, "headers":{"X-Trace":"trace"}})
    assert invalid_response.value.category is IntegrationErrorCategory.INVALID_RESPONSE

    fake.status = 429
    with pytest.raises(IntegrationExternalError) as rate_limited:
        client.execute(sdk, "getResource", {"path":{"resource_id":"a"}, "query":{"limit":2}, "headers":{"X-Trace":"trace"}})
    assert rate_limited.value.category is IntegrationErrorCategory.RATE_LIMITED


def test_openapi_rejects_unsupported_documents_and_sensitive_manual_headers(openapi: tuple[IntegrationOpenAPIClient, object, FakeHTTP]) -> None:
    client, sdk, _ = openapi
    with pytest.raises(PlatformValidationError, match="sensitive"):
        client.execute(sdk, "getResource", {"path":{"resource_id":"a"}, "query":{"limit":2}, "headers":{"Authorization":"bad","X-Trace":"trace"}})
    with pytest.raises(PlatformValidationError, match="OpenAPI 3"):
        IntegrationOpenAPIClient({"openapi":"2.0.0", "paths":{}}, client.http)
