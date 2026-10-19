"""Persistent OAuth authorization for the Lynewright MCP resource server.

The MCP SDK owns the OAuth HTTP handlers and PKCE comparison. This module
provides their durable storage contract and the Lynewright-specific consent,
administrator, scope, resource, redirect, and token-lifecycle checks.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Any, Iterator, Literal
from urllib.parse import parse_qsl, parse_qs, urlencode, urlsplit, urlunsplit

from pydantic import AnyUrl, BaseModel, PrivateAttr, ValidationError
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import (
    InvalidRedirectUriError,
    InvalidScopeError,
    OAuthClientInformationFull,
    OAuthToken,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.models.mcp_oauth import MCPOAuthDecision
from app.services.platform_capabilities import (
    PUBLIC_CAPABILITIES,
    CapabilityActor,
)
from app.services.platform_services import PlatformStore

logger = logging.getLogger(__name__)

PENDING_REQUEST_TTL_SECONDS = 300
AUTHORIZATION_CODE_TTL_SECONDS = 300
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_FAMILY_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_OAUTH_CLIENTS = 1000
MAX_REDIRECT_URIS = 20
MAX_PENDING_REQUESTS = 1000
MAX_APP_NAME_CHARS = 120
OAUTH_ACCESS_TOKEN_PREFIX = "qxo_"
OAUTH_REFRESH_TOKEN_PREFIX = "qxr_"
OAUTH_CODE_PREFIX = "qoc_"
OAUTH_REQUEST_PREFIX = "qmr_"

_S256_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_REQUEST_ID = re.compile(r"^qmr_[A-Za-z0-9_-]{43}$")
_GRANT_ID = re.compile(r"^mcpgrant_[a-f0-9]{32}$")
_OAUTH_RESPONSE_PARAMS = frozenset({"code", "state", "error", "error_description", "error_uri", "iss"})

# The HTTP adapter captures the original form/query strings before the SDK's
# AnyUrl parser normalizes them. That lets the provider preserve exact callback
# matching while still reusing the SDK's route handlers and PKCE implementation.
_REGISTER_REDIRECT_URIS: ContextVar[list[str] | None] = ContextVar("mcp_oauth_register_redirect_uris", default=None)
_AUTHORIZATION_REDIRECT_URI: ContextVar[str | None] = ContextVar("mcp_oauth_authorization_redirect_uri", default=None)
_TOKEN_REDIRECT_URI: ContextVar[str | None] = ContextVar("mcp_oauth_token_redirect_uri", default=None)


def mcp_oauth_enabled() -> bool:
    return os.getenv("LYNEWRIGHT_MCP_OAUTH_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


class MCPOAuthError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class LynewrightOAuthClient(OAuthClientInformationFull):
    """OAuth client metadata with raw, exact callback matching."""

    _exact_redirect_uris: list[str] = PrivateAttr(default_factory=list)

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            return super().validate_redirect_uri(redirect_uri)
        requested = _AUTHORIZATION_REDIRECT_URI.get() or str(redirect_uri)
        if any(_redirect_matches(requested, allowed) for allowed in self._exact_redirect_uris):
            return redirect_uri
        raise InvalidRedirectUriError("The redirect URI does not exactly match a registered callback.")

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        if requested_scope is None:
            return None
        requested = requested_scope.split()
        allowed = set(self.scope.split()) if self.scope else PUBLIC_CAPABILITIES
        if not requested or any(scope not in allowed or scope not in PUBLIC_CAPABILITIES for scope in requested):
            raise InvalidScopeError("The request includes a scope that this client cannot use.")
        return requested


def canonical_public_origin(public_url: str) -> str:
    """Return a configured origin, rejecting URL parts that could redirect it."""
    try:
        parsed = urlsplit(public_url.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        _ = parsed.port
    except (ValueError, AttributeError) as exc:
        raise ValueError("LYNEWRIGHT_PUBLIC_URL must be a valid public origin.") from exc
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme.lower()}://{host}{port}"


def is_read_capability(scope: str) -> bool:
    return (
        scope.endswith(":read")
        or scope in {"system:diagnostics", "credentials:list", "credentials:status", "ai:read-config"}
    )


def _loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_redirect_uri(uri: str) -> str:
    if len(uri) > 2048 or "*" in uri:
        raise ValueError("The redirect URI is invalid.")
    try:
        parsed = urlsplit(uri)
        hostname = parsed.hostname
        _ = parsed.port
        query_keys = {key for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    except ValueError as exc:
        raise ValueError("The redirect URI is invalid.") from exc
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or query_keys & _OAUTH_RESPONSE_PARAMS
    ):
        raise ValueError("The redirect URI is invalid.")
    if parsed.scheme == "https":
        return uri
    if parsed.scheme == "http" and _loopback_host(hostname):
        return uri
    raise ValueError("Redirect URIs must use HTTPS or a native application's loopback address.")


def _redirect_matches(requested: str, registered: str) -> bool:
    if requested == registered:
        return True
    try:
        actual = urlsplit(requested)
        expected = urlsplit(registered)
        if actual.scheme != "http" or expected.scheme != "http":
            return False
        if not _loopback_host(actual.hostname) or not _loopback_host(expected.hostname):
            return False
        # RFC 8252 permits the ephemeral port to change for loopback-native
        # clients. The address, path and query remain exact.
        return (
            actual.hostname.casefold() == expected.hostname.casefold()
            and actual.path == expected.path
            and actual.query == expected.query
            and not actual.fragment
            and not expected.fragment
            and actual.username is None
            and expected.username is None
            and actual.password is None
            and expected.password is None
        )
    except ValueError:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _request_hash(request_id: str) -> str:
    if not _REQUEST_ID.fullmatch(request_id):
        raise MCPOAuthError("This authorization request is not available.", status_code=404)
    return _token_hash(request_id)


def _new_secret(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def _json_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _with_oauth_response(uri: str, *, issuer: str, state: str | None, code: str | None = None) -> str:
    parsed = urlsplit(uri)
    params: list[tuple[str, str]] = parse_qsl(parsed.query, keep_blank_values=True)
    if code is None:
        params.append(("error", "access_denied"))
    else:
        params.append(("code", code))
    if state is not None:
        params.append(("state", state))
    params.append(("iss", issuer))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))


def _is_oauth_callback_location(location: str, requested_redirect: str) -> bool:
    """Confirm an SDK error redirect targets the already validated callback."""
    try:
        parsed = urlsplit(location)
        response_values = {
            "code", "state", "error", "error_description", "error_uri", "iss",
        }
        original_query = [
            (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in response_values
        ]
        callback = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(original_query), ""))
        return _redirect_matches(callback, requested_redirect)
    except ValueError:
        return False


class MCPOAuthService:
    """Persists registrations, consent, grants, codes, and rotating token families."""

    def __init__(
        self,
        store: PlatformStore | None = None,
        *,
        issuer_url: str,
        resource_url: str,
    ) -> None:
        self.store = store or PlatformStore()
        # Preserve the URL serialization used by OAuth metadata. An origin
        # issuer serializes with a trailing slash (AnyHttpUrl), and the value
        # must match exactly in authorization-response `iss` and token claims.
        self.issuer_url = issuer_url
        self.resource_url = resource_url

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_name TEXT NOT NULL,
                redirect_uris_json TEXT NOT NULL,
                scope TEXT,
                grant_types_json TEXT NOT NULL,
                response_types_json TEXT NOT NULL,
                token_endpoint_auth_method TEXT NOT NULL,
                disabled_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_oauth_clients_created
                ON mcp_oauth_clients(created_at);

            CREATE TABLE IF NOT EXISTS mcp_oauth_requests (
                request_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                redirect_uri_explicit INTEGER NOT NULL,
                state TEXT,
                code_challenge TEXT NOT NULL,
                requested_scopes_json TEXT NOT NULL,
                resource TEXT NOT NULL,
                created_at_epoch REAL NOT NULL,
                expires_at_epoch REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_user_id INTEGER,
                approved_scopes_json TEXT,
                completed_at_epoch REAL
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_oauth_requests_expiry
                ON mcp_oauth_requests(expires_at_epoch,status);

            CREATE TABLE IF NOT EXISTS mcp_oauth_grants (
                grant_id TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL UNIQUE,
                client_id TEXT NOT NULL,
                admin_user_id INTEGER NOT NULL,
                approved_scopes_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_oauth_grants_client
                ON mcp_oauth_grants(client_id,admin_user_id,created_at);

            CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
                code_hash TEXT PRIMARY KEY,
                grant_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                resource TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                expires_at_epoch REAL NOT NULL,
                consumed_at_epoch REAL
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_oauth_codes_expiry
                ON mcp_oauth_codes(expires_at_epoch);

            CREATE TABLE IF NOT EXISTS mcp_oauth_refresh_families (
                family_id TEXT PRIMARY KEY,
                grant_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                resource TEXT NOT NULL,
                created_at_epoch REAL NOT NULL,
                expires_at_epoch REAL NOT NULL,
                revoked_at_epoch REAL
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_oauth_refresh_family_grant
                ON mcp_oauth_refresh_families(grant_id,expires_at_epoch);

            CREATE TABLE IF NOT EXISTS mcp_oauth_refresh_tokens (
                token_hash TEXT PRIMARY KEY,
                family_id TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                created_at_epoch REAL NOT NULL,
                consumed_at_epoch REAL
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_oauth_refresh_tokens_family
                ON mcp_oauth_refresh_tokens(family_id);

            CREATE TABLE IF NOT EXISTS mcp_oauth_access_tokens (
                token_hash TEXT PRIMARY KEY,
                grant_id TEXT NOT NULL,
                family_id TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                resource TEXT NOT NULL,
                created_at_epoch REAL NOT NULL,
                expires_at_epoch REAL NOT NULL,
                revoked_at_epoch REAL
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_oauth_access_grant
                ON mcp_oauth_access_tokens(grant_id,expires_at_epoch);
            """
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    def register_client(self, client: OAuthClientInformationFull) -> None:
        client_id = str(client.client_id or "")
        if not client_id or client.client_secret or client.token_endpoint_auth_method != "none":
            raise RegistrationError("invalid_client_metadata", "Only public clients using token auth method none are supported.")
        if set(client.grant_types) != {"authorization_code", "refresh_token"} or set(client.response_types) != {"code"}:
            raise RegistrationError("invalid_client_metadata", "Only authorization_code, refresh_token, and code are supported.")
        if not client.redirect_uris or len(client.redirect_uris) > MAX_REDIRECT_URIS:
            raise RegistrationError("invalid_redirect_uri", "Register between 1 and 20 redirect URIs.")
        supplied = _REGISTER_REDIRECT_URIS.get()
        raw_redirects = supplied if supplied is not None else [str(uri) for uri in client.redirect_uris]
        if len(raw_redirects) != len(client.redirect_uris):
            raise RegistrationError("invalid_redirect_uri", "The redirect URI list is invalid.")
        try:
            exact_redirects = [_validated_redirect_uri(uri) for uri in raw_redirects]
        except ValueError as exc:
            raise RegistrationError("invalid_redirect_uri", str(exc)) from exc
        if len(exact_redirects) != len(set(exact_redirects)):
            raise RegistrationError("invalid_redirect_uri", "Duplicate redirect URIs are not allowed.")
        registered_scopes = client.scope.split() if client.scope else None
        if registered_scopes is not None and (
            len(registered_scopes) != len(set(registered_scopes))
            or not set(registered_scopes).issubset(PUBLIC_CAPABILITIES)
        ):
            raise RegistrationError("invalid_client_metadata", "Only supported public capabilities may be registered.")
        app_name = (client.client_name or "").strip()[:MAX_APP_NAME_CHARS] or "Unidentified MCP application"
        registration_error: tuple[str, str] | None = None
        with self._connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._purge_stale(db)
                count = int(db.execute("SELECT COUNT(*) FROM mcp_oauth_clients").fetchone()[0])
                if count >= MAX_OAUTH_CLIENTS:
                    db.rollback()
                    registration_error = (
                        "invalid_client_metadata", "The OAuth client registration limit has been reached."
                    )
                else:
                    db.execute(
                        """INSERT INTO mcp_oauth_clients (
                            client_id,client_name,redirect_uris_json,scope,grant_types_json,
                            response_types_json,token_endpoint_auth_method
                        ) VALUES (?,?,?,?,?,?,?)""",
                        (
                            client_id, app_name, json.dumps(exact_redirects),
                            " ".join(registered_scopes) if registered_scopes is not None else None,
                            json.dumps(["authorization_code", "refresh_token"]), json.dumps(["code"]), "none",
                        ),
                    )
                    db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                registration_error = ("invalid_client_metadata", "The OAuth client could not be registered.")
            except Exception:
                db.rollback()
                raise
        if registration_error is not None:
            raise RegistrationError(*registration_error)
        # Never echo metadata that Lynewright does not use or fetch.
        client.client_name = app_name
        client.redirect_uris = [AnyUrl(uri) for uri in exact_redirects]
        client.grant_types = ["authorization_code", "refresh_token"]
        client.response_types = ["code"]
        client.client_uri = None
        client.logo_uri = None
        client.tos_uri = None
        client.policy_uri = None
        client.jwks_uri = None
        client.jwks = None

    def get_client(self, client_id: str) -> LynewrightOAuthClient | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM mcp_oauth_clients WHERE client_id=? AND disabled_at IS NULL",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        exact_redirects = _json_list(str(row["redirect_uris_json"]))
        try:
            client = LynewrightOAuthClient(
                client_id=str(row["client_id"]),
                client_name=str(row["client_name"]),
                redirect_uris=[AnyUrl(uri) for uri in exact_redirects],
                token_endpoint_auth_method="none",
                grant_types=_json_list(str(row["grant_types_json"])),
                response_types=_json_list(str(row["response_types_json"])),
                scope=str(row["scope"]) if row["scope"] is not None else None,
            )
            client._exact_redirect_uris = exact_redirects
            return client
        except (TypeError, ValueError):
            return None

    def create_authorization_request(
        self,
        client: LynewrightOAuthClient,
        params: AuthorizationParams,
    ) -> str:
        if not params.redirect_uri_provided_explicitly:
            raise MCPOAuthError("A redirect URI is required.")
        if params.resource != self.resource_url:
            raise MCPOAuthError("The authorization request must target this MCP resource.")
        if not _S256_CHALLENGE.fullmatch(params.code_challenge):
            raise MCPOAuthError("A valid PKCE S256 challenge is required.")
        state = params.state
        if state is not None and (len(state) > 2048 or any(ord(char) < 32 for char in state)):
            raise MCPOAuthError("The OAuth state value is invalid.")
        raw_redirect = _AUTHORIZATION_REDIRECT_URI.get() or str(params.redirect_uri)
        if not any(_redirect_matches(raw_redirect, registered) for registered in client._exact_redirect_uris):
            raise MCPOAuthError("The redirect URI does not match the client registration.")
        scopes = list(dict.fromkeys(params.scopes or ["system:read"]))
        if not scopes or len(scopes) > 128 or any(scope not in PUBLIC_CAPABILITIES for scope in scopes):
            raise MCPOAuthError("The request contains an unsupported capability.")
        now = time.time()
        with self._connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._purge_stale(db)
                pending = int(db.execute(
                    "SELECT COUNT(*) FROM mcp_oauth_requests WHERE status='pending' AND expires_at_epoch>?",
                    (now,),
                ).fetchone()[0])
                if pending >= MAX_PENDING_REQUESTS:
                    raise MCPOAuthError("There are too many pending authorization requests.", status_code=429)
                request_id = _new_secret(OAUTH_REQUEST_PREFIX)
                db.execute(
                    """INSERT INTO mcp_oauth_requests (
                        request_hash,client_id,redirect_uri,redirect_uri_explicit,state,
                        code_challenge,requested_scopes_json,resource,created_at_epoch,expires_at_epoch
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        _token_hash(request_id), str(client.client_id), raw_redirect, 1, state,
                        params.code_challenge, json.dumps(scopes), self.resource_url,
                        now, now + PENDING_REQUEST_TTL_SECONDS,
                    ),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return request_id

    def request_details(self, request_id: str, admin_user_id: int) -> dict[str, Any]:
        request_hash = _request_hash(request_id)
        with self._connection() as db, db:
            self._require_current_admin(db, admin_user_id)
            row = db.execute(
                """SELECT r.*,c.client_name,c.disabled_at
                   FROM mcp_oauth_requests r
                   JOIN mcp_oauth_clients c ON c.client_id=r.client_id
                   WHERE r.request_hash=?""",
                (request_hash,),
            ).fetchone()
            if row is None:
                raise MCPOAuthError("This authorization request is not available.", status_code=404)
            if str(row["status"]) != "pending":
                raise MCPOAuthError("This authorization request has already been decided.", status_code=409)
            if float(row["expires_at_epoch"]) <= time.time():
                raise MCPOAuthError("This authorization request has expired.", status_code=410)
            if row["disabled_at"] is not None:
                raise MCPOAuthError("This application is no longer enabled.", status_code=404)
            redirect_uri = str(row["redirect_uri"])
            scopes = _json_list(str(row["requested_scopes_json"]))
            return {
                "request_id": request_id,
                "application_name": str(row["client_name"]),
                "redirect_origin": f"{urlsplit(redirect_uri).scheme}://{urlsplit(redirect_uri).netloc}",
                "requested_scopes": scopes,
                "default_scopes": [scope for scope in scopes if is_read_capability(scope)],
                "account_email": self._admin_email(db, admin_user_id),
                "expires_at": float(row["expires_at_epoch"]),
            }

    def decide(
        self,
        request_id: str,
        admin_user_id: int,
        decision: MCPOAuthDecision,
    ) -> str:
        now = time.time()
        with self._connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._require_current_admin(db, admin_user_id)
                request_hash = _request_hash(request_id)
                row = db.execute(
                    """SELECT r.*,c.disabled_at
                       FROM mcp_oauth_requests r
                       JOIN mcp_oauth_clients c ON c.client_id=r.client_id
                       WHERE r.request_hash=?""",
                    (request_hash,),
                ).fetchone()
                if row is None:
                    raise MCPOAuthError("This authorization request is not available.", status_code=404)
                if str(row["status"]) != "pending":
                    raise MCPOAuthError("This authorization request has already been decided.", status_code=409)
                if float(row["expires_at_epoch"]) <= now:
                    raise MCPOAuthError("This authorization request has expired.", status_code=410)
                if row["disabled_at"] is not None:
                    raise MCPOAuthError("This application is no longer enabled.", status_code=404)

                if not decision.approved:
                    db.execute(
                        """UPDATE mcp_oauth_requests
                           SET status='denied',admin_user_id=?,completed_at_epoch=?
                           WHERE request_hash=? AND status='pending'""",
                        (admin_user_id, now, request_hash),
                    )
                    redirect_to = _with_oauth_response(
                        str(row["redirect_uri"]), issuer=self.issuer_url,
                        state=str(row["state"]) if row["state"] is not None else None,
                    )
                else:
                    requested = set(_json_list(str(row["requested_scopes_json"])))
                    approved = set(decision.scopes)
                    if not approved or not approved.issubset(requested) or not approved.issubset(PUBLIC_CAPABILITIES):
                        raise MCPOAuthError("Choose one or more requested, supported permissions.")
                    if any(not is_read_capability(scope) for scope in approved) and not decision.confirm_writes:
                        raise MCPOAuthError("Confirm platform-wide write access before continuing.")
                    grant_id = f"mcpgrant_{secrets.token_hex(16)}"
                    actor_id = f"mcpactor_{secrets.token_hex(16)}"
                    code = _new_secret(OAUTH_CODE_PREFIX)
                    db.execute(
                        """INSERT INTO mcp_oauth_grants (
                            grant_id,actor_id,client_id,admin_user_id,approved_scopes_json
                        ) VALUES (?,?,?,?,?)""",
                        (grant_id, actor_id, str(row["client_id"]), admin_user_id, json.dumps(sorted(approved))),
                    )
                    db.execute(
                        """INSERT INTO mcp_oauth_codes (
                            code_hash,grant_id,client_id,redirect_uri,code_challenge,
                            scopes_json,resource,user_id,expires_at_epoch
                        ) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            _token_hash(code), grant_id, str(row["client_id"]), str(row["redirect_uri"]),
                            str(row["code_challenge"]), json.dumps(sorted(approved)),
                            self.resource_url, admin_user_id, now + AUTHORIZATION_CODE_TTL_SECONDS,
                        ),
                    )
                    db.execute(
                        """UPDATE mcp_oauth_requests
                           SET status='approved',admin_user_id=?,approved_scopes_json=?,completed_at_epoch=?
                           WHERE request_hash=? AND status='pending'""",
                        (admin_user_id, json.dumps(sorted(approved)), now, request_hash),
                    )
                    redirect_to = _with_oauth_response(
                        str(row["redirect_uri"]), issuer=self.issuer_url,
                        state=str(row["state"]) if row["state"] is not None else None,
                        code=code,
                    )
                db.commit()
                return redirect_to
            except Exception:
                db.rollback()
                raise

    def list_connections(self, admin_user_id: int) -> list[dict[str, Any]]:
        with self._connection() as db:
            self._require_current_admin(db, admin_user_id)
            rows = db.execute(
                """SELECT g.grant_id,g.approved_scopes_json,g.created_at,g.revoked_at,
                          c.client_name,u.email
                   FROM mcp_oauth_grants g
                   JOIN mcp_oauth_clients c ON c.client_id=g.client_id
                   LEFT JOIN users u ON u.id=g.admin_user_id
                   ORDER BY g.created_at DESC,g.grant_id"""
            ).fetchall()
            return [
                {
                    "id": str(row["grant_id"]),
                    "application_name": str(row["client_name"]),
                    "account_email": str(row["email"] or "Inactive account"),
                    "approved_scopes": _json_list(str(row["approved_scopes_json"])),
                    "created_at": str(row["created_at"]),
                    "revoked_at": str(row["revoked_at"]) if row["revoked_at"] is not None else None,
                }
                for row in rows
            ]

    def revoke_connection(self, grant_id: str, admin_user_id: int) -> dict[str, Any]:
        if not _GRANT_ID.fullmatch(grant_id):
            raise MCPOAuthError("OAuth connection not found.", status_code=404)
        with self._connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._require_current_admin(db, admin_user_id)
                row = db.execute(
                    """SELECT g.grant_id,g.revoked_at,c.client_name
                       FROM mcp_oauth_grants g JOIN mcp_oauth_clients c ON c.client_id=g.client_id
                       WHERE g.grant_id=?""",
                    (grant_id,),
                ).fetchone()
                if row is None:
                    raise MCPOAuthError("OAuth connection not found.", status_code=404)
                if row["revoked_at"] is None:
                    db.execute(
                        "UPDATE mcp_oauth_grants SET revoked_at=CURRENT_TIMESTAMP WHERE grant_id=?",
                        (grant_id,),
                    )
                db.commit()
                return {"id": str(row["grant_id"]), "application_name": str(row["client_name"])}
            except Exception:
                db.rollback()
                raise

    def disable_client(self, client_id: str) -> None:
        """Administrative kill switch for an application registration."""
        with self._connection() as db, db:
            db.execute(
                "UPDATE mcp_oauth_clients SET disabled_at=COALESCE(disabled_at,CURRENT_TIMESTAMP) WHERE client_id=?",
                (client_id,),
            )

    def revoke_all_grants(self) -> int:
        with self._connection() as db, db:
            cursor = db.execute(
                "UPDATE mcp_oauth_grants SET revoked_at=CURRENT_TIMESTAMP WHERE revoked_at IS NULL"
            )
            return max(cursor.rowcount, 0)

    async def authorize(self, client: LynewrightOAuthClient, params: AuthorizationParams) -> str:
        try:
            request_id = self.create_authorization_request(client, params)
        except MCPOAuthError as exc:
            raise AuthorizeError("invalid_request", exc.message) from exc
        return f"{self.issuer_url.rstrip('/')}/mcp-connect?request={request_id}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        raw_redirect = _TOKEN_REDIRECT_URI.get()
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM mcp_oauth_codes WHERE code_hash=?",
                (_token_hash(authorization_code),),
            ).fetchone()
            if row is None or row["consumed_at_epoch"] is not None:
                return None
            if float(row["expires_at_epoch"]) <= time.time() or str(row["client_id"]) != str(client.client_id):
                return None
            if raw_redirect is not None and not _redirect_matches(raw_redirect, str(row["redirect_uri"])):
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=_json_list(str(row["scopes_json"])),
                expires_at=float(row["expires_at_epoch"]),
                client_id=str(row["client_id"]),
                code_challenge=str(row["code_challenge"]),
                redirect_uri=AnyUrl(str(row["redirect_uri"])),
                redirect_uri_provided_explicitly=True,
                resource=str(row["resource"]),
                subject=str(row["user_id"]),
            )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        now = time.time()
        code_hash = _token_hash(authorization_code.code)
        raw_redirect = _TOKEN_REDIRECT_URI.get()
        token_error: tuple[str, str] | None = None
        with self._connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM mcp_oauth_codes WHERE code_hash=?",
                    (code_hash,),
                ).fetchone()
                if (
                    row is None
                    or row["consumed_at_epoch"] is not None
                    or float(row["expires_at_epoch"]) <= now
                    or str(row["client_id"]) != str(client.client_id)
                    or str(row["client_id"]) != authorization_code.client_id
                    or str(row["code_challenge"]) != authorization_code.code_challenge
                    or str(row["resource"]) != self.resource_url
                    or (raw_redirect is not None and not _redirect_matches(raw_redirect, str(row["redirect_uri"])))
                ):
                    raise TokenError("invalid_grant", "The authorization code is invalid or expired.")
                grant = db.execute(
                    """SELECT g.*,c.disabled_at,u.role,u.is_active
                       FROM mcp_oauth_grants g
                       JOIN mcp_oauth_clients c ON c.client_id=g.client_id
                       LEFT JOIN users u ON u.id=g.admin_user_id
                       WHERE g.grant_id=?""",
                    (str(row["grant_id"]),),
                ).fetchone()
                if not self._row_is_live_grant(grant):
                    raise TokenError("invalid_grant", "The authorization grant is no longer active.")
                if set(_json_list(str(row["scopes_json"]))) != set(authorization_code.scopes):
                    raise TokenError("invalid_grant", "The authorization code is invalid.")
                updated = db.execute(
                    """UPDATE mcp_oauth_codes SET consumed_at_epoch=?
                       WHERE code_hash=? AND consumed_at_epoch IS NULL AND expires_at_epoch>?""",
                    (now, code_hash, now),
                )
                if updated.rowcount != 1:
                    raise TokenError("invalid_grant", "The authorization code has already been used.")
                family_id = f"mcpfamily_{secrets.token_hex(16)}"
                absolute_expiry = now + REFRESH_FAMILY_TTL_SECONDS
                scopes = _json_list(str(row["scopes_json"]))
                access = _new_secret(OAUTH_ACCESS_TOKEN_PREFIX)
                refresh = _new_secret(OAUTH_REFRESH_TOKEN_PREFIX)
                db.execute(
                    """INSERT INTO mcp_oauth_refresh_families
                       (family_id,grant_id,client_id,resource,created_at_epoch,expires_at_epoch)
                       VALUES (?,?,?,?,?,?)""",
                    (family_id, str(row["grant_id"]), str(row["client_id"]), self.resource_url, now, absolute_expiry),
                )
                self._insert_access_token(db, access, str(row["grant_id"]), family_id, scopes, now)
                db.execute(
                    """INSERT INTO mcp_oauth_refresh_tokens
                       (token_hash,family_id,scopes_json,created_at_epoch)
                       VALUES (?,?,?,?)""",
                    (_token_hash(refresh), family_id, json.dumps(scopes), now),
                )
                db.commit()
                return OAuthToken(
                    access_token=access, refresh_token=refresh,
                    expires_in=ACCESS_TOKEN_TTL_SECONDS, scope=" ".join(scopes),
                )
            except TokenError as exc:
                db.rollback()
                token_error = (exc.error, exc.error_description)
            except Exception:
                db.rollback()
                raise
        if token_error is not None:
            # The MCP SDK's errors are frozen dataclasses. Raising them through
            # the PlatformStore generator context manager makes contextlib try
            # to assign __traceback__, masking the protocol error. Recreate and
            # raise only after the SQLite context has closed.
            raise TokenError(*token_error)
        raise TokenError("server_error", "The authorization code could not be exchanged.")

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        token_hash = _token_hash(refresh_token)
        now = time.time()
        with self._connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """SELECT r.token_hash,r.family_id,r.scopes_json,r.consumed_at_epoch,
                              f.client_id,f.expires_at_epoch,f.revoked_at_epoch,
                              g.admin_user_id,g.revoked_at,c.disabled_at,u.role,u.is_active
                       FROM mcp_oauth_refresh_tokens r
                       JOIN mcp_oauth_refresh_families f ON f.family_id=r.family_id
                       JOIN mcp_oauth_grants g ON g.grant_id=f.grant_id
                       JOIN mcp_oauth_clients c ON c.client_id=f.client_id
                       LEFT JOIN users u ON u.id=g.admin_user_id
                       WHERE r.token_hash=?""",
                    (token_hash,),
                ).fetchone()
                if row is None:
                    db.commit()
                    return None
                if row["consumed_at_epoch"] is not None:
                    self._revoke_family(db, str(row["family_id"]), now)
                    db.commit()
                    return None
                if (
                    str(row["client_id"]) != str(client.client_id)
                    or float(row["expires_at_epoch"]) <= now
                    or row["revoked_at_epoch"] is not None
                    or row["revoked_at"] is not None
                    or row["disabled_at"] is not None
                    or not bool(row["is_active"])
                    or str(row["role"]) != "admin"
                ):
                    db.commit()
                    return None
                scopes = _json_list(str(row["scopes_json"]))
                db.commit()
                return RefreshToken(
                    token=refresh_token,
                    client_id=str(row["client_id"]),
                    scopes=scopes,
                    expires_at=int(row["expires_at_epoch"]),
                    subject=str(row["admin_user_id"]),
                )
            except Exception:
                db.rollback()
                raise

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        now = time.time()
        token_hash = _token_hash(refresh_token.token)
        token_error: tuple[str, str] | None = None
        with self._connection() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """SELECT r.*,f.grant_id,f.client_id,f.resource,f.expires_at_epoch,f.revoked_at_epoch,
                              g.approved_scopes_json,g.revoked_at,c.disabled_at,u.role,u.is_active
                       FROM mcp_oauth_refresh_tokens r
                       JOIN mcp_oauth_refresh_families f ON f.family_id=r.family_id
                       JOIN mcp_oauth_grants g ON g.grant_id=f.grant_id
                       JOIN mcp_oauth_clients c ON c.client_id=f.client_id
                       LEFT JOIN users u ON u.id=g.admin_user_id
                       WHERE r.token_hash=?""",
                    (token_hash,),
                ).fetchone()
                if row is None:
                    raise TokenError("invalid_grant", "The refresh token is invalid.")
                if row["consumed_at_epoch"] is not None:
                    self._revoke_family(db, str(row["family_id"]), now)
                    db.commit()
                    raise TokenError("invalid_grant", "Refresh token reuse revoked the token family.")
                if (
                    str(row["client_id"]) != str(client.client_id)
                    or float(row["expires_at_epoch"]) <= now
                    or row["revoked_at_epoch"] is not None
                    or row["revoked_at"] is not None
                    or row["disabled_at"] is not None
                    or not bool(row["is_active"])
                    or str(row["role"]) != "admin"
                    or str(row["resource"]) != self.resource_url
                ):
                    raise TokenError("invalid_grant", "The refresh grant is no longer active.")
                requested = set(scopes)
                prior = set(_json_list(str(row["scopes_json"])))
                ceiling = set(_json_list(str(row["approved_scopes_json"])))
                if not requested or not requested.issubset(prior) or not requested.issubset(ceiling):
                    raise TokenError("invalid_scope", "Refresh requests may only narrow existing permissions.")
                consumed = db.execute(
                    """UPDATE mcp_oauth_refresh_tokens SET consumed_at_epoch=?
                       WHERE token_hash=? AND consumed_at_epoch IS NULL""",
                    (now, token_hash),
                )
                if consumed.rowcount != 1:
                    self._revoke_family(db, str(row["family_id"]), now)
                    db.commit()
                    raise TokenError("invalid_grant", "Refresh token reuse revoked the token family.")
                access = _new_secret(OAUTH_ACCESS_TOKEN_PREFIX)
                next_refresh = _new_secret(OAUTH_REFRESH_TOKEN_PREFIX)
                scope_list = sorted(requested)
                self._insert_access_token(
                    db, access, str(row["grant_id"]), str(row["family_id"]), scope_list, now,
                )
                db.execute(
                    """INSERT INTO mcp_oauth_refresh_tokens
                       (token_hash,family_id,scopes_json,created_at_epoch)
                       VALUES (?,?,?,?)""",
                    (
                        _token_hash(next_refresh), str(row["family_id"]),
                        json.dumps(scope_list), now,
                    ),
                )
                expires_in = max(0, min(ACCESS_TOKEN_TTL_SECONDS, int(float(row["expires_at_epoch"]) - now)))
                db.commit()
                return OAuthToken(
                    access_token=access, refresh_token=next_refresh,
                    expires_in=expires_in, scope=" ".join(scope_list),
                )
            except TokenError as exc:
                db.rollback()
                token_error = (exc.error, exc.error_description)
            except Exception:
                db.rollback()
                raise
        if token_error is not None:
            raise TokenError(*token_error)
        raise TokenError("server_error", "The refresh token could not be exchanged.")

    async def load_access_token(self, token: str) -> AccessToken | None:
        if not token.startswith(OAUTH_ACCESS_TOKEN_PREFIX):
            return None
        now = time.time()
        with self._connection() as db:
            row = db.execute(
                """SELECT t.*,g.actor_id,g.client_id,g.admin_user_id,g.approved_scopes_json,
                          g.revoked_at,c.client_name,c.disabled_at,u.role,u.is_active,
                          f.revoked_at_epoch AS family_revoked,
                          f.expires_at_epoch AS family_expires_at
                   FROM mcp_oauth_access_tokens t
                   JOIN mcp_oauth_grants g ON g.grant_id=t.grant_id
                   JOIN mcp_oauth_clients c ON c.client_id=g.client_id
                   JOIN users u ON u.id=g.admin_user_id
                   JOIN mcp_oauth_refresh_families f ON f.family_id=t.family_id
                   WHERE t.token_hash=?""",
                (_token_hash(token),),
            ).fetchone()
            if (
                row is None
                or row["revoked_at_epoch"] is not None
                or row["family_revoked"] is not None
                or row["revoked_at"] is not None
                or row["disabled_at"] is not None
                or not bool(row["is_active"])
                or str(row["role"]) != "admin"
                or float(row["expires_at_epoch"]) <= now
                or float(row["family_expires_at"]) <= now
                or str(row["resource"]) != self.resource_url
            ):
                return None
            scopes = sorted(set(_json_list(str(row["scopes_json"]))) & set(_json_list(str(row["approved_scopes_json"]))))
            if not scopes:
                return None
            return AccessToken(
                token=token,
                # The SDK compares this field with the registered OAuth
                # application's client_id during token revocation. Grant actor
                # identity remains server-side and is resolved from the token.
                client_id=str(row["client_id"]),
                scopes=scopes,
                expires_at=int(row["expires_at_epoch"]),
                resource=str(row["resource"]),
                subject=str(row["admin_user_id"]),
                claims={
                    "iss": self.issuer_url,
                    "aud": self.resource_url,
                    "mcp_oauth_grant_id": str(row["grant_id"]),
                    "mcp_oauth_client_id": str(row["client_id"]),
                    "mcp_oauth_subject": str(row["admin_user_id"]),
                },
            )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        raw = token.token
        if not raw.startswith((OAUTH_ACCESS_TOKEN_PREFIX, OAUTH_REFRESH_TOKEN_PREFIX)):
            return
        now = time.time()
        with self._connection() as db, db:
            if raw.startswith(OAUTH_ACCESS_TOKEN_PREFIX):
                row = db.execute(
                    "SELECT family_id FROM mcp_oauth_access_tokens WHERE token_hash=?",
                    (_token_hash(raw),),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT family_id FROM mcp_oauth_refresh_tokens WHERE token_hash=?",
                    (_token_hash(raw),),
                ).fetchone()
            if row is not None:
                self._revoke_family(db, str(row["family_id"]), now)

    def revoke_token_for_client(self, raw_token: str, client_id: str) -> bool:
        """Revoke a token family only when the database binds it to this client."""
        now = time.time()
        with self._connection() as db, db:
            if raw_token.startswith(OAUTH_ACCESS_TOKEN_PREFIX):
                row = db.execute(
                    """SELECT t.family_id,g.client_id
                       FROM mcp_oauth_access_tokens t
                       JOIN mcp_oauth_grants g ON g.grant_id=t.grant_id
                       WHERE t.token_hash=?""",
                    (_token_hash(raw_token),),
                ).fetchone()
            elif raw_token.startswith(OAUTH_REFRESH_TOKEN_PREFIX):
                row = db.execute(
                    """SELECT r.family_id,f.client_id
                       FROM mcp_oauth_refresh_tokens r
                       JOIN mcp_oauth_refresh_families f ON f.family_id=r.family_id
                       WHERE r.token_hash=?""",
                    (_token_hash(raw_token),),
                ).fetchone()
            else:
                return False
            if row is None or str(row["client_id"]) != client_id:
                return False
            self._revoke_family(db, str(row["family_id"]), now)
            return True

    def actor_for_access_token(self, token: AccessToken) -> CapabilityActor:
        now = time.time()
        with self._connection() as db:
            row = db.execute(
                """SELECT t.scopes_json,t.resource,t.expires_at_epoch,t.revoked_at_epoch,
                          t.family_id,g.grant_id,g.actor_id,g.client_id,g.admin_user_id,
                          g.approved_scopes_json,g.revoked_at,c.client_name,c.disabled_at,
                          u.role,u.is_active,u.email AS actor_email,f.revoked_at_epoch AS family_revoked,
                          f.expires_at_epoch AS family_expiry
                   FROM mcp_oauth_access_tokens t
                   JOIN mcp_oauth_grants g ON g.grant_id=t.grant_id
                   JOIN mcp_oauth_clients c ON c.client_id=g.client_id
                   JOIN users u ON u.id=g.admin_user_id
                   JOIN mcp_oauth_refresh_families f ON f.family_id=t.family_id
                   WHERE t.token_hash=?""",
                (_token_hash(token.token),),
            ).fetchone()
            if (
                row is None
                or row["revoked_at_epoch"] is not None
                or row["family_revoked"] is not None
                or row["revoked_at"] is not None
                or row["disabled_at"] is not None
                or not bool(row["is_active"])
                or str(row["role"]) != "admin"
                or float(row["expires_at_epoch"]) <= now
                or float(row["family_expiry"]) <= now
                or str(row["resource"]) != self.resource_url
            ):
                raise ValueError("MCP OAuth authorization is no longer active.")
            effective = set(_json_list(str(row["scopes_json"])))
            effective.intersection_update(_json_list(str(row["approved_scopes_json"])))
            effective.intersection_update(token.scopes)
            from app.services.tenant_schema import default_tenant_for_user

            tenancy_ready = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tenant_memberships'"
            ).fetchone() is not None
            tenant_id = default_tenant_for_user(db, int(row["admin_user_id"])) or "" if tenancy_ready else ""
            project_ids = tuple(
                str(project[0]) for project in db.execute(
                    "SELECT project_id FROM tenant_projects WHERE tenant_id=? AND status='active' ORDER BY created_at,project_id",
                    (tenant_id,),
                ).fetchall()
            ) if tenant_id else ()
            return CapabilityActor(
                client_id=str(row["actor_id"]),
                name=str(row["client_name"]),
                client_type="mcp-oauth",
                permission_mode="custom",
                granted_capabilities=frozenset(effective),
                tenant_id=tenant_id,
                project_ids=project_ids,
                on_behalf_of=str(row["actor_email"] or ""),
            )

    def _insert_access_token(
        self,
        db: sqlite3.Connection,
        token: str,
        grant_id: str,
        family_id: str,
        scopes: list[str],
        now: float,
    ) -> None:
        db.execute(
            """INSERT INTO mcp_oauth_access_tokens
               (token_hash,grant_id,family_id,scopes_json,resource,created_at_epoch,expires_at_epoch)
               VALUES (?,?,?,?,?,?,?)""",
            (
                _token_hash(token), grant_id, family_id, json.dumps(sorted(scopes)),
                self.resource_url, now, now + ACCESS_TOKEN_TTL_SECONDS,
            ),
        )

    @staticmethod
    def _revoke_family(db: sqlite3.Connection, family_id: str, now: float) -> None:
        db.execute(
            """UPDATE mcp_oauth_refresh_families SET revoked_at_epoch=COALESCE(revoked_at_epoch,?)
               WHERE family_id=?""",
            (now, family_id),
        )

    @staticmethod
    def _row_is_live_grant(row: sqlite3.Row | None) -> bool:
        return bool(
            row is not None
            and row["revoked_at"] is None
            and row["disabled_at"] is None
            and row["is_active"]
            and str(row["role"]) == "admin"
        )

    @staticmethod
    def _require_current_admin(db: sqlite3.Connection, user_id: int) -> None:
        row = db.execute("SELECT role,is_active FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None or not bool(row["is_active"]) or str(row["role"]) != "admin" or user_id <= 0:
            raise MCPOAuthError("An active platform administrator is required.", status_code=403)

    @staticmethod
    def _admin_email(db: sqlite3.Connection, user_id: int) -> str:
        row = db.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise MCPOAuthError("An active platform administrator is required.", status_code=403)
        return str(row["email"])

    @staticmethod
    def _purge_stale(db: sqlite3.Connection) -> None:
        now = time.time()
        # Keep cleanup bounded on public endpoints. Raw bearer values and codes
        # are never stored, so deleted rows cannot reveal reusable credentials.
        for table, predicate, args in (
            ("mcp_oauth_requests", "expires_at_epoch<? AND status IN ('pending','expired','denied','approved')", (now - 86400,)),
            ("mcp_oauth_codes", "expires_at_epoch<?", (now - 86400,)),
            ("mcp_oauth_access_tokens", "expires_at_epoch<?", (now - 86400,)),
            ("mcp_oauth_refresh_tokens", "family_id IN (SELECT family_id FROM mcp_oauth_refresh_families WHERE expires_at_epoch<? OR revoked_at_epoch IS NOT NULL)", (now - 86400,)),
            ("mcp_oauth_refresh_families", "expires_at_epoch<? OR revoked_at_epoch IS NOT NULL", (now - 86400,)),
        ):
            db.execute(
                f"DELETE FROM {table} WHERE rowid IN "
                f"(SELECT rowid FROM {table} WHERE {predicate} LIMIT 200)",
                args,
            )


class MCPAuthorizationProvider:
    """SDK provider adapter that also verifies the retained qxc_ token format."""

    def __init__(self, oauth: MCPOAuthService, legacy_clients: Any) -> None:
        self.oauth = oauth
        self.legacy_clients = legacy_clients

    async def get_client(self, client_id: str) -> LynewrightOAuthClient | None:
        return self.oauth.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.oauth.register_client(client_info)

    async def authorize(self, client: LynewrightOAuthClient, params: AuthorizationParams) -> str:
        try:
            return await self.oauth.authorize(client, params)
        except AuthorizeError:
            raise

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        return await self.oauth.load_authorization_code(client, authorization_code)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        return await self.oauth.exchange_authorization_code(client, authorization_code)

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        return await self.oauth.load_refresh_token(client, refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return await self.oauth.exchange_refresh_token(client, refresh_token, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token.startswith("qxc_"):
            try:
                actor = self.legacy_clients.authenticate(token)
            except Exception:
                return None
            from app.services.platform_capabilities import allowed_capabilities
            return AccessToken(
                token=token,
                client_id=actor.client_id,
                scopes=sorted(allowed_capabilities(actor)),
                subject=actor.client_id,
            )
        return await self.oauth.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self.oauth.revoke_token(token)


class PublicClientRevocationRequest(BaseModel):
    """RFC 7009 form for public clients; a client secret is not required."""

    token: str
    token_type_hint: Literal["access_token", "refresh_token"] | None = None
    client_id: str
    client_secret: str | None = None


class MCPClientRevocationHandler:
    """SDK-authenticated revocation with a public-client-compatible request model."""

    _NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

    def __init__(self, provider: MCPAuthorizationProvider) -> None:
        from mcp.server.auth.middleware.client_auth import ClientAuthenticator

        self.provider = provider
        self.client_authenticator = ClientAuthenticator(provider)

    async def handle(self, request: Request) -> Response:
        from mcp.server.auth.errors import stringify_pydantic_error
        from mcp.server.auth.middleware.client_auth import AuthenticationError

        if request.method == "OPTIONS":
            return Response(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type",
                    "Cache-Control": "no-store",
                },
            )
        try:
            client = await self.client_authenticator.authenticate_request(request)
        except AuthenticationError as exc:
            return JSONResponse(
                {"error": "unauthorized_client", "error_description": exc.message},
                status_code=401,
                headers=self._NO_STORE,
            )

        try:
            form_data = await request.form()
            revocation_request = PublicClientRevocationRequest.model_validate(dict(form_data))
        except ValidationError as exc:
            return JSONResponse(
                {"error": "invalid_request", "error_description": stringify_pydantic_error(exc)},
                status_code=400,
                headers=self._NO_STORE,
            )
        except ValueError:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "The revocation request is invalid."},
                status_code=400,
                headers=self._NO_STORE,
            )

        # The token value is digest-matched to its grant/family and ownership is
        # checked in the same transaction as revocation. This also lets an
        # expired access token revoke its still-live refresh family.
        self.provider.oauth.revoke_token_for_client(
            revocation_request.token,
            client.client_id,
        )

        # RFC 7009 intentionally does not disclose whether a token exists or
        # belongs to another client.
        return Response(status_code=200, headers=self._NO_STORE)


class MCPOAuthRequestGuard:
    """Rate limits, bounds bodies, rejects duplicate protocol fields, and preserves raw URIs."""

    _LIMITS = {
        "/register": ("mcp_oauth_register", 10),
        "/authorize": ("mcp_oauth_authorize", 30),
        "/token": ("mcp_oauth_token", 60),
        "/revoke": ("mcp_oauth_revoke", 60),
    }
    _SINGLE_AUTH_FIELDS = frozenset({
        "client_id", "redirect_uri", "response_type", "response_mode",
        "code_challenge", "code_challenge_method", "state", "scope", "resource",
    })
    _SINGLE_TOKEN_FIELDS = frozenset({
        "grant_type", "client_id", "code", "code_verifier", "redirect_uri",
        "refresh_token", "scope", "resource",
    })
    _SINGLE_REVOCATION_FIELDS = frozenset({"token", "token_type_hint", "client_id", "client_secret"})

    def __init__(
        self,
        app: Any,
        *,
        issuer_url: str,
        resource_url: str,
        oauth_enabled: bool = True,
    ) -> None:
        self.app = app
        self.issuer_url = issuer_url
        self.resource_url = resource_url
        self.oauth_enabled = oauth_enabled

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if not self.oauth_enabled and path in self._LIMITS:
            await self.app(scope, receive, send)
            return
        namespace_limit = self._LIMITS.get(path)
        if namespace_limit is not None:
            from app.services.rate_limiter import RateLimiterUnavailable, get_rate_limiter
            address = str((scope.get("client") or ("unknown",))[0])
            namespace, maximum = namespace_limit
            try:
                allowed = get_rate_limiter().consume(
                    namespace=namespace, key=address, maximum=maximum, window_seconds=60,
                )
            except RateLimiterUnavailable:
                await self._reply(send, 503, "temporarily_unavailable", "OAuth request processing is unavailable.")
                return
            if not allowed:
                await self._reply(send, 429, "rate_limited", "Too many OAuth requests.", retry_after="60")
                return

        query = scope.get("query_string", b"")
        if path == "/authorize" and len(query) > 8192:
            await self._reply(send, 400, "invalid_request", "The authorization request is too large.")
            return

        body = b""
        replay: list[dict[str, Any]] = []
        if path in {"/register", "/authorize", "/token", "/revoke"} and scope.get("method") == "POST":
            content_length = next(
                (value.decode("latin1") for key, value in scope.get("headers", []) if key.lower() == b"content-length"),
                "",
            )
            try:
                if content_length and int(content_length) > 32768:
                    await self._reply(send, 413, "invalid_request", "The OAuth request body is too large.")
                    return
            except ValueError:
                await self._reply(send, 400, "invalid_request", "The content length is invalid.")
                return
            chunks: list[bytes] = []
            total = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > 32768:
                    await self._reply(send, 413, "invalid_request", "The OAuth request body is too large.")
                    return
                chunks.append(chunk)
                replay.append(message)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)

        tokens: list[tuple[ContextVar[Any], Any]] = []
        raw_params: dict[str, list[str]] = {}
        if path == "/authorize":
            if scope.get("method") == "GET":
                raw_params = parse_qs(query.decode("utf-8", "replace"), keep_blank_values=True)
            elif scope.get("method") == "POST":
                raw_params = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            if any(len(raw_params.get(key, [])) > 1 for key in self._SINGLE_AUTH_FIELDS):
                await self._reply(send, 400, "invalid_request", "Duplicate authorization parameters are not allowed.")
                return
            tokens.append((_AUTHORIZATION_REDIRECT_URI, raw_params.get("redirect_uri", [None])[0]))
        elif path == "/register" and body:
            try:
                registration = json.loads(body)
                redirects = registration.get("redirect_uris") if isinstance(registration, dict) else None
                if redirects is not None and (
                    not isinstance(redirects, list)
                    or any(not isinstance(uri, str) for uri in redirects)
                ):
                    raise ValueError
                tokens.append((_REGISTER_REDIRECT_URIS, redirects))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                await self._reply(send, 400, "invalid_client_metadata", "The registration request is invalid.")
                return
        elif path == "/token":
            raw_params = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            if any(len(raw_params.get(key, [])) > 1 for key in self._SINGLE_TOKEN_FIELDS):
                await self._reply(send, 400, "invalid_request", "Duplicate token parameters are not allowed.")
                return
            if raw_params.get("resource", [None])[0] != self.resource_url:
                await self._reply(send, 400, "invalid_request", "The token request must target this MCP resource.")
                return
            if raw_params.get("grant_type", [""])[0] == "authorization_code":
                verifier = raw_params.get("code_verifier", [""])[0]
                if not _PKCE_VERIFIER.fullmatch(verifier):
                    await self._reply(send, 400, "invalid_request", "A valid PKCE verifier is required.")
                    return
            tokens.append((_TOKEN_REDIRECT_URI, raw_params.get("redirect_uri", [None])[0]))
        elif path == "/revoke":
            raw_params = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            if any(len(raw_params.get(key, [])) > 1 for key in self._SINGLE_REVOCATION_FIELDS):
                await self._reply(send, 400, "invalid_request", "Duplicate revocation parameters are not allowed.")
                return

        token_values = [(var, var.set(value)) for var, value in tokens]

        async def replay_receive() -> dict[str, Any]:
            if replay:
                return replay.pop(0)
            return await receive()

        async def scoped_send(message: dict[str, Any]) -> None:
            if path == "/authorize" and message.get("type") == "http.response.start" and message.get("status") == 302:
                requested_redirect = _AUTHORIZATION_REDIRECT_URI.get()
                if requested_redirect:
                    headers = list(message.get("headers", []))
                    for index, (key, value) in enumerate(headers):
                        if key.lower() != b"location":
                            continue
                        location = value.decode("latin1")
                        if not _is_oauth_callback_location(location, requested_redirect):
                            break
                        parsed = urlsplit(location)
                        params = parse_qsl(parsed.query, keep_blank_values=True)
                        if not any(name == "iss" for name, _item in params):
                            params.append(("iss", self.issuer_url))
                            location = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))
                            headers[index] = (key, location.encode("latin1"))
                            message = {**message, "headers": headers}
                        break
            if path == "/mcp" and message.get("type") == "http.response.start" and message.get("status") == 401:
                headers = list(message.get("headers", []))
                for index, (key, value) in enumerate(headers):
                    if key.lower() == b"www-authenticate":
                        challenge = value.decode("latin1")
                        if "scope=" not in challenge:
                            headers[index] = (key, f'{challenge}, scope="system:read"'.encode("latin1"))
                        break
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, replay_receive, scoped_send)
        finally:
            for var, token in reversed(token_values):
                var.reset(token)

    @staticmethod
    async def _reply(
        send: Any,
        status_code: int,
        error: str,
        description: str,
        *,
        retry_after: str | None = None,
    ) -> None:
        body = json.dumps({"error": error, "error_description": description}).encode("utf-8")
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store"), (b"content-length", str(len(body)).encode())]
        if retry_after is not None:
            headers.append((b"retry-after", retry_after.encode()))
        await send({"type": "http.response.start", "status": status_code, "headers": headers})
        await send({"type": "http.response.body", "body": body})
