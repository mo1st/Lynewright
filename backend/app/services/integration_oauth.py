"""Provider-neutral OAuth authorization-code and refresh primitives."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_sdk import IntegrationSDK
from app.services.platform_services import PlatformNotFoundError, PlatformOperationError, PlatformStore, PlatformValidationError
from app.services.secret_store import decrypt_secret, encrypt_secret


@dataclass(frozen=True, slots=True)
class OAuthConfiguration:
    authorization_url: str
    token_url: str
    client_id_ref: str
    client_secret_ref: str
    scopes: tuple[str, ...] = ()
    use_pkce: bool = True


class OAuthService:
    """OAuth mechanics only; provider-specific parameters stay in adapters."""

    def __init__(
        self,
        store: PlatformStore,
        connections: ConnectionProfileService,
        *,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.store = store
        self.connections = connections
        self.now = now or time.time

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_integration_oauth_states (
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                verifier_encrypted TEXT NOT NULL DEFAULT '',
                redirect_uri TEXT NOT NULL,
                expires_at_epoch INTEGER NOT NULL,
                consumed_at_epoch INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, integration_id, connection_id, state_hash)
            );
            CREATE TABLE IF NOT EXISTS platform_integration_oauth_tokens (
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                access_credential_ref TEXT NOT NULL,
                refresh_credential_ref TEXT NOT NULL DEFAULT '',
                token_type TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT '',
                expires_at_epoch INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, integration_id, connection_id)
            );
            """
        )

    def begin_authorization(
        self,
        sdk: IntegrationSDK,
        config: OAuthConfiguration,
        *,
        redirect_uri: str,
        connection_id: str | None = None,
        extra_parameters: dict[str, str] | None = None,
        state_ttl_seconds: int = 600,
    ) -> dict[str, str | int]:
        connection = self._connection_profile(sdk, connection_id)
        if not 60 <= state_ttl_seconds <= 3600:
            raise PlatformValidationError("OAuth state TTL must be between 60 seconds and one hour.")
        sdk.http.validate_url(config.authorization_url)
        sdk.http.validate_url(config.token_url)
        sdk.http.validate_url(redirect_uri)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64) if config.use_pkce else ""
        now = int(self.now())
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_integration_oauth_states
                (tenant_id,integration_id,connection_id,state_hash,verifier_encrypted,redirect_uri,expires_at_epoch)
                VALUES (?,?,?,?,?,?,?)""",
                (self.connections.tenant_id, sdk.integration_id, connection["id"], self._hash(state), encrypt_secret(verifier), redirect_uri, now + state_ttl_seconds),
            )
        def build_url(client_id: str) -> str:
            parameters = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state}
            if config.scopes:
                parameters["scope"] = " ".join(config.scopes)
            if verifier:
                parameters["code_challenge"] = self._challenge(verifier)
                parameters["code_challenge_method"] = "S256"
            parameters.update(extra_parameters or {})
            return self._append_query(config.authorization_url, parameters)

        authorization_url = sdk.credentials.use(config.client_id_ref, build_url)
        return {"authorization_url": authorization_url, "state": state, "expires_at": now + state_ttl_seconds}

    def complete_callback(
        self,
        sdk: IntegrationSDK,
        config: OAuthConfiguration,
        *,
        state: str,
        code: str,
        access_credential_ref: str,
        refresh_credential_ref: str = "",
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        if not code:
            raise PlatformValidationError("OAuth authorization code is required.")
        connection = self._connection_profile(sdk, connection_id)
        pending = self._consume_state(sdk, connection["id"], state)
        form = {
            "grant_type": "authorization_code", "code": code, "redirect_uri": pending["redirect_uri"],
        }
        verifier = decrypt_secret(pending["verifier_encrypted"])
        if verifier:
            form["code_verifier"] = verifier
        payload = self._token_request(sdk, config, form)
        return self._store_tokens(
            sdk, connection["id"], payload, access_credential_ref=access_credential_ref,
            refresh_credential_ref=refresh_credential_ref,
        )

    def refresh(
        self,
        sdk: IntegrationSDK,
        config: OAuthConfiguration,
        *,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        connection = self._connection_profile(sdk, connection_id)
        token = self.token_status(sdk, connection_id=connection["id"])
        refresh_ref = str(token["refresh_credential_ref"] or "")
        if not refresh_ref:
            raise PlatformOperationError("OAuth connection has no refresh credential.")

        def with_refresh(refresh_token: str) -> dict[str, Any]:
            payload = self._token_request(sdk, config, {"grant_type": "refresh_token", "refresh_token": refresh_token})
            return self._store_tokens(
                sdk, connection["id"], payload,
                access_credential_ref=str(token["access_credential_ref"]),
                refresh_credential_ref=refresh_ref,
            )

        return sdk.credentials.use(refresh_ref, with_refresh)

    def token_status(self, sdk: IntegrationSDK, *, connection_id: str | None = None) -> dict[str, Any]:
        connection = self._connection_profile(sdk, connection_id)
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM platform_integration_oauth_tokens WHERE tenant_id=? AND integration_id=? AND connection_id=?",
                (self.connections.tenant_id, sdk.integration_id, connection["id"]),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("OAuth token metadata not found.")
        result = dict(row)
        result["expired"] = result["expires_at_epoch"] is not None and int(result["expires_at_epoch"]) <= int(self.now())
        return result

    def _consume_state(self, sdk: IntegrationSDK, connection_id: str, state: str) -> sqlite3.Row:
        now = int(self.now())
        with self._connection() as db, db:
            row = db.execute(
                """SELECT * FROM platform_integration_oauth_states WHERE tenant_id=? AND integration_id=? AND connection_id=?
                AND state_hash=?""",
                (self.connections.tenant_id, sdk.integration_id, connection_id, self._hash(state)),
            ).fetchone()
            if row is None or row["consumed_at_epoch"] is not None or int(row["expires_at_epoch"]) < now:
                raise PlatformOperationError("OAuth state is invalid, expired, or already consumed.")
            if db.execute(
                """UPDATE platform_integration_oauth_states SET consumed_at_epoch=? WHERE tenant_id=? AND integration_id=?
                AND connection_id=? AND state_hash=? AND consumed_at_epoch IS NULL""",
                (now, self.connections.tenant_id, sdk.integration_id, connection_id, self._hash(state)),
            ).rowcount != 1:
                raise PlatformOperationError("OAuth state is invalid, expired, or already consumed.")
        return row

    def _token_request(self, sdk: IntegrationSDK, config: OAuthConfiguration, form: dict[str, str]) -> dict[str, Any]:
        def with_client_id(client_id: str) -> dict[str, Any]:
            def with_client_secret(client_secret: str) -> dict[str, Any]:
                body = {**form, "client_id": client_id, "client_secret": client_secret}
                response = sdk.http.request(
                    config.token_url, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"},
                    content=urlencode(body), max_bytes=1_000_000, timeout_seconds=20,
                )
                if not 200 <= int(response["status_code"]) < 300:
                    raise PlatformOperationError("OAuth token endpoint rejected the request.")
                try:
                    parsed = json.loads(bytes(response["body"]).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise PlatformOperationError("OAuth token endpoint returned invalid JSON.") from exc
                if not isinstance(parsed, dict) or not isinstance(parsed.get("access_token"), str) or not parsed["access_token"]:
                    raise PlatformOperationError("OAuth token endpoint returned no access token.")
                return parsed
            return sdk.credentials.use(config.client_secret_ref, with_client_secret)
        return sdk.credentials.use(config.client_id_ref, with_client_id)

    def _store_tokens(self, sdk: IntegrationSDK, connection_id: str, payload: dict[str, Any], *, access_credential_ref: str, refresh_credential_ref: str) -> dict[str, Any]:
        access = str(payload["access_token"])
        refresh = str(payload.get("refresh_token") or "")
        sdk.credentials.set(name=access_credential_ref, secret=access, kind="oauth-access-token")
        if refresh:
            if not refresh_credential_ref:
                raise PlatformValidationError("OAuth response included a refresh token but no refresh credential reference was supplied.")
            sdk.credentials.set(name=refresh_credential_ref, secret=refresh, kind="oauth-refresh-token")
        expires_in = payload.get("expires_in")
        expires_at = int(self.now()) + int(expires_in) if isinstance(expires_in, int) and expires_in >= 0 else None
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_integration_oauth_tokens
                (tenant_id,integration_id,connection_id,access_credential_ref,refresh_credential_ref,token_type,scope,expires_at_epoch)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,integration_id,connection_id)
                DO UPDATE SET access_credential_ref=excluded.access_credential_ref,refresh_credential_ref=excluded.refresh_credential_ref,
                token_type=excluded.token_type,scope=excluded.scope,expires_at_epoch=excluded.expires_at_epoch,updated_at=CURRENT_TIMESTAMP""",
                (self.connections.tenant_id, sdk.integration_id, connection_id, access_credential_ref, refresh_credential_ref,
                 str(payload.get("token_type") or ""), str(payload.get("scope") or ""), expires_at),
            )
        return self.token_status(sdk, connection_id=connection_id)

    def _connection_profile(self, sdk: IntegrationSDK, connection_id: str | None) -> dict[str, Any]:
        selected = connection_id or sdk.connection_id
        if not selected:
            raise PlatformOperationError("OAuth operations require a connection profile.")
        connection = self.connections.get(sdk.integration_id, selected)
        if not connection["enabled"]:
            raise PlatformOperationError("OAuth connection is disabled.")
        return connection

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _challenge(verifier: str) -> str:
        return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")

    @staticmethod
    def _append_query(url: str, parameters: dict[str, str]) -> str:
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.update(parameters)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
