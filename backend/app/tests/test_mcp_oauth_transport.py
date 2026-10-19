from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from app.models.mcp_oauth import MCPOAuthDecision
from app.mcp_server import create_mcp_http_app, create_mcp_server
from app.services.mcp_oauth import MCPAuthorizationProvider
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformStore
from app.services.tool_models import CoreToolDefinition


TEST_INFO_TOOL = "lynewright.test.info"


def _register_test_info_tool(mcp, registry) -> None:
    registry.register_tool(CoreToolDefinition(
        name=TEST_INFO_TOOL, capability="system:read",
        handler=lambda _actor, _arguments: {"name": "Lynewright"},
        description="Return the test host identity.", read_only=True,
        risk_class="READ_ONLY", input_schema={"type": "object", "properties": {}},
    ))

    async def test_info():
        return await registry.invoke_current(TEST_INFO_TOOL, {})

    mcp.tool(name=TEST_INFO_TOOL, description="Return the test host identity.")(test_info)

    write_name = "lynewright.test.write"
    registry.register_tool(CoreToolDefinition(
        name=write_name, capability="mcp:client:use",
        handler=lambda _actor, arguments: {"accepted": arguments.get("value")},
        description="Accept a test write.", input_schema={
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        },
    ))

    async def test_write(value: str):
        return await registry.invoke_current(write_name, {"value": value})

    mcp.tool(name=write_name, description="Accept a test write.")(test_write)

    @mcp.resource("lynewright://test/summary")
    def test_summary() -> str:
        return "Lynewright test summary"


class AllowRateLimiter:
    def consume(self, **_kwargs):
        return True


class DenyRateLimiter:
    def consume(self, **_kwargs):
        return False


def _authorization_provider(registry):
    return MCPAuthorizationProvider(registry.oauth_service, registry.clients)


def _tool_result(response):
    result = response.json()["result"]
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                value = json.loads(block["text"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
    return {}


def _create_transport(monkeypatch, *, oauth_enabled: bool = True):
    issuer = "https://app.example.test"
    monkeypatch.setenv("LYNEWRIGHT_PUBLIC_URL", issuer)
    monkeypatch.setenv("LYNEWRIGHT_MCP_OAUTH_ENABLED", "true" if oauth_enabled else "false")
    monkeypatch.setattr("app.services.rate_limiter.get_rate_limiter", lambda: AllowRateLimiter())
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL, role TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )"""
    )
    connection.executemany(
        "INSERT INTO users (id,email,role,is_active) VALUES (?,?,?,1)",
        [(1, "admin@example.test", "admin"), (2, "other@example.test", "admin")],
    )
    connection.execute(
        """CREATE TABLE tenant_projects (
            project_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE (tenant_id, name)
        )"""
    )
    connection.commit()
    store = PlatformStore(lambda: connection)
    mcp, registry = create_mcp_server(store)
    _register_test_info_tool(mcp, registry)
    app = create_mcp_http_app(mcp, oauth_provider=_authorization_provider(registry))
    return issuer, connection, store, registry, app


def _register_public_client(http, name: str) -> tuple[str, str]:
    suffix = hashlib.sha256(name.encode()).hexdigest()[:8]
    redirect_uri = f"https://{suffix}.client.example.test/callback"
    response = http.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "system:read",
        },
    )
    assert response.status_code == 201
    return response.json()["client_id"], redirect_uri


def _issue_oauth_tokens(http, registry, issuer: str, name: str, *, admin_user_id: int = 1):
    client_id, redirect_uri = _register_public_client(http, name)
    verifier = "correct-horse-battery-staple-012345678901234567890123456789"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    authorization = http.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": "review-state",
            "scope": "system:read",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": issuer + "/mcp",
        },
        follow_redirects=False,
    )
    assert authorization.status_code == 302
    consent_url = urlsplit(authorization.headers["location"])
    request_id = parse_qs(consent_url.query)["request"][0]
    callback = registry.oauth_service.decide(
        request_id,
        admin_user_id,
        MCPOAuthDecision(approved=True, scopes=["system:read"]),
    )
    code = parse_qs(urlsplit(callback).query)["code"][0]
    token_response = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": issuer + "/mcp",
        },
    )
    assert token_response.status_code == 200
    return client_id, token_response.json()


def _mcp_initialize(http, token: str):
    return http.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "review-test", "version": "1"},
            },
        },
    )


def _mcp_tool(http, token: str, name: str, arguments: dict | None = None):
    initialized = _mcp_initialize(http, token)
    if initialized.status_code != 200:
        return initialized
    return http.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )


def test_oauth_discovery_registration_pkce_and_mcp_round_trip(monkeypatch) -> None:
    issuer = "https://app.example.test"
    resource = issuer + "/mcp"
    monkeypatch.setenv("LYNEWRIGHT_PUBLIC_URL", issuer)
    monkeypatch.setenv("LYNEWRIGHT_MCP_OAUTH_ENABLED", "true")
    monkeypatch.setattr("app.services.rate_limiter.get_rate_limiter", lambda: AllowRateLimiter())

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL, role TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )"""
    )
    connection.execute("INSERT INTO users (id,email,role,is_active) VALUES (1,'admin@example.test','admin',1)")
    connection.commit()
    store = PlatformStore(lambda: connection)
    mcp, registry = create_mcp_server(store)
    _register_test_info_tool(mcp, registry)
    app = create_mcp_http_app(mcp, oauth_provider=_authorization_provider(registry))
    redirect_uri = "https://generic-client.example.test/callback"
    verifier = "correct-horse-battery-staple-012345678901234567890123456789"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    with TestClient(app, base_url=issuer) as http:
        unauthenticated = http.get("/mcp")
        assert unauthenticated.status_code == 401
        challenge_header = unauthenticated.headers["www-authenticate"]
        assert 'resource_metadata="https://app.example.test/.well-known/oauth-protected-resource/mcp"' in challenge_header

        protected = http.get("/.well-known/oauth-protected-resource/mcp")
        authorization_server = http.get("/.well-known/oauth-authorization-server")
        assert protected.status_code == authorization_server.status_code == 200
        assert protected.headers["content-type"].startswith("application/json")
        assert protected.json()["resource"] == resource
        assert protected.json()["authorization_servers"] == [issuer + "/"]
        assert authorization_server.json()["token_endpoint_auth_methods_supported"] == [
            "client_secret_post", "client_secret_basic",
        ]
        assert authorization_server.json()["issuer"] == issuer + "/"
        assert authorization_server.json()["authorization_endpoint"] == issuer + "/authorize"
        assert "authorization_response_iss_parameter_supported" not in authorization_server.json()
        assert authorization_server.json()["code_challenge_methods_supported"] == ["S256"]

        registered = http.post(
            "/register",
            json={
                "client_name": "Generic MCP Client",
                "redirect_uris": [redirect_uri],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "system:read",
            },
        )
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        assert registered.json()["token_endpoint_auth_method"] == "none"

        invalid_scope = http.get(
            "/authorize",
            params={
                "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
                "state": "scope-error-state", "scope": "system:internal",
                "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource,
            },
            follow_redirects=False,
        )
        assert invalid_scope.status_code == 302
        invalid_scope_query = parse_qs(urlsplit(invalid_scope.headers["location"]).query)
        assert invalid_scope_query["error"] == ["invalid_scope"]
        assert invalid_scope_query["state"] == ["scope-error-state"]
        assert invalid_scope_query["iss"] == [issuer + "/"]

        unsafe_callback = http.get(
            "/authorize",
            params={
                "client_id": client_id, "response_type": "code",
                "redirect_uri": "https://attacker.example.test/steal", "state": "unsafe-state",
                "scope": "system:read", "code_challenge": challenge,
                "code_challenge_method": "S256", "resource": resource,
            },
            follow_redirects=False,
        )
        assert unsafe_callback.status_code == 400
        assert "location" not in unsafe_callback.headers

        authorization = http.get(
            "/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "state": "preserved-state",
                "scope": "system:read",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": resource,
            },
            follow_redirects=False,
        )
        assert authorization.status_code == 302
        consent_url = urlsplit(authorization.headers["location"])
        assert consent_url.path == "/mcp-connect"
        request_id = parse_qs(consent_url.query)["request"][0]
        assert request_id.startswith("qmr_")

        details = registry.oauth_service.request_details(request_id, 1)
        assert details["application_name"] == "Generic MCP Client"
        callback = registry.oauth_service.decide(
            request_id,
            1,
                MCPOAuthDecision(approved=True, scopes=["system:read"]),
        )
        callback_query = parse_qs(urlsplit(callback).query)
        assert callback_query["state"] == ["preserved-state"]
        assert callback_query["iss"] == [issuer + "/"]
        code = callback_query["code"][0]

        wrong_client = http.post(
            "/token",
            data={
                "grant_type": "authorization_code", "client_id": "not-registered",
                "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier,
                "resource": resource,
            },
        )
        assert wrong_client.status_code == 401

        wrong_callback = http.post(
            "/token",
            data={
                "grant_type": "authorization_code", "client_id": client_id,
                "code": code, "redirect_uri": "https://attacker.example.test/callback",
                "code_verifier": verifier, "resource": resource,
            },
        )
        assert wrong_callback.status_code == 400

        missing_pkce = http.post(
            "/token",
            data={
                "grant_type": "authorization_code", "client_id": client_id,
                "code": code, "redirect_uri": redirect_uri, "resource": resource,
            },
        )
        assert missing_pkce.status_code == 400

        bad_pkce = http.post(
            "/token",
            data={
                "grant_type": "authorization_code", "client_id": client_id,
                "code": code, "redirect_uri": redirect_uri, "code_verifier": "wrong-" + verifier,
                "resource": resource,
            },
        )
        assert bad_pkce.status_code == 400
        assert bad_pkce.json()["error"] == "invalid_grant"

        token_response = http.post(
            "/token",
            data={
                "grant_type": "authorization_code", "client_id": client_id,
                "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier,
                "resource": resource,
            },
        )
        assert token_response.status_code == 200, token_response.text
        tokens = token_response.json()
        assert tokens["token_type"].casefold() == "bearer"
        assert tokens["scope"] == "system:read"
        assert tokens["access_token"].startswith("qxo_")
        assert tokens["refresh_token"].startswith("qxr_")
        assert tokens["expires_in"] == 3600

        replayed_code = http.post(
            "/token",
            data={
                "grant_type": "authorization_code", "client_id": client_id,
                "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier,
                "resource": resource,
            },
        )
        assert replayed_code.status_code == 400
        assert replayed_code.json()["error"] == "invalid_grant"

        headers = {
            "Authorization": f"Bearer {tokens['access_token']}",
            "Accept": "application/json, text/event-stream",
        }
        initialize = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}},
        }
        call = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "lynewright.test.info", "arguments": {}},
        }
        assert http.post("/mcp", headers=headers, json=initialize).status_code == 200
        tool_response = http.post("/mcp", headers=headers, json=call)
        assert tool_response.status_code == 200, tool_response.text
        assert _tool_result(tool_response)["name"] == "Lynewright"

        write = http.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "lynewright.projects.create", "arguments": {"name": "Must be denied"}},
            },
        )
        assert write.status_code == 200
        assert write.json()["result"]["isError"] is True

        missing_resource = http.post(
            "/token",
            data={
                "grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert missing_resource.status_code == 400

        wrong_resource = http.post(
            "/token",
            data={
                "grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": tokens["refresh_token"], "resource": issuer + "/other",
            },
        )
        assert wrong_resource.status_code == 400

        # No query-string or metadata representation contains a reusable token/code.
        assert "access_token" not in authorization_server.text
        assert tokens["access_token"] not in authorization_server.text
        assert tokens["access_token"] not in callback

    connection.close()


def test_oauth_guard_rejects_duplicate_fields_and_oversized_authorization_query(monkeypatch) -> None:
    issuer = "https://app.example.test"
    monkeypatch.setenv("LYNEWRIGHT_PUBLIC_URL", issuer)
    monkeypatch.setattr("app.services.rate_limiter.get_rate_limiter", lambda: AllowRateLimiter())
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, role TEXT, is_active INTEGER)")
    connection.execute("INSERT INTO users VALUES (1,'admin@example.test','admin',1)")
    connection.commit()
    mcp, registry = create_mcp_server(PlatformStore(lambda: connection))

    with TestClient(create_mcp_http_app(mcp, oauth_provider=_authorization_provider(registry)), base_url=issuer) as http:
        duplicate = http.get("/authorize?client_id=a&client_id=b")
        assert duplicate.status_code == 400
        assert duplicate.json()["error"] == "invalid_request"
        duplicate_revoke = http.post("/revoke", content="token=a&token=b&client_id=c")
        assert duplicate_revoke.status_code == 400
        assert duplicate_revoke.json()["error"] == "invalid_request"
        duplicate_secret = http.post(
            "/revoke", content="token=a&client_id=c&client_secret=x&client_secret=y",
        )
        assert duplicate_secret.status_code == 400
        assert duplicate_secret.json()["error"] == "invalid_request"
        oversized = http.get("/authorize?" + "x" * 8200)
        assert oversized.status_code == 400
        assert oversized.json()["error"] == "invalid_request"
        preflight = http.options(
            "/revoke",
            headers={
                "Origin": "https://client.example.test",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "*"
    connection.close()


@pytest.mark.parametrize("token_type_hint", [None, "access_token"])
def test_public_client_access_token_revocation_without_secret_revokes_family(
    monkeypatch, token_type_hint: str | None,
) -> None:
    issuer, connection, _store, registry, app = _create_transport(monkeypatch)
    with TestClient(app, base_url=issuer) as http:
        client_id, tokens = _issue_oauth_tokens(http, registry, issuer, "Access revocation")
        data = {"token": tokens["access_token"], "client_id": client_id}
        if token_type_hint is not None:
            data["token_type_hint"] = token_type_hint
        revoked = http.post("/revoke", data=data)
        assert revoked.status_code == 200
        assert revoked.content == b""
        assert revoked.headers["cache-control"] == "no-store"
        assert revoked.headers["pragma"] == "no-cache"

        assert _mcp_initialize(http, tokens["access_token"]).status_code == 401
        refresh = http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
                "resource": issuer + "/mcp",
            },
        )
        assert refresh.status_code == 400
        assert refresh.json()["error"] == "invalid_grant"
    connection.close()


def test_public_client_refresh_token_revocation_without_secret_revokes_family(monkeypatch) -> None:
    issuer, connection, _store, registry, app = _create_transport(monkeypatch)
    with TestClient(app, base_url=issuer) as http:
        client_id, tokens = _issue_oauth_tokens(http, registry, issuer, "Refresh revocation")
        revoked = http.post(
            "/revoke",
            data={
                "token": tokens["refresh_token"],
                "client_id": client_id,
                "token_type_hint": "refresh_token",
            },
        )
        assert revoked.status_code == 200
        assert revoked.content == b""
        assert revoked.headers["cache-control"] == "no-store"
        assert _mcp_initialize(http, tokens["access_token"]).status_code == 401
        refresh = http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
                "resource": issuer + "/mcp",
            },
        )
        assert refresh.status_code == 400
        assert refresh.json()["error"] == "invalid_grant"
    connection.close()


def test_expired_access_token_can_still_revoke_its_live_refresh_family(monkeypatch) -> None:
    issuer, connection, _store, registry, app = _create_transport(monkeypatch)
    with TestClient(app, base_url=issuer) as http:
        client_id, tokens = _issue_oauth_tokens(http, registry, issuer, "Expired access revocation")
        connection.execute(
            """UPDATE mcp_oauth_access_tokens SET expires_at_epoch=0
               WHERE grant_id=(SELECT grant_id FROM mcp_oauth_grants WHERE client_id=?)""",
            (client_id,),
        )
        connection.commit()
        assert _mcp_initialize(http, tokens["access_token"]).status_code == 401
        revoked = http.post("/revoke", data={"token": tokens["access_token"], "client_id": client_id})
        assert revoked.status_code == 200
        refresh = http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
                "resource": issuer + "/mcp",
            },
        )
        assert refresh.status_code == 400
        assert refresh.json()["error"] == "invalid_grant"
    connection.close()


@pytest.mark.parametrize("token_kind", ["access_token", "refresh_token"])
def test_other_oauth_client_cannot_revoke_token_or_learn_ownership(monkeypatch, token_kind: str) -> None:
    issuer, connection, _store, registry, app = _create_transport(monkeypatch)
    with TestClient(app, base_url=issuer) as http:
        owner_id, tokens = _issue_oauth_tokens(http, registry, issuer, "Owner client")
        other_id, _redirect = _register_public_client(http, "Other client")
        token_value = tokens[token_kind]
        attempted = http.post(
            "/revoke",
            data={"token": token_value, "client_id": other_id, "token_type_hint": token_kind},
        )
        assert attempted.status_code == 200
        assert attempted.content == b""

        if token_kind == "access_token":
            assert _tool_result(_mcp_tool(http, tokens["access_token"], "lynewright.test.info"))["name"] == "Lynewright"
        else:
            owner_refresh = http.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": owner_id,
                    "refresh_token": tokens["refresh_token"],
                    "resource": issuer + "/mcp",
                },
            )
            assert owner_refresh.status_code == 200
            assert _mcp_initialize(http, tokens["access_token"]).status_code == 200
    connection.close()


def test_revoke_unknown_and_already_revoked_tokens_is_non_disclosing_and_legacy_is_unchanged(monkeypatch) -> None:
    issuer, connection, _store, registry, app = _create_transport(monkeypatch)
    with TestClient(app, base_url=issuer) as http:
        client_id, tokens = _issue_oauth_tokens(http, registry, issuer, "Unknown token behavior")
        unknown = http.post(
            "/revoke", data={"token": "qxo_unknown-token", "client_id": client_id},
        )
        assert unknown.status_code == 200
        assert unknown.content == b""

        first = http.post("/revoke", data={"token": tokens["access_token"], "client_id": client_id})
        repeated = http.post("/revoke", data={"token": tokens["access_token"], "client_id": client_id})
        assert first.status_code == repeated.status_code == 200
        assert repeated.content == b""

        legacy = registry.clients.create_client(
            {"name": "Legacy revocation isolation", "permission_mode": "custom", "granted_capabilities": ["system:read"]},
            actor=CapabilityActor.platform_manager(),
        )
        legacy_revoke = http.post(
            "/revoke", data={"token": legacy["access_token"], "client_id": client_id},
        )
        assert legacy_revoke.status_code == 200
        assert _tool_result(_mcp_tool(http, legacy["access_token"], "lynewright.test.info"))["name"] == "Lynewright"

        missing_token = http.post("/revoke", data={"client_id": client_id})
        assert missing_token.status_code == 400
        assert missing_token.json()["error"] == "invalid_request"
    connection.close()


def test_revoke_rate_limit_still_applies(monkeypatch) -> None:
    issuer, connection, _store, _registry, app = _create_transport(monkeypatch)
    monkeypatch.setattr("app.services.rate_limiter.get_rate_limiter", lambda: DenyRateLimiter())
    with TestClient(app, base_url=issuer) as http:
        limited = http.post("/revoke", data={"token": "qxo_unknown", "client_id": "unknown"})
        assert limited.status_code == 429
        assert limited.json()["error"] == "rate_limited"
        assert limited.headers["retry-after"] == "60"
    connection.close()


def test_oauth_disabled_falls_back_to_verified_legacy_tokens_and_revokes_oauth_grants(monkeypatch) -> None:
    issuer, connection, store, registry, enabled_app = _create_transport(monkeypatch)
    with TestClient(enabled_app, base_url=issuer) as enabled_http:
        oauth_client_id, oauth_tokens = _issue_oauth_tokens(
            enabled_http, registry, issuer, "Rollback grant",
        )
        legacy = registry.clients.create_client(
            {"name": "Rollback legacy", "permission_mode": "custom", "granted_capabilities": ["system:read"]},
            actor=CapabilityActor.platform_manager(),
        )

    monkeypatch.setenv("LYNEWRIGHT_MCP_OAUTH_ENABLED", "false")
    mcp, rollback_registry = create_mcp_server(store)
    _register_test_info_tool(mcp, rollback_registry)
    rollback_app = create_mcp_http_app(mcp, oauth_provider=_authorization_provider(rollback_registry))
    rollback_paths = {getattr(route, "path", None) for route in rollback_app.app.routes}
    assert "/.well-known/oauth-authorization-server" not in rollback_paths
    assert not {"/authorize", "/token", "/register", "/revoke"} & rollback_paths
    grant = connection.execute(
        "SELECT revoked_at FROM mcp_oauth_grants WHERE client_id=?", (oauth_client_id,),
    ).fetchone()
    assert grant is not None and grant["revoked_at"] is not None

    with TestClient(rollback_app, base_url=issuer) as http:
        assert http.get("/.well-known/oauth-authorization-server").status_code == 404
        assert http.post("/register", json={}).status_code == 404
        assert http.post("/token", data={}).status_code == 404
        assert http.post("/revoke", data={}).status_code == 404
        assert _mcp_initialize(http, oauth_tokens["access_token"]).status_code == 401

        info = _mcp_tool(http, legacy["access_token"], "lynewright.test.info")
        assert info.status_code == 200
        assert _tool_result(info)["name"] == "Lynewright"
        summary = http.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {legacy['access_token']}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0", "id": 3, "method": "resources/read",
                "params": {"uri": "lynewright://test/summary"},
            },
        )
        assert summary.status_code == 200
        assert summary.json()["result"]["contents"]
        write = _mcp_tool(http, legacy["access_token"], "lynewright.test.write", {"value": "Should be denied"})
        assert write.status_code == 200
        assert write.json()["result"]["isError"] is True

        rollback_registry.clients.update_client(
            legacy["id"], {"enabled": False}, actor=CapabilityActor.platform_manager(),
        )
        assert _mcp_initialize(http, legacy["access_token"]).status_code == 401
    connection.close()


def test_oauth_enabled_keeps_grants_and_legacy_clients_isolated(monkeypatch) -> None:
    issuer, connection, _store, registry, app = _create_transport(monkeypatch)
    legacy = registry.clients.create_client(
        {"name": "Enabled legacy", "permission_mode": "custom", "granted_capabilities": ["system:read"]},
        actor=CapabilityActor.platform_manager(),
    )
    with TestClient(app, base_url=issuer) as http:
        first_id, first_tokens = _issue_oauth_tokens(http, registry, issuer, "First grant", admin_user_id=1)
        second_id, second_tokens = _issue_oauth_tokens(http, registry, issuer, "Second grant", admin_user_id=2)
        actor_rows = connection.execute(
            "SELECT actor_id,grant_id FROM mcp_oauth_grants WHERE client_id IN (?,?)",
            (first_id, second_id),
        ).fetchall()
        assert len(actor_rows) == 2
        assert len({row["actor_id"] for row in actor_rows}) == 2
        assert len({row["grant_id"] for row in actor_rows}) == 2
        first_live = _mcp_tool(http, first_tokens["access_token"], "lynewright.test.info")
        assert _tool_result(first_live)["name"] == "Lynewright"

        broadened = http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": first_id,
                "refresh_token": first_tokens["refresh_token"],
                "resource": issuer + "/mcp",
                "scope": "system:read projects:read",
            },
        )
        assert broadened.status_code == 400
        assert broadened.json()["error"] == "invalid_scope"

        first_result = http.post(
            "/revoke", data={"token": first_tokens["access_token"], "client_id": first_id},
        )
        assert first_result.status_code == 200
        assert _mcp_initialize(http, first_tokens["access_token"]).status_code == 401
        assert _tool_result(_mcp_tool(http, second_tokens["access_token"], "lynewright.test.info"))["name"] == "Lynewright"
        assert _tool_result(_mcp_tool(http, legacy["access_token"], "lynewright.test.info"))["name"] == "Lynewright"
        second_family = connection.execute(
            "SELECT revoked_at_epoch FROM mcp_oauth_refresh_families WHERE client_id=?",
            (second_id,),
        ).fetchone()
        assert second_family is not None and second_family["revoked_at_epoch"] is None
    connection.close()


def test_rollback_flag_removes_oauth_protocol_routes_but_keeps_mcp(monkeypatch) -> None:
    issuer = "https://app.example.test"
    monkeypatch.setenv("LYNEWRIGHT_PUBLIC_URL", issuer)
    monkeypatch.setenv("LYNEWRIGHT_MCP_OAUTH_ENABLED", "false")
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, role TEXT, is_active INTEGER)")
    connection.execute("INSERT INTO users VALUES (1,'admin@example.test','admin',1)")
    connection.commit()
    mcp, registry = create_mcp_server(PlatformStore(lambda: connection))
    paths = {
        getattr(route, "path", None)
        for route in create_mcp_http_app(mcp, oauth_provider=_authorization_provider(registry)).app.routes
    }
    assert "/mcp" in paths
    assert "/.well-known/oauth-protected-resource/mcp" in paths
    assert "/.well-known/oauth-authorization-server" not in paths
    assert not {"/authorize", "/token", "/register", "/revoke"} & paths
    connection.close()
