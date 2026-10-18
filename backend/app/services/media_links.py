"""Opaque, revocable links for tenant-scoped managed media."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from typing import Any, Callable

from app.services.capability_context import CapabilityInvocationContext
from app.services.integration_media_transfer import IntegrationMediaTransferService, managed_media_root
from app.services.operation_trace import OperationTraceService
from app.services.platform_services import MediaService, PlatformNotFoundError, PlatformStore, PlatformValidationError
from app.services.tenant_schema import DEFAULT_TENANT_ID


DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 86_400


class MediaLinksService:
    """Issue bearer links while keeping the bearer value out of durable storage."""

    def __init__(
        self,
        store: PlatformStore | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = str(tenant_id or DEFAULT_TENANT_ID)
        self.now = now

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_media_links (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                project_id TEXT,
                media_id TEXT NOT NULL,
                purpose TEXT NOT NULL,
                expires_at REAL NOT NULL,
                revoked_at REAL,
                created_by TEXT NOT NULL DEFAULT '',
                access_count INTEGER NOT NULL DEFAULT 0,
                run_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_platform_media_links_expiry
                ON platform_media_links(expires_at,revoked_at);
            CREATE INDEX IF NOT EXISTS idx_platform_media_links_scope
                ON platform_media_links(tenant_id,project_id,media_id,run_id);
            """
        )

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    @staticmethod
    def public_url(token: str, *, tenant_id: str, internal: bool = False) -> str:
        import os
        from urllib.parse import quote

        base = os.getenv("LYNEWRIGHT_INTERNAL_MEDIA_URL", "").strip() if internal else ""
        base = base or os.getenv("LYNEWRIGHT_PUBLIC_URL", "http://127.0.0.1:8000").strip()
        if not base:
            raise PlatformValidationError("LYNEWRIGHT_PUBLIC_URL must be set to serve media links.")
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise PlatformValidationError("A tenant-scoped media URL requires a tenant id.")
        return f"{base.rstrip('/')}/api/media/l/{quote(token, safe='')}?tenant_id={quote(tenant, safe='')}"

    def issue(
        self,
        media_id: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        purpose: str = "integration",
        actor: Any = None,
        *,
        project_id: str | None = None,
        run_id: str = "",
    ) -> str:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise PlatformValidationError("Media link lifetime must be between 1 second and 24 hours.")
        selected_purpose = str(purpose or "").strip()
        if not selected_purpose or len(selected_purpose) > 120:
            raise PlatformValidationError("Media link purpose is required and bounded.")
        media = MediaService(self.store, tenant_id=self.tenant_id).get(str(media_id))
        media_project = str(media.get("project_id") or "")
        selected_project = str(project_id or "")
        if media_project != selected_project:
            raise PlatformNotFoundError("Media not found.")
        metadata = media.get("metadata") if isinstance(media.get("metadata"), dict) else {}
        stored_as = str(metadata.get("stored_as") or "")
        transfer = IntegrationMediaTransferService(temp_dir=managed_media_root())
        path = transfer.resolve_managed_path(stored_as)
        if not path.is_file():
            raise PlatformNotFoundError("Media not found.")

        token = secrets.token_urlsafe(32)
        digest = self.token_hash(token)
        link_id = "ml_" + secrets.token_hex(12)
        expires_at = self.now() + ttl_seconds
        context = self._context(actor, project_id=selected_project, media_id=str(media_id), entry_point="media.link.issue")
        with self.store.connection() as db, db:
            self.ensure_schema(db)
            db.execute(
                """INSERT INTO platform_media_links
                   (id,token_hash,tenant_id,project_id,media_id,purpose,expires_at,created_by,run_id,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (link_id, digest, self.tenant_id, selected_project or None, str(media_id), selected_purpose,
                 expires_at, self._actor_id(actor), str(run_id or ""), self.now()),
            )
        self._trace(context, "issued", digest, link_id=link_id, media_id=str(media_id), purpose=selected_purpose, run_id=run_id)
        from app.services.application_logging import log_event
        log_event(
            level="info", category="media_link", source="lynewright.media_links",
            message="Managed media link issued.",
            details={"link_hash_sha256": digest, "link_id": link_id, "media_id": str(media_id), "purpose": selected_purpose},
            project_id=selected_project or None, tenant_id=self.tenant_id,
        )
        return token

    def lookup(
        self,
        token: str,
        *,
        expected_tenant_id: str | None = None,
        expected_project_id: str | None = None,
    ) -> dict[str, Any]:
        digest = self.token_hash(token)
        row: dict[str, Any] | None = None
        with self.store.connection() as db:
            self.ensure_schema(db)
            found = db.execute(
                """SELECT l.*,m.name,m.content_type,m.metadata_json,m.deleted_at,m.project_id AS media_project_id
                   FROM platform_media_links l LEFT JOIN platform_media m
                     ON m.tenant_id=l.tenant_id AND m.id=l.media_id
                   WHERE l.token_hash=?""",
                (digest,),
            ).fetchone()
            if found is not None:
                row = dict(found)
        valid = bool(row)
        if row:
            valid = (
                row.get("revoked_at") is None
                and float(row.get("expires_at") or 0) > self.now()
                and row.get("deleted_at") is None
                and str(row.get("tenant_id") or "") == self.tenant_id
                and (expected_tenant_id is None or str(row.get("tenant_id") or "") == str(expected_tenant_id))
                and (expected_project_id is None or str(row.get("project_id") or "") == str(expected_project_id))
                and str(row.get("project_id") or "") == str(row.get("media_project_id") or "")
            )
        if not valid or row is None:
            self._access_record(digest, None, "not_found")
            raise PlatformNotFoundError("Media link not found.") from None
        import json

        try:
            metadata = json.loads(str(row.pop("metadata_json") or "{}"))
        except (TypeError, ValueError):
            metadata = {}
        stored_as = str(metadata.get("stored_as") or "") if isinstance(metadata, dict) else ""
        try:
            path = IntegrationMediaTransferService(temp_dir=managed_media_root()).resolve_managed_path(stored_as)
            if not path.is_file():
                raise PlatformNotFoundError("Media not found.")
            stat = path.stat()
        except (OSError, RuntimeError, PlatformNotFoundError, PlatformValidationError):
            self._access_record(digest, row, "not_found")
            raise PlatformNotFoundError("Media link not found.") from None
        with self.store.connection() as db, db:
            db.execute(
                "UPDATE platform_media_links SET access_count=access_count+1 WHERE id=? AND tenant_id=? AND revoked_at IS NULL",
                (row["id"], row["tenant_id"]),
            )
        row["metadata"] = metadata if isinstance(metadata, dict) else {}
        row["path"] = path
        row["size"] = stat.st_size
        self._access_record(digest, row, "served")
        return row

    def revoke(
        self,
        *,
        token: str | None = None,
        link_id: str | None = None,
        actor: Any = None,
        reason: str = "operator",
    ) -> bool:
        if bool(token) == bool(link_id):
            raise PlatformValidationError("Revoke requires exactly one media link token or id.")
        predicate = "token_hash=?" if token else "id=?"
        value = self.token_hash(token) if token else str(link_id)
        with self.store.connection() as db, db:
            self.ensure_schema(db)
            row = db.execute(
                f"SELECT * FROM platform_media_links WHERE tenant_id=? AND {predicate}",
                (self.tenant_id, value),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                return False
            db.execute(
                "UPDATE platform_media_links SET revoked_at=? WHERE id=? AND tenant_id=? AND revoked_at IS NULL",
                (self.now(), row["id"], self.tenant_id),
            )
        digest = str(row["token_hash"])
        context = self._context(actor, project_id=str(row["project_id"] or ""), media_id=str(row["media_id"]), entry_point="media.link.revoke")
        self._trace(context, "revoked", digest, link_id=str(row["id"]), media_id=str(row["media_id"]), reason=reason)
        return True

    def revoke_run(self, run_id: str, *, reason: str, actor: Any = None) -> int:
        if not run_id:
            return 0
        with self.store.connection() as db, db:
            self.ensure_schema(db)
            rows = db.execute(
                "SELECT * FROM platform_media_links WHERE tenant_id=? AND run_id=? AND revoked_at IS NULL",
                (self.tenant_id, str(run_id)),
            ).fetchall()
            if rows:
                db.execute(
                    "UPDATE platform_media_links SET revoked_at=? WHERE tenant_id=? AND run_id=? AND revoked_at IS NULL",
                    (self.now(), self.tenant_id, str(run_id)),
                )
        for row in rows:
            context = self._context(actor, project_id=str(row["project_id"] or ""), media_id=str(row["media_id"]), entry_point="media.link.revoke")
            self._trace(context, "revoked", str(row["token_hash"]), link_id=str(row["id"]), media_id=str(row["media_id"]), run_id=run_id, reason=reason)
        return len(rows)

    def revoke_media(self, media_id: str) -> int:
        with self.store.connection() as db, db:
            self.ensure_schema(db)
            rows = db.execute(
                "SELECT * FROM platform_media_links WHERE tenant_id=? AND media_id=? AND revoked_at IS NULL",
                (self.tenant_id, str(media_id)),
            ).fetchall()
            if rows:
                db.execute(
                    "UPDATE platform_media_links SET revoked_at=? WHERE tenant_id=? AND media_id=? AND revoked_at IS NULL",
                    (self.now(), self.tenant_id, str(media_id)),
                )
        for row in rows:
            context = self._context(None, project_id=str(row["project_id"] or ""), media_id=str(media_id), entry_point="media.link.revoke")
            self._trace(context, "revoked", str(row["token_hash"]), link_id=str(row["id"]), media_id=str(media_id), reason="media_deleted")
        return len(rows)

    def sweep_expired(self, *, limit: int = 500) -> int:
        if not 1 <= int(limit) <= 5_000:
            raise PlatformValidationError("Media link expiry sweep batch is invalid.")
        with self.store.connection() as db, db:
            self.ensure_schema(db)
            rows = db.execute(
                "SELECT id FROM platform_media_links WHERE tenant_id=? AND expires_at<=? AND revoked_at IS NULL ORDER BY expires_at LIMIT ?",
                (self.tenant_id, self.now(), int(limit)),
            ).fetchall()
            if not rows:
                return 0
            ids = [str(row["id"]) for row in rows]
            marks = ",".join("?" for _ in ids)
            db.execute(
                f"UPDATE platform_media_links SET revoked_at=? WHERE tenant_id=? AND id IN ({marks}) AND revoked_at IS NULL",
                (self.now(), self.tenant_id, *ids),
            )
        return len(ids)

    def sweep_expired_all(self, *, limit: int = 500) -> int:
        """Expire a bounded batch across tenants for the shared worker loop."""
        if not 1 <= int(limit) <= 5_000:
            raise PlatformValidationError("Media link expiry sweep batch is invalid.")
        with self.store.connection() as db, db:
            self.ensure_schema(db)
            rows = db.execute(
                "SELECT * FROM platform_media_links WHERE expires_at<=? AND revoked_at IS NULL ORDER BY expires_at LIMIT ?",
                (self.now(), int(limit)),
            ).fetchall()
            if rows:
                ids = [str(row["id"]) for row in rows]
                marks = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE platform_media_links SET revoked_at=? WHERE id IN ({marks}) AND revoked_at IS NULL",
                    (self.now(), *ids),
                )
        for row in rows:
            context = self._context(None, tenant_id=str(row["tenant_id"]), project_id=str(row["project_id"] or ""), media_id=str(row["media_id"]), entry_point="media.link.expiry_sweep")
            self._trace(context, "revoked", str(row["token_hash"]), link_id=str(row["id"]), media_id=str(row["media_id"]), reason="expired")
        return len(rows)

    def _access_record(self, digest: str, row: dict[str, Any] | None, status: str) -> None:
        tenant_id = str(row.get("tenant_id") if row else self.tenant_id)
        project_id = str(row.get("project_id") or "") if row else ""
        media_id = str(row.get("media_id") or "") if row else ""
        context = self._context(None, tenant_id=tenant_id, project_id=project_id, media_id=media_id, entry_point="media.link.access")
        self._trace(context, status, digest, link_id=str(row.get("id") or "") if row else "", media_id=media_id)
        from app.services.application_logging import log_event
        log_event(
            level="info" if status == "served" else "warning",
            category="media_link", source="lynewright.media_links",
            message="Managed media link accessed." if status == "served" else "Managed media link access was refused.",
            details={"link_hash_sha256": digest, "outcome": status, "link_id": str(row.get("id") or "") if row else ""},
            project_id=project_id or None, tenant_id=tenant_id,
        )

    def _trace(self, context: CapabilityInvocationContext, status: str, digest: str, **metadata: Any) -> None:
        traces = OperationTraceService(self.store, tenant_id=context.tenant_id)
        try:
            traces.start(context, operation=f"media.link.{status}")
            traces.record(
                context, stage=status, category="media_link", status="completed" if status in {"issued", "served", "revoked"} else "failed",
                resource_type="media", resource_id=str(metadata.get("media_id") or "") or None,
                metadata={"link_hash_sha256": digest, **metadata},
            )
            traces.finish(context.trace_id, tenant_id=context.tenant_id, status="completed" if status in {"issued", "served", "revoked"} else "failed")
        except Exception as exc:
            from app.services.application_logging import log_event
            log_event(
                level="error", category="media_link", source="lynewright.media_links",
                message="Media link trace could not be written.",
                details={"link_hash_sha256": digest, "error_type": type(exc).__name__},
                project_id=context.project_id, tenant_id=context.tenant_id,
            )

    def _context(
        self,
        actor: Any,
        *,
        tenant_id: str | None = None,
        project_id: str = "",
        media_id: str = "",
        entry_point: str,
    ) -> CapabilityInvocationContext:
        if isinstance(actor, CapabilityInvocationContext):
            return CapabilityInvocationContext(
                tenant_id=tenant_id or actor.tenant_id, actor_type=actor.actor_type,
                actor_id=actor.actor_id, client_id=actor.client_id,
                project_id=project_id or actor.project_id, resource_type="media" if media_id else None,
                resource_id=media_id or None, integration_id=actor.integration_id, connection_id=actor.connection_id,
                trace_id=actor.trace_id, parent_span_id=actor.parent_span_id,
                idempotency_key=actor.idempotency_key, entry_point=entry_point,
            )
        client_type = str(getattr(actor, "client_type", "") or "")
        actor_type = "automation" if client_type == "automation" else "operator" if client_type in {"operator", "internal-operator", "internal"} else "human" if actor is not None else "system"
        actor_id = self._actor_id(actor) or None
        return CapabilityInvocationContext.from_actor(
            tenant_id=tenant_id or self.tenant_id, actor_type=actor_type, actor_id=actor_id,
            project_id=project_id or None, resource_type="media" if media_id else None,
            resource_id=media_id or None, entry_point=entry_point,
        )

    @staticmethod
    def _actor_id(actor: Any) -> str:
        if actor is None:
            return "system"
        if isinstance(actor, dict):
            return str(actor.get("user_id") or actor.get("client_id") or actor.get("id") or "")
        return str(getattr(actor, "client_id", "") or getattr(actor, "user_id", "") or "")


__all__ = ["DEFAULT_TTL_SECONDS", "MAX_TTL_SECONDS", "MediaLinksService"]
