from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.integration_capabilities import IntegrationAuthorizationService
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_oauth import OAuthConfiguration, OAuthService
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_sdk import IntegrationSDKFactory
from app.services.platform_capabilities import CapabilityActor, ClientAuthorizationService
from app.services.platform_services import CredentialService, PlatformOperationError, PlatformStore


class MockOAuthHTTP:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refresh = False

    def validate_public_url(self, url: str) -> dict[str, str]:
        return {"url": url}

    def request(self, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"url": url, **kwargs})
        body = str(kwargs["content"])
        if "grant_type=refresh_token" in body:
            self.refresh = True
            payload = {"access_token": "access-2", "refresh_token": "refresh-2", "token_type": "Bearer", "expires_in": 1800, "scope": "read write"}
        else:
            payload = {"access_token": "access-1", "refresh_token": "refresh-1", "token_type": "Bearer", "expires_in": 3600, "scope": "read write"}
        return {"status_code": 200, "content_type": "application/json", "headers": {}, "body": __import__("json").dumps(payload).encode("utf-8")}


@pytest.fixture()
def oauth_setup() -> tuple[OAuthService, IntegrationSDKFactory, ClientAuthorizationService, CredentialService, list[int], MockOAuthHTTP, PlatformStore]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    integrations = IntegrationRegistry(store)
    integrations.register({
        "schema_version": "1.0", "id": "lynewright.integration.oauth-mock", "name": "OAuth Mock", "version": "0.1.0",
        "type": "service", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0",
        "requested_capabilities": ["network:use", "credentials:use", "credentials:set"],
        "service": {"kind": "http", "health_check": "/health"}, "backend": "dist/backend.mjs",
    })
    integrations.enable("lynewright.integration.oauth-mock")
    authorization = IntegrationAuthorizationService(integrations)
    authorization.set_permissions("lynewright.integration.oauth-mock", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager())
    credentials = CredentialService(store)
    credentials.set({"name": "oauth.client-id", "kind": "oauth-client-id", "secret": "client-id"})
    credentials.set({"name": "oauth.client-secret", "kind": "oauth-client-secret", "secret": "client-secret"})
    http = MockOAuthHTTP()
    connections = ConnectionProfileService(store, registry=integrations, credentials=credentials, endpoint_validator=http.validate_public_url)
    connections.create("lynewright.integration.oauth-mock", connection_id="primary", name="Primary", endpoint_url="https://api.example.test")
    now = [1_000]
    oauth = OAuthService(store, connections, now=lambda: now[0])
    factory = IntegrationSDKFactory(store, authorization=authorization, connections=connections, http=http)
    clients = ClientAuthorizationService(store)
    yield oauth, factory, clients, credentials, now, http, store
    connection.close()


def config() -> OAuthConfiguration:
    return OAuthConfiguration(
        authorization_url="https://auth.example.test/authorize", token_url="https://auth.example.test/token",
        client_id_ref="oauth.client-id", client_secret_ref="oauth.client-secret", scopes=("read", "write"),
    )


def caller(clients: ClientAuthorizationService) -> CapabilityActor:
    provisioned = clients.create_client({"name": "OAuth caller", "permission_mode": "full"}, actor=CapabilityActor.platform_manager())
    return clients.authenticate(provisioned["access_token"])


def test_pkce_authorization_exchange_and_replay_protection(oauth_setup: tuple[OAuthService, IntegrationSDKFactory, ClientAuthorizationService, CredentialService, list[int], MockOAuthHTTP, PlatformStore]) -> None:
    oauth, factory, clients, credentials, _, http, store = oauth_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.oauth-mock", connection_id="primary")
    started = oauth.begin_authorization(sdk, config(), redirect_uri="https://dashboard.example.test/oauth/callback")
    query = parse_qs(urlsplit(str(started["authorization_url"])).query)
    assert query["state"] == [started["state"]]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_verifier" not in query

    with store.connection() as db:
        row = db.execute("SELECT state_hash,verifier_encrypted FROM platform_integration_oauth_states").fetchone()
    assert row["state_hash"] != started["state"]
    assert row["verifier_encrypted"].startswith("enc:v1:")

    completed = oauth.complete_callback(
        sdk, config(), state=str(started["state"]), code="authorization-code",
        access_credential_ref="oauth.access", refresh_credential_ref="oauth.refresh",
    )
    assert completed["access_credential_ref"] == "oauth.access"
    assert completed["refresh_credential_ref"] == "oauth.refresh"
    assert completed["expires_at_epoch"] == 4_600
    assert credentials.get_status("oauth.access")["configured"] is True
    assert "secret" not in credentials.get_status("oauth.access")
    assert "client_secret=client-secret" in str(http.calls[0]["content"])
    with pytest.raises(PlatformOperationError, match="already consumed"):
        oauth.complete_callback(sdk, config(), state=str(started["state"]), code="authorization-code", access_credential_ref="oauth.access")


def test_refresh_rotates_credentials_and_expired_state_is_rejected(oauth_setup: tuple[OAuthService, IntegrationSDKFactory, ClientAuthorizationService, CredentialService, list[int], MockOAuthHTTP, PlatformStore]) -> None:
    oauth, factory, clients, credentials, now, http, _ = oauth_setup
    sdk = factory.create(caller=caller(clients), integration_id="lynewright.integration.oauth-mock", connection_id="primary")
    started = oauth.begin_authorization(sdk, config(), redirect_uri="https://dashboard.example.test/oauth/callback")
    oauth.complete_callback(sdk, config(), state=str(started["state"]), code="code", access_credential_ref="oauth.access", refresh_credential_ref="oauth.refresh")
    now[0] = 2_000
    refreshed = oauth.refresh(sdk, config())
    assert refreshed["expires_at_epoch"] == 3_800
    assert http.refresh is True
    assert credentials.use("oauth.refresh", lambda value: {"value": value}) == {"value": "refresh-2"}

    expired = oauth.begin_authorization(sdk, config(), redirect_uri="https://dashboard.example.test/oauth/callback", state_ttl_seconds=60)
    now[0] += 61
    with pytest.raises(PlatformOperationError, match="expired"):
        oauth.complete_callback(sdk, config(), state=str(expired["state"]), code="late", access_credential_ref="oauth.access")
