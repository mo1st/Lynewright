"""Scoped credentials and queue operations for isolated integration runners."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
import secrets
import sqlite3
import time
from typing import Any

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_provenance import get_provenance
from app.services.integration_worker_invocations import IntegrationWorkerInvocationStore
from app.services.platform_services import (
    CredentialService,
    MediaService,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID


RUNNER_HEALTH_SECONDS = 45
MAX_CLAIM_WAIT_SECONDS = 30
MAX_RUNNER_ARTIFACT_BYTES = 250_000_000


def _contains_media_ref(value: Any, media_id: str) -> bool:
    if isinstance(value, dict):
        if str(value.get("media_id") or "") == media_id:
            return True
        return any(_contains_media_ref(item, media_id) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_media_ref(item, media_id) for item in value)
    if isinstance(value, str):
        for scheme in ("managed://", "lynewright-upload://"):
            if value.startswith(scheme) and value.removeprefix(scheme).split("?", 1)[0].strip() == media_id:
                return True
        return False
    return False


def _now() -> str:
    return datetime.now(UTC).isoformat()


class IntegrationRunnerService:
    """Runner tokens are hashed at rest and bind to one tenant and integration."""

    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = str(tenant_id or DEFAULT_TENANT_ID)

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS integration_runner_tokens (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                last_seen_at TEXT
            );
            CREATE INDEX IF NOT EXISTS integration_runner_tokens_scope
                ON integration_runner_tokens(tenant_id,integration_id,revoked_at);
            """
        )

    @staticmethod
    def token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_or_rotate(self, integration_id: str) -> dict[str, str]:
        selected = str(integration_id or "").strip()
        if not selected:
            raise PlatformValidationError("Integration id is required for a runner token.")
        provenance = get_provenance(self.store, self.tenant_id, selected)
        if provenance and provenance.get("status") == "awaiting_confirmation":
            raise PlatformOperationError("This outside package is awaiting admin confirmation; no runner token can be issued yet.")
        token = "crn_" + secrets.token_urlsafe(36)
        token_id = "rtn_" + secrets.token_urlsafe(12)
        with self.store.connection() as db, db:
            self.ensure_schema(db)
            db.execute(
                "UPDATE integration_runner_tokens SET revoked_at=? WHERE tenant_id=? AND integration_id=? AND revoked_at IS NULL",
                (_now(), self.tenant_id, selected),
            )
            db.execute(
                """INSERT INTO integration_runner_tokens
                   (id,token_hash,tenant_id,integration_id,created_at)
                   VALUES (?,?,?,?,?)""",
                (token_id, self.token_digest(token), self.tenant_id, selected, _now()),
            )
        return {"token": token, "token_id": token_id, "integration_id": selected}

    def revoke(self, integration_id: str) -> bool:
        with self.store.connection() as db, db:
            self.ensure_schema(db)
            changed = db.execute(
                """UPDATE integration_runner_tokens SET revoked_at=?
                   WHERE tenant_id=? AND integration_id=? AND revoked_at IS NULL""",
                (_now(), self.tenant_id, str(integration_id)),
            ).rowcount
        return bool(changed)

    def authenticate(self, token: str) -> dict[str, str] | None:
        digest = self.token_digest(str(token or ""))
        with self.store.connection() as db:
            self.ensure_schema(db)
            row = db.execute(
                """SELECT id,tenant_id,integration_id FROM integration_runner_tokens
                   WHERE token_hash=? AND revoked_at IS NULL""",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        provenance = get_provenance(self.store, str(row["tenant_id"]), str(row["integration_id"]))
        if provenance and provenance.get("status") == "awaiting_confirmation":
            return None
        return {
            "runner_id": str(row["id"]),
            "tenant_id": str(row["tenant_id"]),
            "integration_id": str(row["integration_id"]),
        }

    def _active(self, runner_id: str, integration_id: str) -> bool:
        with self.store.connection() as db:
            row = db.execute(
                """SELECT 1 FROM integration_runner_tokens WHERE id=? AND tenant_id=?
                   AND integration_id=? AND revoked_at IS NULL""",
                (runner_id, self.tenant_id, integration_id),
            ).fetchone()
        return row is not None

    def heartbeat(self, identity: dict[str, str]) -> dict[str, Any]:
        with self.store.connection() as db, db:
            self.ensure_schema(db)
            changed = db.execute(
                """UPDATE integration_runner_tokens SET last_seen_at=?
                   WHERE id=? AND tenant_id=? AND integration_id=? AND revoked_at IS NULL""",
                (_now(), identity["runner_id"], self.tenant_id, identity["integration_id"]),
            ).rowcount
        if changed != 1:
            raise PlatformOperationError("Runner token is revoked.")
        return self.status(identity["integration_id"])

    def status(self, integration_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            row = db.execute(
                """SELECT last_seen_at FROM integration_runner_tokens
                   WHERE tenant_id=? AND integration_id=? AND revoked_at IS NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (self.tenant_id, str(integration_id)),
            ).fetchone()
        last_seen = str(row["last_seen_at"] or "") if row else ""
        if not last_seen:
            state = "never_seen"
        else:
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(last_seen)).total_seconds()
                state = "connected" if age <= RUNNER_HEALTH_SECONDS else "stale"
            except ValueError:
                state = "stale"
        return {"status": state, "last_seen_at": last_seen or None}

    def claim(
        self, identity: dict[str, str], *, wait_seconds: float = 0,
    ) -> dict[str, Any] | None:
        wait = max(0.0, min(float(wait_seconds), MAX_CLAIM_WAIT_SECONDS))
        deadline = time.monotonic() + wait
        integration_id = identity["integration_id"]
        while True:
            if not self._active(identity["runner_id"], integration_id):
                raise PlatformOperationError("Runner token is revoked.")
            registry = IntegrationRegistry(self.store, tenant_id=self.tenant_id)
            integration = registry.get(integration_id)
            if not integration.get("enabled"):
                raise PlatformOperationError("Integration is disabled; runner claim refused.")
            queue = IntegrationWorkerInvocationStore(self.store, tenant_id=self.tenant_id)
            request = queue.claim_for_runner(
                integration_id=integration_id, runner_id=identity["runner_id"],
            )
            if request is not None:
                try:
                    return self._prepare_claim(integration, request)
                except Exception:
                    # Do not strand a lease if scoped connection/credential
                    # preparation fails. The error row contains no provider or
                    # credential details.
                    try:
                        queue.fail_for_runner(
                            str(request["id"]), integration_id=integration_id,
                            runner_id=identity["runner_id"], claim_token=str(request.get("claim_token") or ""),
                            error="Container runner claim configuration is invalid.",
                        )
                    except Exception:
                        pass
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.25, remaining))

    def _prepare_claim(self, integration: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        """Attach scoped non-secret settings and only declared/bound secrets."""
        context = request.get("_context") if isinstance(request.get("_context"), dict) else {}
        connection_id = str(context.get("connection_id") or "")
        project_id = str(context.get("project_id") or "")
        if context.get("tenant_id") and str(context["tenant_id"]) != self.tenant_id:
            raise PlatformOperationError("Runner invocation tenant scope mismatch.")
        if context.get("integration_id") and str(context["integration_id"]) != str(integration["id"]):
            raise PlatformOperationError("Runner invocation integration scope mismatch.")
        manifest = integration.get("manifest") if isinstance(integration.get("manifest"), dict) else {}
        declared = {str(value) for value in manifest.get("credential_refs", []) if isinstance(value, str)}
        credential_values: dict[str, str] = {}
        connection: dict[str, Any] | None = None
        internal_endpoint_url = ""
        if connection_id:
            connection = ConnectionProfileService(
                self.store, tenant_id=self.tenant_id, registry=IntegrationRegistry(self.store, tenant_id=self.tenant_id),
            ).get(str(integration["id"]), connection_id)
            if not connection.get("enabled"):
                raise PlatformOperationError("Integration connection is disabled; runner claim refused.")
            if connection.get("project_id") and str(connection.get("project_id")) != project_id:
                raise PlatformOperationError("Runner invocation project scope mismatch.")
            internal_endpoint_ref = str(connection.get("internal_endpoint_ref") or "")
            if internal_endpoint_ref:
                from app.services.internal_endpoints import resolve_internal_endpoint

                internal_endpoint_url = resolve_internal_endpoint(internal_endpoint_ref)
            allowed_refs = declared.intersection({str(value) for value in connection.get("credential_refs", [])})
            credentials = CredentialService(self.store)
            for reference in sorted(allowed_refs):
                # CredentialService intentionally refuses returning a bare str.
                boxed = credentials.use(reference, lambda secret: {"value": secret})
                credential_values[reference] = str(boxed["value"])
        from app.services.integration_settings import IntegrationSettingsService

        settings = IntegrationSettingsService.model(integration)
        request.update({
            "operation": str(request["operation"]),
            "args": request.pop("_args"),
            "kwargs": request.pop("_kwargs"),
            "context": context,
            "claim_token": request.pop("claim_token"),
            "credentials": credential_values,
            "settings": (settings or {}).get("values", {}),
            "media_base_url": str(
                os.getenv("LYNEWRIGHT_INTERNAL_MEDIA_URL", "").strip()
                or os.getenv("LYNEWRIGHT_PUBLIC_URL", "http://127.0.0.1:8000").strip()
            ),
            "connection": {
                "id": connection_id,
                "endpoint_url": str((connection or {}).get("endpoint_url") or ""),
                "internal_endpoint_ref": str((connection or {}).get("internal_endpoint_ref") or ""),
                "internal_endpoint_url": internal_endpoint_url,
                "origin_kind": str((connection or {}).get("origin_kind") or ""),
                "configuration": (connection or {}).get("configuration") or {},
            },
        })
        request.pop("_context", None)
        return request

    def media_link(
        self, identity: dict[str, str], *, request_id: str, media_id: str, ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        request = IntegrationWorkerInvocationStore(self.store, tenant_id=self.tenant_id).get_for_runner(
            request_id, integration_id=identity["integration_id"],
        )
        if request.get("status") != "running" or request.get("worker_id") != identity["runner_id"]:
            raise PlatformOperationError("Runner request is not actively claimed by this token.")
        context = request.get("_context") or {}
        project_id = str(context.get("project_id") or "")
        if not project_id:
            raise PlatformOperationError("Runner media links require a project-scoped invocation.")
        if not _contains_media_ref((request.get("_args"), request.get("_kwargs")), str(media_id)):
            raise PlatformNotFoundError("Media not found in this invocation.")
        media = MediaService(self.store, tenant_id=self.tenant_id).get(str(media_id))
        if str(media.get("project_id") or "") != project_id:
            raise PlatformNotFoundError("Media not found in this project.")
        from app.services.media_links import MediaLinksService

        links = MediaLinksService(self.store, tenant_id=self.tenant_id)
        token = links.issue(
            str(media_id), ttl_seconds=ttl_seconds, purpose="integration-runner",
            project_id=project_id, run_id=str(context.get("resource_id") or ""),
        )
        return {"url": links.public_url(token, tenant_id=self.tenant_id, internal=True), "expires_in": ttl_seconds}

    def upload_artifact(
        self, identity: dict[str, str], *, request_id: str, body: bytes,
        name: str, content_type: str,
    ) -> dict[str, Any]:
        request = IntegrationWorkerInvocationStore(self.store, tenant_id=self.tenant_id).get_for_runner(
            request_id, integration_id=identity["integration_id"],
        )
        if request.get("status") != "running" or request.get("worker_id") != identity["runner_id"]:
            raise PlatformOperationError("Runner request is not actively claimed by this token.")
        if not body or len(body) > MAX_RUNNER_ARTIFACT_BYTES:
            raise PlatformValidationError("Runner artifact must be non-empty and at most 250 MB.")
        context = request.get("_context") or {}
        project_id = str(context.get("project_id") or "")
        if not project_id:
            raise PlatformOperationError("Runner artifacts require a project-scoped invocation.")
        from app.services.integration_artifacts import materialize_artifact_bytes

        media = materialize_artifact_bytes(
            tenant_id=self.tenant_id, project_id=project_id,
            origin_url=f"runner:{identity['integration_id']}:{request_id}:{name}",
            body=body, name=name, content_type=content_type,
            max_bytes=MAX_RUNNER_ARTIFACT_BYTES, store=self.store,
            integration_id=str(identity["integration_id"]),
        )
        return {
            "media_id": str(media["id"]), "url": f"managed://{media['id']}",
            "name": str(media.get("name") or name),
            "content_type": str(media.get("content_type") or content_type),
            "project_id": project_id,
        }


__all__ = ["IntegrationRunnerService", "MAX_CLAIM_WAIT_SECONDS", "RUNNER_HEALTH_SECONDS"]
