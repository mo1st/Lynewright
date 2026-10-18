from __future__ import annotations

import base64
import hashlib
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

import app.routers.mcp_oauth as oauth_router
from app.models.mcp_oauth import MCPOAuthDecision
from app.routers.mcp_oauth import router
from app.services.mcp_oauth import MCPOAuthService
from app.services.platform_capabilities import PUBLIC_CAPABILITIES
from app.services.platform_services import PlatformStore


@pytest.fixture()
def router_setup(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path / "router.sqlite3", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL, role TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )"""
    )
    db.execute("INSERT INTO users VALUES (1,'admin@example.test','admin',1)")
    db.execute("INSERT INTO users VALUES (2,'member@example.test','member',1)")
    db.commit()
    service = MCPOAuthService(PlatformStore(lambda: db), issuer_url="https://app.example.test/", resource_url="https://app.example.test/mcp")
    info = OAuthClientInformationFull(
        client_id="router-test-client", client_name="Router MCP Client",
        redirect_uris=[AnyUrl("https://router-client.example.test/callback")],
        token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
        response_types=["code"], scope=" ".join(sorted(PUBLIC_CAPABILITIES)),
    )
    service.register_client(info)
    client = service.get_client("router-test-client")
    assert client is not None
    verifier = "router-test-verifier-value-long-enough-to-pass-validation"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    request_id = service.create_authorization_request(
        client,
        AuthorizationParams(
            state="keep-this-state", scopes=["system:read"], code_challenge=challenge,
            redirect_uri=AnyUrl("https://router-client.example.test/callback"),
            redirect_uri_provided_explicitly=True, resource="https://app.example.test/mcp",
        ),
    )
    monkeypatch.setattr(oauth_router, "_service", lambda: service)
    monkeypatch.setattr(oauth_router, "log_event", lambda **_kwargs: None)
    monkeypatch.setattr(
        oauth_router, "resolve_session",
        lambda token: {
            "admin-session": {"id": 1, "email": "admin@example.test", "role": "admin", "is_active": True},
            "member-session": {"id": 2, "email": "member@example.test", "role": "member", "is_active": True},
            "synthetic-session": {"id": 0, "email": "system", "role": "admin", "is_active": True, "synthetic": True},
        }.get(token),
    )
    app = FastAPI()
    app.include_router(router)
    yield TestClient(app), db, service, request_id
    db.close()


def test_consent_routes_require_real_admin_and_never_return_secrets(router_setup) -> None:
    client, _db, service, request_id = router_setup
    assert client.get(f"/api/mcp-oauth/requests/{request_id}").status_code == 401
    assert client.get(
        f"/api/mcp-oauth/requests/{request_id}",
        headers={"Authorization": "Bearer member-session"},
    ).status_code == 403
    assert client.get(
        f"/api/mcp-oauth/requests/{request_id}",
        headers={"Authorization": "Bearer synthetic-session"},
    ).status_code == 401

    headers = {"Authorization": "Bearer admin-session"}
    detail = client.get(f"/api/mcp-oauth/requests/{request_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["application_name"] == "Router MCP Client"
    assert detail.json()["default_scopes"] == ["system:read"]
    assert "client_id" not in detail.text

    response = client.post(
        f"/api/mcp-oauth/requests/{request_id}/decision",
        headers=headers,
        json={"approved": True, "scopes": ["system:read"], "confirm_writes": False},
    )
    assert response.status_code == 200
    location = response.json()["redirect_to"]
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["keep-this-state"]
    assert query["iss"] == ["https://app.example.test/"]

    repeated = client.post(
        f"/api/mcp-oauth/requests/{request_id}/decision",
        headers=headers,
        json={"approved": True, "scopes": ["system:read"]},
    )
    assert repeated.status_code == 409

    connections = client.get("/api/mcp-oauth/connections", headers=headers)
    assert connections.status_code == 200
    assert len(connections.json()["connections"]) == 1
    assert "qxo_" not in connections.text
    assert "qxr_" not in connections.text
    assert "token" not in connections.text.lower()
    grant_id = connections.json()["connections"][0]["id"]
    revoked = client.post(f"/api/mcp-oauth/connections/{grant_id}/revoke", headers=headers)
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert service.list_connections(1)[0]["revoked_at"] is not None


def test_consent_routes_fail_closed_when_oauth_disabled(router_setup, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _db, _service, request_id = router_setup
    monkeypatch.setattr(oauth_router, "mcp_oauth_enabled", lambda: False)
    response = client.get(
        f"/api/mcp-oauth/requests/{request_id}",
        headers={"Authorization": "Bearer admin-session"},
    )
    assert response.status_code == 404
