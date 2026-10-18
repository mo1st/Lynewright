from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from app.models.mcp_oauth import MCPOAuthDecision
from app.services.mcp_oauth import (
    MCPOAuthError,
    MCPOAuthService,
    LynewrightOAuthClient,
    canonical_public_origin,
)
from app.services.platform_capabilities import PUBLIC_CAPABILITIES


class SQLiteStore:
    """Minimal store adapter: OAuth owns its schema; platform schema is irrelevant here."""

    def __init__(self, connect, *, close_connection: bool = True):
        self.connect = connect
        self.close_connection = close_connection
        self.lock = threading.RLock()

    @contextmanager
    def connection(self):
        with self.lock:
            db = self.connect()
            try:
                yield db
            finally:
                if self.close_connection:
                    db.close()

ISSUER_ORIGIN = "https://app.example.test"
ISSUER = ISSUER_ORIGIN + "/"
RESOURCE = f"{ISSUER_ORIGIN}/mcp"
CALLBACK = "https://client.example.test/callback"
VERIFIER = "m" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture()
def oauth_db(tmp_path):
    """A file-backed database exercises persistence and SQLite transaction behavior."""
    path = tmp_path / "oauth.sqlite3"
    opened: list[sqlite3.Connection] = []

    def connect() -> sqlite3.Connection:
        db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        db.row_factory = sqlite3.Row
        opened.append(db)
        return db

    bootstrap = connect()
    bootstrap.execute(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL, role TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )"""
    )
    bootstrap.execute("INSERT INTO users (id,email,role,is_active) VALUES (1,'admin@example.test','admin',1)")
    bootstrap.commit()
    bootstrap.close()
    service = MCPOAuthService(SQLiteStore(connect), issuer_url=ISSUER, resource_url=RESOURCE)
    yield service, connect, path, opened
    for db in opened:
        try:
            db.close()
        except sqlite3.Error:
            pass


def register(
    service: MCPOAuthService,
    redirect_uri: str = CALLBACK,
    client_id: str = "generic-public-client",
) -> LynewrightOAuthClient:
    info = OAuthClientInformationFull(
        client_id=client_id,
        client_name="Generic MCP Client",
        redirect_uris=[AnyUrl(redirect_uri)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=" ".join(sorted(PUBLIC_CAPABILITIES)),
    )
    service.register_client(info)
    client = service.get_client(client_id)
    assert client is not None
    return client


def pending(
    service: MCPOAuthService,
    client: LynewrightOAuthClient,
    *,
    scopes: list[str] | None = None,
    state: str = "state-value",
) -> str:
    return service.create_authorization_request(
        client,
        AuthorizationParams(
            state=state,
            scopes=scopes or ["system:read"],
            code_challenge=CHALLENGE,
            redirect_uri=AnyUrl(CALLBACK),
            redirect_uri_provided_explicitly=True,
            resource=RESOURCE,
        ),
    )


def approve(service: MCPOAuthService, request_id: str, scopes: list[str] | None = None) -> tuple[str, str]:
    selected = scopes or ["system:read"]
    redirect = service.decide(
        request_id,
        1,
        MCPOAuthDecision(
            approved=True,
            scopes=selected,
            confirm_writes=any(not scope.endswith(":read") for scope in selected),
        ),
    )
    query = parse_qs(urlsplit(redirect).query)
    return redirect, query["code"][0]


def exchange(service: MCPOAuthService, client: LynewrightOAuthClient, code: str):
    auth_code = run_oauth_method(service.load_authorization_code(client, code))
    assert auth_code is not None
    return auth_code, run_oauth_method(service.exchange_authorization_code(client, auth_code))


def run_oauth_method(coroutine):
    """Run the provider's await-free SQLite adapter without a Windows event loop."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("OAuth persistence methods should not suspend during a SQLite operation")


def test_public_origin_and_registration_reject_unsafe_redirects(oauth_db) -> None:
    service, _, _, _ = oauth_db
    assert canonical_public_origin("HTTPS://APP.EXAMPLE.TEST/") == ISSUER_ORIGIN
    for value in ("https://user:pass@example.test", "https://example.test/path", "https://example.test/?x=1"):
        with pytest.raises(ValueError):
            canonical_public_origin(value)

    for index, redirect in enumerate((
        "https://client.example.test/callback#fragment",
        "https://client.example.test/callback?code=attacker",
        "https://client.example.test/*",
        "http://oauth.example.invalid/callback",
        "javascript:alert(1)",
    )):
        info = OAuthClientInformationFull(
            client_id=f"invalid-{index}",
            redirect_uris=[AnyUrl("https://client.example.test/callback")],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        # The raw redirect adapter validates values before AnyUrl normalization.
        from app.services.mcp_oauth import _REGISTER_REDIRECT_URIS
        context = _REGISTER_REDIRECT_URIS.set([redirect])
        try:
            with pytest.raises(Exception):
                service.register_client(info)
        finally:
            _REGISTER_REDIRECT_URIS.reset(context)


def test_authorization_code_is_bound_hashed_and_one_time(oauth_db) -> None:
    service, connect, _, _ = oauth_db
    client = register(service)
    request_id = pending(service, client, scopes=["system:read", "projects:create"])
    details = service.request_details(request_id, 1)
    assert details["application_name"] == "Generic MCP Client"
    assert details["default_scopes"] == ["system:read"]

    redirect, code = approve(service, request_id, ["system:read"])
    query = parse_qs(urlsplit(redirect).query)
    assert query["state"] == ["state-value"]
    assert query["iss"] == [ISSUER]
    db = connect()
    code_hash = db.execute("SELECT code_hash FROM mcp_oauth_codes").fetchone()[0]
    raw_request = db.execute("SELECT request_hash FROM mcp_oauth_requests").fetchone()[0]
    db.close()
    assert code not in str(code_hash)
    assert request_id not in raw_request

    auth_code, tokens = exchange(service, client, code)
    assert tokens.access_token.startswith("qxo_")
    assert tokens.refresh_token.startswith("qxr_")
    db = connect()
    row = db.execute("SELECT code_hash,consumed_at_epoch FROM mcp_oauth_codes").fetchone()
    db.close()
    assert row["consumed_at_epoch"] is not None
    assert code not in row["code_hash"]
    with pytest.raises(TokenError):
        run_oauth_method(service.exchange_authorization_code(client, auth_code))


def test_access_actor_is_bounded_by_token_scope_and_persists_across_service_instances(oauth_db) -> None:
    service, connect, _, _ = oauth_db
    client = register(service)
    _, code = approve(service, pending(service, client, scopes=["system:read", "projects:create"]))
    _, tokens = exchange(service, client, code)

    restarted = MCPOAuthService(SQLiteStore(connect), issuer_url=ISSUER, resource_url=RESOURCE)
    access = run_oauth_method(restarted.load_access_token(tokens.access_token))
    assert access is not None
    assert access.client_id == client.client_id
    actor = restarted.actor_for_access_token(access)
    assert actor.client_id != client.client_id
    assert actor.permission_mode == "custom"
    assert actor.granted_capabilities == frozenset({"system:read"})

    narrowed_token = access.model_copy(update={"scopes": ["projects:create"]})
    narrowed_actor = restarted.actor_for_access_token(narrowed_token)
    assert narrowed_actor.granted_capabilities == frozenset()
    assert run_oauth_method(restarted.load_access_token("qxc_not-an-oauth-token")) is None
    assert run_oauth_method(restarted.load_access_token("dashboard-session-token")) is None


def test_refresh_rotation_reuse_revokes_family_and_cannot_expand_scope(oauth_db) -> None:
    service, _, _, _ = oauth_db
    client = register(service)
    _, code = approve(service, pending(service, client, scopes=["system:read", "projects:create"]))
    _, initial = exchange(service, client, code)
    loaded = run_oauth_method(service.load_refresh_token(client, initial.refresh_token))
    assert loaded is not None
    with pytest.raises(TokenError, match="narrow"):
        run_oauth_method(service.exchange_refresh_token(client, loaded, ["projects:create"]))

    rotated = run_oauth_method(service.exchange_refresh_token(client, loaded, ["system:read"]))
    assert rotated.refresh_token != initial.refresh_token
    assert rotated.scope == "system:read"
    stale = run_oauth_method(service.load_refresh_token(client, initial.refresh_token))
    assert stale is None  # reuse detection has revoked the entire family
    assert run_oauth_method(service.load_access_token(rotated.access_token)) is None


def test_grant_revocation_client_disable_and_admin_demotion_invalidate_tokens(oauth_db) -> None:
    service, connect, _, _ = oauth_db
    client = register(service)
    _, code = approve(service, pending(service, client))
    _, tokens = exchange(service, client, code)
    access = run_oauth_method(service.load_access_token(tokens.access_token))
    assert access is not None

    grant_id = service.list_connections(1)[0]["id"]
    service.revoke_connection(grant_id, 1)
    assert run_oauth_method(service.load_access_token(tokens.access_token)) is None

    _, second_code = approve(service, pending(service, client))
    _, second_tokens = exchange(service, client, second_code)
    service.disable_client(str(client.client_id))
    assert run_oauth_method(service.load_access_token(second_tokens.access_token)) is None

    # Re-register another application and prove current role is checked at use time.
    client2 = register(service, "https://other.example.test/callback", "other-public-client")
    # register() helper's pending URI uses CALLBACK, so create the matching request directly.
    params = AuthorizationParams(
        state=None, scopes=["system:read"], code_challenge=CHALLENGE,
        redirect_uri=AnyUrl("https://other.example.test/callback"),
        redirect_uri_provided_explicitly=True, resource=RESOURCE,
    )
    request_id = service.create_authorization_request(client2, params)
    _, third_code = approve(service, request_id)
    _, third_tokens = exchange(service, client2, third_code)
    db = connect()
    db.execute("UPDATE users SET role='member' WHERE id=1")
    db.commit()
    db.close()
    assert run_oauth_method(service.load_access_token(third_tokens.access_token)) is None


def test_token_revocation_leaves_connection_visible_but_expiry_and_denial_fail_closed(oauth_db, monkeypatch) -> None:
    service, connect, _, _ = oauth_db
    client = register(service)

    expired_request = pending(service, client)
    db = connect()
    db.execute(
        "UPDATE mcp_oauth_requests SET expires_at_epoch=0 WHERE request_hash=(SELECT request_hash FROM mcp_oauth_requests LIMIT 1)"
    )
    db.commit()
    db.close()
    with pytest.raises(MCPOAuthError) as expired_details:
        service.request_details(expired_request, 1)
    assert expired_details.value.status_code == 410
    with pytest.raises(MCPOAuthError) as expired_decision:
        service.decide(expired_request, 1, MCPOAuthDecision(approved=True, scopes=["system:read"]))
    assert expired_decision.value.status_code == 410

    denied_request = pending(service, client, state="denied-state")
    denied_callback = service.decide(denied_request, 1, MCPOAuthDecision(approved=False))
    denied_query = parse_qs(urlsplit(denied_callback).query)
    assert denied_query["error"] == ["access_denied"]
    assert denied_query["state"] == ["denied-state"]
    assert "code" not in denied_query

    _, approved_code = approve(service, pending(service, client))
    _, tokens = exchange(service, client, approved_code)
    access = run_oauth_method(service.load_access_token(tokens.access_token))
    assert access is not None
    refresh = run_oauth_method(service.load_refresh_token(client, tokens.refresh_token))
    assert refresh is not None
    run_oauth_method(service.revoke_token(access))
    assert run_oauth_method(service.load_access_token(tokens.access_token)) is None
    assert run_oauth_method(service.load_refresh_token(client, tokens.refresh_token)) is None
    assert service.list_connections(1)[0]["revoked_at"] is None

    _, expiring_code = approve(service, pending(service, client))
    now = time.time
    monkeypatch.setattr("app.services.mcp_oauth.time.time", lambda: now() + 301)
    assert run_oauth_method(service.load_authorization_code(client, expiring_code)) is None


def test_admin_consent_requires_supported_scopes_and_explicit_write_confirmation(oauth_db) -> None:
    service, connect, _, _ = oauth_db
    client = register(service)
    with pytest.raises(MCPOAuthError):
        pending(service, client, scopes=["core-db:read"])

    request_id = pending(service, client, scopes=["system:read", "projects:create"])
    with pytest.raises(MCPOAuthError, match="write access"):
        service.decide(request_id, 1, MCPOAuthDecision(approved=True, scopes=["projects:create"]))

    with pytest.raises(MCPOAuthError, match="administrator"):
        service.request_details(request_id, 999)
    db = connect()
    db.execute("UPDATE users SET role='member' WHERE id=1")
    db.commit()
    db.close()
    with pytest.raises(MCPOAuthError, match="administrator"):
        service.decide(request_id, 1, MCPOAuthDecision(approved=True, scopes=["system:read"]))


def test_concurrent_authorization_code_exchange_has_exactly_one_winner(oauth_db) -> None:
    service, _, _, _opened = oauth_db
    client = register(service)
    _, code = approve(service, pending(service, client))
    auth_code = run_oauth_method(service.load_authorization_code(client, code))
    assert auth_code is not None
    other_worker = MCPOAuthService(SQLiteStore(oauth_db[1]), issuer_url=ISSUER, resource_url=RESOURCE)

    def attempt(worker: MCPOAuthService):
        try:
            return run_oauth_method(worker.exchange_authorization_code(client, auth_code))
        except TokenError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [service, other_worker]))
    assert sum(result is not None for result in results) == 1
