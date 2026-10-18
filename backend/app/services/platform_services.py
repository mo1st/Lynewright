"""Stable, adapter-neutral management services for Lynewright.

These services deliberately expose public platform concepts only. REST, MCP,
CLI, and future extension adapters must call them instead of reaching into the
SQLite schema or feature-specific services directly.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import tempfile
import threading
from contextlib import contextmanager, closing
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

import httpx
from urllib.parse import urljoin, urlsplit

from app.config import get_settings
from app.database import (
    get_ai_settings,
    get_connection,
    init_db,
    list_ai_provider_profiles,
    save_ai_settings,
)
from app.models.settings import AI_PROVIDERS
from app.services.ai_provider import settings_response, test_provider
from app.services.pinned_http_transport import PinnedHTTPTransport
from app.services.secret_store import decrypt_secret, encrypt_secret, mask_secret
from app.services.tenant_schema import DEFAULT_TENANT_ID
from app.services.url_security import URLSecurityError, resolve_public_target

from app.services.platform_models import (
    CredentialSetInput,
    EventCreateInput,
    JobCreateInput,
    MediaCreateInput,
    ProjectCreateInput,
    ProjectUpdateInput,
    SettingSetInput,
)


_JOB_CREATION_GATES: list[Callable[[str, dict[str, Any]], None]] = []


def register_job_creation_gate(gate: Callable[[str, dict[str, Any]], None]) -> None:
    """Register a server-side policy check consulted by every JobService.create call.

    Gates receive (job_type, payload) before any row is written and refuse
    creation by raising a PlatformServiceError. This is generic policy
    plumbing; specific gates live in feature services and are installed by
    the code path that owns them.
    """
    _JOB_CREATION_GATES.append(gate)


def reset_job_creation_gates() -> None:
    _JOB_CREATION_GATES.clear()


class PlatformServiceError(Exception):
    """Stable base error for platform adapters."""

    code = "platform_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PlatformValidationError(PlatformServiceError):
    code = "invalid_input"


class PlatformNotFoundError(PlatformServiceError):
    code = "not_found"


class PlatformConflictError(PlatformServiceError):
    code = "conflict"


class PlatformOperationError(PlatformServiceError):
    code = "operation_failed"


_T = TypeVar("_T")
_SENSITIVE_KEY = re.compile(r"(secret|token|password|api[_-]?key|credential)", re.IGNORECASE)
_PLATFORM_SCHEMA_LOCK = threading.RLock()


def _validated(model: type[_T], value: Any) -> _T:
    try:
        return model.model_validate(value)  # type: ignore[attr-defined, no-any-return]
    except Exception as exc:
        raise PlatformValidationError(str(exc)) from exc


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class PlatformStore:
    """Owns the small, public-platform tables without exposing a database API."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection] | None = None) -> None:
        self._connection_factory = connection_factory

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self._connection_factory is not None:
            db = self._connection_factory()
            self.ensure_schema(db)
            yield db
            return
        init_db()
        with closing(get_connection()) as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        # A few worker bridges intentionally share an in-memory SQLite
        # connection across threads in tests and lightweight deployments.
        # Serialize schema probes/migrations so one thread cannot leave an
        # active cursor while another executes ALTER TABLE on the same handle.
        with _PLATFORM_SCHEMA_LOCK:
            PlatformStore._ensure_schema_unlocked(db)

    @staticmethod
    def _ensure_schema_unlocked(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_jobs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT,
                client_id TEXT,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                completed_at TEXT,
                cancelled_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_platform_jobs_scope
                ON platform_jobs(tenant_id, project_id, status, created_at DESC);
            CREATE TABLE IF NOT EXISTS platform_media (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                content_type TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                deleted_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_platform_media_scope
                ON platform_media(tenant_id, project_id, deleted_at, created_at DESC);
            CREATE TABLE IF NOT EXISTS platform_credentials (
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                encrypted_secret TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS platform_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS platform_events (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_platform_events_scope
                ON platform_events(tenant_id, project_id, event_type, created_at DESC);
            CREATE TABLE IF NOT EXISTS platform_clients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                client_type TEXT NOT NULL,
                permission_mode TEXT NOT NULL,
                granted_capabilities_json TEXT NOT NULL DEFAULT '[]',
                token_hash TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_authenticated_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_platform_clients_name
                ON platform_clients(name);
            CREATE TABLE IF NOT EXISTS platform_mcp_servers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                endpoint_url TEXT NOT NULL,
                credential_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'configured',
                protocol_version TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        job_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(platform_jobs)").fetchall()}
        if "client_id" not in job_columns:
            try:
                db.execute("ALTER TABLE platform_jobs ADD COLUMN client_id TEXT")
            except sqlite3.OperationalError as exc:
                # Two workers can initialize an older shared SQLite database
                # concurrently.  The other worker may add the column between
                # our PRAGMA and ALTER; that is successful initialization, not
                # a request failure.
                if "duplicate column name" not in str(exc).lower():
                    raise
        if "progress" not in job_columns:
            try:
                db.execute("ALTER TABLE platform_jobs ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise


def _json_contains_exact(value: Any, target: str, *, depth: int = 0) -> bool:
    """Bounded exact-value search for neutral CredentialRefs in settings JSON."""
    if depth > 8:
        return False
    if isinstance(value, str):
        return value == target
    if isinstance(value, list):
        return any(_json_contains_exact(item, target, depth=depth + 1) for item in value[:200])
    if isinstance(value, dict):
        return any(_json_contains_exact(item, target, depth=depth + 1) for item in list(value.values())[:200])
    return False


class SystemService:
    def __init__(self, store: PlatformStore | None = None) -> None:
        self.store = store or PlatformStore()

    def get_info(self) -> dict[str, Any]:
        settings = get_settings()
        return {
            "name": "Lynewright",
            "application_version": settings.application_version,
            "environment": settings.environment,
            "storage": "sqlite",
        }

    def get_health(self) -> dict[str, Any]:
        try:
            with self.store.connection() as db:
                db.execute("SELECT 1").fetchone()
            return {"status": "ok", "database": "ok"}
        except sqlite3.Error as exc:
            return {"status": "degraded", "database": "error", "error": str(exc)}

    def get_capabilities(self) -> list[str]:
        return [
            "system.read", "projects.read", "projects.write", "projects.delete",
            "jobs.read", "jobs.write", "media.read", "media.write", "media.delete",
            "ai.read", "ai.configure", "ai.test", "credentials.manage",
            "settings.read", "settings.write", "events.read",
        ]

    def get_diagnostics(self) -> dict[str, Any]:
        return {"system": self.get_info(), "health": self.get_health(), "capabilities": self.get_capabilities()}


class ProjectService:
    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        clause = "" if include_archived else "AND status = 'active'"
        with self.store.connection() as db:
            rows = db.execute(
                f"SELECT project_id, tenant_id, name, status, created_at, updated_at FROM tenant_projects WHERE tenant_id = ? {clause} ORDER BY created_at, project_id",
                (self.tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, project_id: str) -> dict[str, Any]:
        self._valid_id(project_id)
        with self.store.connection() as db:
            row = db.execute(
                "SELECT project_id, tenant_id, name, status, created_at, updated_at FROM tenant_projects WHERE tenant_id=? AND project_id=?",
                (self.tenant_id, project_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Project not found.")
        return dict(row)

    def create(self, value: ProjectCreateInput | dict[str, Any]) -> dict[str, Any]:
        payload = value if isinstance(value, ProjectCreateInput) else _validated(ProjectCreateInput, value)
        project_id = payload.project_id or f"prj_{secrets.token_hex(8)}"
        try:
            with self.store.connection() as db, db:
                db.execute(
                    "INSERT INTO tenant_projects (project_id,tenant_id,name) VALUES (?,?,?)",
                    (project_id, self.tenant_id, payload.name),
                )
        except sqlite3.IntegrityError as exc:
            raise PlatformConflictError("Project id or project name already exists.") from exc
        return self.get(project_id)

    def update(self, project_id: str, value: ProjectUpdateInput | dict[str, Any]) -> dict[str, Any]:
        self._valid_id(project_id)
        payload = value if isinstance(value, ProjectUpdateInput) else _validated(ProjectUpdateInput, value)
        with self.store.connection() as db, db:
            if db.execute("SELECT 1 FROM tenant_projects WHERE tenant_id=? AND project_id=?", (self.tenant_id, project_id)).fetchone() is None:
                raise PlatformNotFoundError("Project not found.")
            fields: list[str] = []
            params: list[Any] = []
            if payload.name is not None:
                fields.append("name=?")
                params.append(payload.name)
            if payload.status is not None:
                fields.append("status=?")
                params.append(payload.status)
            try:
                db.execute(f"UPDATE tenant_projects SET {', '.join(fields)}, updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND project_id=?", (*params, self.tenant_id, project_id))
            except sqlite3.IntegrityError as exc:
                raise PlatformConflictError("A project with that name already exists.") from exc
        return self.get(project_id)

    def delete(self, project_id: str, *, confirm_project_id: str) -> dict[str, Any]:
        self._valid_id(project_id)
        if confirm_project_id != project_id:
            raise PlatformValidationError("Project deletion requires confirm_project_id to match the target.")
        with self.store.connection() as db, db:
            row = db.execute("SELECT status FROM tenant_projects WHERE tenant_id=? AND project_id=?", (self.tenant_id, project_id)).fetchone()
            if row is None:
                raise PlatformNotFoundError("Project not found.")
            db.execute("UPDATE tenant_projects SET status='archived', updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND project_id=?", (self.tenant_id, project_id))
        return {"project_id": project_id, "status": "archived"}

    @staticmethod
    def _valid_id(project_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", str(project_id)):
            raise PlatformValidationError("Invalid project id.")


class JobService:
    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID, event_bus: Any | None = None) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id
        self.event_bus = event_bus

    def list(self, *, project_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses, params = ["tenant_id=?"], [self.tenant_id]
        if project_id:
            clauses.append("project_id=?")
            params.append(project_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        with self.store.connection() as db:
            rows = db.execute(f"SELECT * FROM platform_jobs WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id DESC", params).fetchall()
        return [self._public(row) for row in rows]

    def get(self, job_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM platform_jobs WHERE tenant_id=? AND id=?", (self.tenant_id, job_id)).fetchone()
        if row is None:
            raise PlatformNotFoundError("Job not found.")
        return self._public(row)

    def create(self, value: JobCreateInput | dict[str, Any], *, project_id: str | None = None, client_id: str | None = None) -> dict[str, Any]:
        payload = value if isinstance(value, JobCreateInput) else _validated(JobCreateInput, value)
        for gate in tuple(_JOB_CREATION_GATES):
            gate(payload.type, payload.payload)
        if project_id:
            with self.store.connection() as db:
                exists = db.execute(
                    "SELECT 1 FROM tenant_projects WHERE tenant_id=? AND project_id=? AND status='active'",
                    (self.tenant_id, project_id),
                ).fetchone()
            if exists is None:
                raise PlatformNotFoundError("Project not found.")
        job_id = f"job_{secrets.token_hex(12)}"
        with self.store.connection() as db, db:
            db.execute("INSERT INTO platform_jobs (id,tenant_id,project_id,client_id,job_type,payload_json) VALUES (?,?,?,?,?,?)", (job_id, self.tenant_id, project_id, client_id, payload.type, json.dumps(_redact(payload.payload))))
        result = self.get(job_id)
        self._emit("job.created", {"job_id": job_id, "type": result["type"], "project_id": project_id, "client_id": client_id})
        return result

    def start(self, job_id: str) -> dict[str, Any]:
        with self.store.connection() as db, db:
            row = db.execute("SELECT status FROM platform_jobs WHERE tenant_id=? AND id=?", (self.tenant_id, job_id)).fetchone()
            if row is None:
                raise PlatformNotFoundError("Job not found.")
            if row["status"] != "queued":
                raise PlatformOperationError("Only queued jobs can be started.")
            db.execute("UPDATE platform_jobs SET status='running',started_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?", (self.tenant_id, job_id))
        result = self.get(job_id)
        self._emit("job.started", {"job_id": job_id, "type": result["type"]})
        return result

    def report_progress(self, job_id: str, *, progress: int, summary: str = "") -> dict[str, Any]:
        if not isinstance(progress, int) or not 0 <= progress <= 100:
            raise PlatformValidationError("Job progress must be an integer between 0 and 100.")
        with self.store.connection() as db, db:
            row = db.execute("SELECT status FROM platform_jobs WHERE tenant_id=? AND id=?", (self.tenant_id, job_id)).fetchone()
            if row is None:
                raise PlatformNotFoundError("Job not found.")
            if row["status"] != "running":
                raise PlatformOperationError("Only running jobs can report progress.")
            db.execute("UPDATE platform_jobs SET progress=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?", (progress, self.tenant_id, job_id))
        result = self.get(job_id)
        self._emit("job.progress", {"job_id": job_id, "progress": progress, "summary": summary[:500]})
        return result

    def complete(self, job_id: str, *, result: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.store.connection() as db, db:
            row = db.execute("SELECT status FROM platform_jobs WHERE tenant_id=? AND id=?", (self.tenant_id, job_id)).fetchone()
            if row is None:
                raise PlatformNotFoundError("Job not found.")
            if row["status"] != "running":
                raise PlatformOperationError("Only running jobs can be completed.")
            db.execute("UPDATE platform_jobs SET status='completed',progress=100,result_json=?,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?", (json.dumps(_redact(result or {})), self.tenant_id, job_id))
        completed = self.get(job_id)
        self._emit("job.completed", {"job_id": job_id, "result": completed["result"]})
        return completed

    def fail(self, job_id: str, *, code: str, message: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,99}", code):
            raise PlatformValidationError("Invalid job error code.")
        with self.store.connection() as db, db:
            row = db.execute("SELECT status FROM platform_jobs WHERE tenant_id=? AND id=?", (self.tenant_id, job_id)).fetchone()
            if row is None:
                raise PlatformNotFoundError("Job not found.")
            if row["status"] != "running":
                raise PlatformOperationError("Only running jobs can fail.")
            db.execute("UPDATE platform_jobs SET status='failed',error_code=?,error_message=?,completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?", (code, message[:1_000], self.tenant_id, job_id))
        failed = self.get(job_id)
        self._emit("job.failed", {"job_id": job_id, "error_code": code, "error_message": failed["error_message"]})
        return failed

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.store.connection() as db, db:
            row = db.execute("SELECT status FROM platform_jobs WHERE tenant_id=? AND id=?", (self.tenant_id, job_id)).fetchone()
            if row is None:
                raise PlatformNotFoundError("Job not found.")
            if row["status"] in {"completed", "failed", "cancelled"}:
                raise PlatformOperationError("Only queued or running jobs can be cancelled.")
            db.execute("UPDATE platform_jobs SET status='cancelled',cancelled_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?", (self.tenant_id, job_id))
        result = self.get(job_id)
        self._emit("job.cancelled", {"job_id": job_id})
        return result

    def retry(self, job_id: str) -> dict[str, Any]:
        with self.store.connection() as db, db:
            row = db.execute("SELECT status FROM platform_jobs WHERE tenant_id=? AND id=?", (self.tenant_id, job_id)).fetchone()
            if row is None:
                raise PlatformNotFoundError("Job not found.")
            if row["status"] not in {"failed", "cancelled"}:
                raise PlatformOperationError("Only failed or cancelled jobs can be retried.")
            db.execute("UPDATE platform_jobs SET status='queued',attempts=attempts+1,error_code='',error_message='',cancelled_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?", (self.tenant_id, job_id))
        result = self.get(job_id)
        self._emit("job.created", {"job_id": job_id, "retry": True, "attempts": result["attempts"]})
        return result

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_bus is not None:
            self.event_bus.publish({"type": event_type, "payload": payload})

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["type"] = value.pop("job_type")
        value["payload"] = _redact(json.loads(value.pop("payload_json") or "{}"))
        value["result"] = _redact(json.loads(value.pop("result_json") or "{}"))
        return value


class MediaService:
    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id

    def list(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM platform_media WHERE tenant_id=? AND deleted_at IS NULL AND (? IS NULL OR project_id=?) ORDER BY created_at DESC,id DESC", (self.tenant_id, project_id, project_id)).fetchall()
        return [self._public(row) for row in rows]

    def get(self, media_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM platform_media WHERE tenant_id=? AND id=? AND deleted_at IS NULL", (self.tenant_id, media_id)).fetchone()
        if row is None:
            raise PlatformNotFoundError("Media not found.")
        return self._public(row)

    def create(self, value: MediaCreateInput | dict[str, Any], *, project_id: str | None = None) -> dict[str, Any]:
        payload = value if isinstance(value, MediaCreateInput) else _validated(MediaCreateInput, value)
        HTTPService().validate_public_url(payload.url)
        media_id = f"med_{secrets.token_hex(12)}"
        with self.store.connection() as db, db:
            db.execute("INSERT INTO platform_media (id,tenant_id,project_id,name,url,content_type,metadata_json) VALUES (?,?,?,?,?,?,?)", (media_id, self.tenant_id, project_id, payload.name, payload.url, payload.content_type, json.dumps(_redact(payload.metadata))))
        return self.get(media_id)

    def create_managed(
        self,
        *,
        name: str,
        content_type: str,
        metadata: dict[str, Any],
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a media row for bytes Lynewright already owns on local storage.

        Managed media is intentionally not treated as an outbound public URL.
        It is exposed only through a separately issued, revocable media link.
        """

        stored_as = str(metadata.get("stored_as") or "")
        if not stored_as:
            raise PlatformValidationError("Managed media requires a confined stored_as value.")
        media_id = f"med_{secrets.token_hex(12)}"
        with self.store.connection() as db, db:
            db.execute(
                "INSERT INTO platform_media (id,tenant_id,project_id,name,url,content_type,metadata_json) VALUES (?,?,?,?,?,?,?)",
                (
                    media_id, self.tenant_id, project_id, str(name)[:255],
                    f"managed://{media_id}", str(content_type),
                    json.dumps(_redact(metadata)),
                ),
            )
        return self.get(media_id)

    def delete(self, media_id: str, *, confirm_media_id: str) -> dict[str, Any]:
        if media_id != confirm_media_id:
            raise PlatformValidationError("Media deletion requires confirm_media_id to match the target.")
        with self.store.connection() as db, db:
            if db.execute("SELECT 1 FROM platform_media WHERE tenant_id=? AND id=? AND deleted_at IS NULL", (self.tenant_id, media_id)).fetchone() is None:
                raise PlatformNotFoundError("Media not found.")
            db.execute("UPDATE platform_media SET deleted_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND id=?", (self.tenant_id, media_id))
        from app.services.media_links import MediaLinksService
        MediaLinksService(self.store, tenant_id=self.tenant_id).revoke_media(media_id)
        return {"id": media_id, "deleted": True}

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = _redact(json.loads(result.pop("metadata_json") or "{}"))
        return result


class AIService:
    def list_providers(self) -> list[dict[str, Any]]:
        return settings_response(get_ai_settings())["providers"]

    def get_provider_status(self, provider_id: int | str | None = None) -> dict[str, Any]:
        settings = get_ai_settings()
        if provider_id is None or provider_id == "default":
            response = settings_response(settings)
            return {key: value for key, value in response.items() if key not in {"api_keys"}}
        for profile in self.list_providers():
            if str(profile["id"]) == str(provider_id):
                return profile
        raise PlatformNotFoundError("AI provider not found.")

    def configure_provider(self, *, provider: str, model: str, base_url: str = "", api_key: str | None = None) -> dict[str, Any]:
        if provider not in AI_PROVIDERS:
            raise PlatformValidationError("Unsupported AI provider.")
        if not model.strip():
            raise PlatformValidationError("AI model is required.")
        settings = get_ai_settings()
        settings["provider"] = provider
        settings["model"] = model.strip()
        if base_url:
            settings["base_url"] = base_url.strip()
        if api_key is not None:
            keys = dict(settings.get("api_keys") or {})
            keys[provider] = api_key
            settings["api_keys"] = keys
        save_ai_settings(settings)
        return self.get_provider_status("default")

    def test_provider(self, provider_id: int | str | None = None) -> dict[str, Any]:
        if provider_id not in {None, "default"}:
            if not any(str(profile["id"]) == str(provider_id) for profile in list_ai_provider_profiles()):
                raise PlatformNotFoundError("AI provider not found.")
        return _redact(test_provider(get_ai_settings()))

    def run(self, *, task: str, payload: dict[str, Any], jobs: JobService, project_id: str | None = None) -> dict[str, Any]:
        return jobs.create({"type": f"ai.{task}", "payload": payload}, project_id=project_id)


class CredentialService:
    def __init__(self, store: PlatformStore | None = None) -> None:
        self.store = store or PlatformStore()

    def list_metadata(self) -> list[dict[str, Any]]:
        with self.store.connection() as db:
            rows = db.execute("SELECT name,kind,created_at,updated_at FROM platform_credentials ORDER BY name").fetchall()
        return [dict(row) | {"configured": True} for row in rows]

    def get_status(self, name: str) -> dict[str, Any]:
        with self.store.connection() as db:
            row = db.execute("SELECT name,kind,created_at,updated_at FROM platform_credentials WHERE name=?", (name,)).fetchone()
        if row is None:
            return {"name": name, "configured": False}
        return dict(row) | {"configured": True}

    def set(self, value: CredentialSetInput | dict[str, Any]) -> dict[str, Any]:
        payload = value if isinstance(value, CredentialSetInput) else _validated(CredentialSetInput, value)
        with self.store.connection() as db, db:
            db.execute("INSERT INTO platform_credentials (name,kind,encrypted_secret) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind,encrypted_secret=excluded.encrypted_secret,updated_at=CURRENT_TIMESTAMP", (payload.name, payload.kind, encrypt_secret(payload.secret)))
        return self.get_status(payload.name)

    def references(self, name: str, *, limit: int = 100) -> list[dict[str, str]]:
        """Return bounded, non-secret consumers of a CredentialRef.

        The lookup deliberately inspects only reference columns and neutral
        settings JSON.  It never reads encrypted credential material and it
        tolerates optional feature tables not yet initialized in a test or a
        minimal deployment.
        """
        target = str(name or "").strip()
        if not target:
            return []
        found: list[dict[str, str]] = []

        def add(consumer: str, label: str) -> None:
            if len(found) < limit and not any(item["consumer"] == consumer and item["label"] == label for item in found):
                found.append({"consumer": consumer, "label": label})

        def table_exists(db: sqlite3.Connection, table: str) -> bool:
            row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            return row is not None

        def parse_json(raw: Any) -> Any:
            try:
                return json.loads(str(raw or "{}"))
            except (TypeError, json.JSONDecodeError):
                return None

        with self.store.connection() as db:
            if table_exists(db, "platform_integration_connections"):
                for row in db.execute("SELECT integration_id,id,name,credential_refs_json FROM platform_integration_connections").fetchall():
                    refs = parse_json(row["credential_refs_json"])
                    if isinstance(refs, list) and target in {str(item) for item in refs}:
                        add("integration_connection", f"{row['integration_id']} / {row['name'] or row['id']}")
            if table_exists(db, "platform_extensions"):
                for row in db.execute("SELECT id,name,configuration_json FROM platform_extensions").fetchall():
                    configuration = parse_json(row["configuration_json"])
                    if _json_contains_exact(configuration, target):
                        add("integration_configuration", f"{row['id']} / {row['name']}")
            if table_exists(db, "platform_integration_oauth_tokens"):
                for row in db.execute("SELECT integration_id,connection_id,access_credential_ref,refresh_credential_ref FROM platform_integration_oauth_tokens").fetchall():
                    if target in {str(row["access_credential_ref"] or ""), str(row["refresh_credential_ref"] or "")}:
                        add("oauth", f"{row['integration_id']} / connection {row['connection_id']}")
            if table_exists(db, "platform_mcp_servers"):
                for row in db.execute("SELECT name FROM platform_mcp_servers WHERE credential_name=?", (target,)).fetchall():
                    add("mcp", str(row["name"]))
            if table_exists(db, "ai_provider_profiles"):
                for row in db.execute("SELECT name FROM ai_provider_profiles WHERE credential_ref=?", (target,)).fetchall():
                    add("ai_profile", str(row["name"]))
            if table_exists(db, "ai_task_providers"):
                for row in db.execute("SELECT task_type FROM ai_task_providers WHERE credential_ref=?", (target,)).fetchall():
                    add("ai_task", str(row["task_type"]))
            if table_exists(db, "ai_settings"):
                row = db.execute("SELECT provider,credential_refs FROM ai_settings WHERE id=1").fetchone()
                refs = parse_json(row["credential_refs"]) if row is not None else None
                if isinstance(refs, dict):
                    for provider, ref in refs.items():
                        if str(ref or "") == target:
                            add("ai_global", str(provider))
            if table_exists(db, "platform_settings"):
                for row in db.execute("SELECT key,value_json FROM platform_settings").fetchall():
                    value = parse_json(row["value_json"])
                    if _json_contains_exact(value, target):
                        key = str(row["key"])
                        if key == "platform.email.provider":
                            add("email", "SMTP/email provider")
                        elif key == "platform.system.backups":
                            add("backup", "S3 backup destination")
                        else:
                            add("setting", key)
        return found

    def delete(self, name: str, *, confirm_name: str) -> dict[str, Any]:
        if name != confirm_name:
            raise PlatformValidationError("Credential deletion requires confirm_name to match the target.")
        references = self.references(name)
        if references:
            details = ", ".join(f"{item['consumer']} / {item['label']}" for item in references[:10])
            suffix = " and more" if len(references) > 10 else ""
            raise PlatformConflictError(f"Credential is currently used by: {details}{suffix}. Detach or replace these references first.")
        with self.store.connection() as db, db:
            if db.execute("DELETE FROM platform_credentials WHERE name=?", (name,)).rowcount != 1:
                raise PlatformNotFoundError("Credential not found.")
        return {"name": name, "deleted": True}

    def use(self, name: str, operation: Callable[[str], _T]) -> _T:
        """Pass a decrypted value only to an internal operation; never return it."""
        with self.store.connection() as db:
            row = db.execute("SELECT encrypted_secret FROM platform_credentials WHERE name=?", (name,)).fetchone()
        if row is None:
            raise PlatformNotFoundError("Credential not found.")
        result = operation(decrypt_secret(str(row["encrypted_secret"])))
        if isinstance(result, str) and result == decrypt_secret(str(row["encrypted_secret"])):
            raise PlatformOperationError("Credential operations must not return a raw secret.")
        return result


class SettingsService:
    def __init__(self, store: PlatformStore | None = None) -> None:
        self.store = store or PlatformStore()

    def list(self) -> list[dict[str, Any]]:
        with self.store.connection() as db:
            rows = db.execute("SELECT key,value_json,updated_at FROM platform_settings ORDER BY key").fetchall()
        return [{"key": row["key"], "value": json.loads(row["value_json"]), "updated_at": row["updated_at"]} for row in rows]

    def get(self, key: str) -> dict[str, Any]:
        with self.store.connection() as db:
            row = db.execute("SELECT key,value_json,updated_at FROM platform_settings WHERE key=?", (key,)).fetchone()
        if row is None:
            raise PlatformNotFoundError("Setting not found.")
        return {"key": row["key"], "value": json.loads(row["value_json"]), "updated_at": row["updated_at"]}

    def set(self, value: SettingSetInput | dict[str, Any]) -> dict[str, Any]:
        payload = value if isinstance(value, SettingSetInput) else _validated(SettingSetInput, value)
        with self.store.connection() as db, db:
            db.execute("INSERT INTO platform_settings (key,value_json) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=CURRENT_TIMESTAMP", (payload.key, json.dumps(_redact(payload.value))))
        try:
            from app.services.operator_runtime_overrides import invalidate_override_cache

            invalidate_override_cache()
        except Exception:
            pass
        return self.get(payload.key)


class HTTPService:
    MAX_BUFFERED_RESPONSE_BYTES = 10_000_000
    MAX_STREAMED_RESPONSE_BYTES = 100 * 1024 * 1024
    MAX_REDIRECTS = 5

    def _transport_for_url(self, url: str, internal_endpoint_ref: str = "") -> Any:
        if internal_endpoint_ref:
            from app.services.internal_endpoints import resolve_internal_endpoint

            origin = resolve_internal_endpoint(internal_endpoint_ref)
            configured = urlsplit(origin)
            parts = urlsplit(url)
            configured_origin = (
                configured.scheme.lower(), (configured.hostname or "").lower(),
                configured.port or (443 if configured.scheme.lower() == "https" else 80),
            )
            requested_origin = (
                parts.scheme.lower(), (parts.hostname or "").lower(),
                parts.port or (443 if parts.scheme.lower() == "https" else 80),
            )
            if requested_origin != configured_origin:
                raise PlatformValidationError(
                    "An internal endpoint request must stay on its pinned origin."
                )
            return None
        return PinnedHTTPTransport(resolve_public_target(url))

    @staticmethod
    def _safe_headers(headers: Any) -> dict[str, str]:
        return {
            key: value for key, value in headers.items()
            if not _SENSITIVE_KEY.search(key)
        }

    @contextmanager
    def stream_to_temp_file(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_seconds: float = 20.0,
        internal_endpoint_ref: str = "",
    ) -> Iterator[dict[str, Any]]:
        """Stream a guarded GET into a private temporary file and hash it.

        This shares the same public-IP pinning and deployment-owned internal
        endpoint checks as ``request``. Each redirect is resolved and checked
        before another connection is opened. The temporary directory is removed
        when the context exits, including when validation or a caller fails.
        """
        if not 1 <= int(max_bytes) <= self.MAX_STREAMED_RESPONSE_BYTES:
            raise PlatformValidationError("HTTP streaming size limit must be between 1 byte and 100 MiB.")
        if not 0.1 <= float(timeout_seconds) <= 120:
            raise PlatformValidationError("HTTP timeout must be between 0.1 and 120 seconds.")
        if not internal_endpoint_ref and urlsplit(url).scheme.lower() != "https":
            raise PlatformValidationError("Public streamed downloads must use HTTPS.")

        try:
            with tempfile.TemporaryDirectory(prefix="lynewright-http-download-") as directory:
                destination = Path(directory) / "response.part"
                result = self._stream_response_to_path(
                    url, destination, max_bytes=int(max_bytes),
                    timeout_seconds=float(timeout_seconds),
                    internal_endpoint_ref=internal_endpoint_ref,
                )
                yield {**result, "path": destination}
        except URLSecurityError as exc:
            raise PlatformValidationError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise PlatformOperationError("Outbound HTTP request failed.") from exc

    def _stream_response_to_path(
        self,
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
        internal_endpoint_ref: str,
    ) -> dict[str, Any]:
        current_url = url
        validated_next_transport: Any = None
        for redirect_count in range(self.MAX_REDIRECTS + 1):
            if not internal_endpoint_ref and urlsplit(current_url).scheme.lower() != "https":
                raise PlatformValidationError("Public streamed downloads must use HTTPS.")
            transport = validated_next_transport
            validated_next_transport = None
            if transport is None and not internal_endpoint_ref:
                transport = self._transport_for_url(current_url, internal_endpoint_ref)
            elif internal_endpoint_ref:
                self._transport_for_url(current_url, internal_endpoint_ref)
            try:
                with httpx.Client(
                    transport=transport, timeout=timeout_seconds,
                    follow_redirects=False, trust_env=False,
                ) as client:
                    with client.stream("GET", current_url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise PlatformOperationError("Outbound HTTP redirect had no destination.")
                            if redirect_count >= self.MAX_REDIRECTS:
                                raise PlatformOperationError("Outbound HTTP request exceeded the redirect limit.")
                            redirected = urljoin(current_url, location)
                            # Validate the next hop before issuing a request. A
                            # public hop is DNS-resolved and IP-pinned; an
                            # internal ref may never escape its configured origin.
                            if not internal_endpoint_ref and urlsplit(redirected).scheme.lower() != "https":
                                raise PlatformValidationError("Public streamed downloads must remain on HTTPS redirects.")
                            validated_next_transport = self._transport_for_url(redirected, internal_endpoint_ref)
                            current_url = redirected
                            continue
                        if response.status_code >= 400:
                            raise PlatformOperationError("Outbound HTTP request failed.")
                        declared_length = response.headers.get("content-length")
                        if declared_length:
                            try:
                                if int(declared_length) > max_bytes:
                                    raise PlatformOperationError("HTTP response exceeded the configured size limit.")
                            except ValueError as exc:
                                raise PlatformOperationError("HTTP response had an invalid content length.") from exc
                        digest = hashlib.sha256()
                        received = 0
                        with destination.open("xb") as output:
                            for chunk in response.iter_bytes():
                                next_size = received + len(chunk)
                                if next_size > max_bytes:
                                    raise PlatformOperationError("HTTP response exceeded the configured size limit.")
                                output.write(chunk)
                                digest.update(chunk)
                                received = next_size
            except URLSecurityError as exc:
                raise PlatformValidationError(str(exc)) from exc
            except httpx.HTTPError as exc:
                raise PlatformOperationError("Outbound HTTP request failed.") from exc
            return {
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "headers": self._safe_headers(response.headers),
                "bytes": received,
                "sha256": digest.hexdigest(),
            }
        raise PlatformOperationError("Outbound HTTP request exceeded the redirect limit.")

    def validate_public_url(self, url: str) -> dict[str, Any]:
        try:
            target = resolve_public_target(url)
        except URLSecurityError as exc:
            raise PlatformValidationError(str(exc)) from exc
        return {"scheme": target.scheme, "hostname": target.hostname, "port": target.port}

    def get(self, url: str, *, max_bytes: int = 1_000_000, timeout_seconds: float = 15.0) -> dict[str, Any]:
        result = self.request(url, method="GET", max_bytes=max_bytes, timeout_seconds=timeout_seconds)
        if int(result["status_code"]) >= 400:
            raise PlatformOperationError("Outbound HTTP request failed.")
        return result

    def request(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json_body: Any | None = None,
        content: bytes | str | Iterable[bytes] | None = None,
        max_bytes: int = 1_000_000,
        timeout_seconds: float = 15.0,
        internal_endpoint_ref: str = "",
    ) -> dict[str, Any]:
        """Send through the outbound boundary.

        `internal_endpoint_ref` names a deployment-configured origin instead of
        going through public validation. The exemption is re-derived here from
        the environment allowlist rather than taken on the caller's word, and
        the URL must match the resolved origin exactly -- so a ref buys access
        to one origin and nothing else. Redirects stay disabled and the same
        size and timeout bounds apply on both paths.
        """
        normalized_method = method.upper()
        if normalized_method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            raise PlatformValidationError("Unsupported outbound HTTP method.")
        if not 1 <= max_bytes <= self.MAX_BUFFERED_RESPONSE_BYTES:
            raise PlatformValidationError("HTTP response size limit must be between 1 byte and 10 MiB.")
        if not 0.1 <= timeout_seconds <= 120:
            raise PlatformValidationError("HTTP timeout must be between 0.1 and 120 seconds.")
        try:
            transport = self._transport_for_url(url, internal_endpoint_ref)
            with httpx.Client(transport=transport, timeout=timeout_seconds, follow_redirects=False, trust_env=False) as client:
                with client.stream(normalized_method, url, headers=headers, params=params, json=json_body, content=content) as response:
                    chunks: list[bytes] = []
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > max_bytes:
                            raise PlatformOperationError("HTTP response exceeded the configured size limit.")
                        chunks.append(chunk)
        except URLSecurityError as exc:
            raise PlatformValidationError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise PlatformOperationError("Outbound HTTP request failed.") from exc
        return {"status_code": response.status_code, "content_type": response.headers.get("content-type", ""), "headers": self._safe_headers(response.headers), "body": b"".join(chunks)}


class EventService:
    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id

    def publish(self, value: EventCreateInput | dict[str, Any]) -> dict[str, Any]:
        payload = value if isinstance(value, EventCreateInput) else _validated(EventCreateInput, value)
        event_id = f"evt_{secrets.token_hex(12)}"
        with self.store.connection() as db, db:
            db.execute("INSERT INTO platform_events (id,tenant_id,project_id,event_type,payload_json) VALUES (?,?,?,?,?)", (event_id, self.tenant_id, payload.project_id, payload.type, json.dumps(_redact(payload.payload))))
        return self.get(event_id)

    def list(self, *, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise PlatformValidationError("Event limit must be between 1 and 500.")
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM platform_events WHERE tenant_id=? AND (? IS NULL OR project_id=?) ORDER BY created_at DESC,id DESC LIMIT ?", (self.tenant_id, project_id, project_id, limit)).fetchall()
        return [self._public(row) for row in rows]

    def get(self, event_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM platform_events WHERE tenant_id=? AND id=?", (self.tenant_id, event_id)).fetchone()
        if row is None:
            raise PlatformNotFoundError("Event not found.")
        return self._public(row)

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["type"] = result.pop("event_type")
        result["payload"] = _redact(json.loads(result.pop("payload_json") or "{}"))
        return result
