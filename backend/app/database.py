"""Lynewright SQLite connection and schema boundary."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from app.config import get_settings
from app.passwords import (
    generate_session_token, hash_password, hash_token, verify_password,
)

BUSY_TIMEOUT_MS = 30_000
LOG_BUSY_TIMEOUT_MS = 1_000
USER_ROLES = ("viewer", "editor", "admin")
_ROLE_RANK = {role: rank for rank, role in enumerate(USER_ROLES)}
_SCHEMA_LOCK = threading.RLock()
_INITIALIZING: set[str] = set()
_INITIALIZED: set[str] = set()


def get_db_path() -> Path:
    return get_settings().database_path


def get_connection(
    db_path: Path | None = None, *, busy_timeout_ms: int | None = None,
) -> sqlite3.Connection:
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=(busy_timeout_ms or BUSY_TIMEOUT_MS) / 1000)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms or BUSY_TIMEOUT_MS)}")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def _ensure_tenants_first(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS tenants (
        id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")


def ensure_users_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")


def ensure_sessions_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        active_tenant_id TEXT NOT NULL DEFAULT '',
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, expires_at)")


def ensure_application_logs_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS application_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL,
        message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}',
        correlation_id TEXT NOT NULL DEFAULT '', request_path TEXT NOT NULL DEFAULT '',
        project_id TEXT, tenant_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_application_logs_created ON application_logs(id DESC)")


def ensure_ai_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS ai_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), settings_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS ai_provider_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        provider TEXT NOT NULL, protocol TEXT NOT NULL DEFAULT 'openai_chat',
        model TEXT NOT NULL, base_url TEXT NOT NULL DEFAULT '', api_key TEXT NOT NULL DEFAULT '',
        credential_ref TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 100,
        enabled INTEGER NOT NULL DEFAULT 1, reasoning_effort TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE INDEX IF NOT EXISTS idx_ai_provider_profiles_enabled
        ON ai_provider_profiles(enabled, priority, id);
    CREATE TABLE IF NOT EXISTS ai_task_providers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_type TEXT NOT NULL,
        provider TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', protocol TEXT NOT NULL DEFAULT '',
        settings_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS ai_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '', artifact_type TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE INDEX IF NOT EXISTS idx_ai_artifacts_scope ON ai_artifacts(project_id, id DESC);
    """)


def _schema_steps() -> tuple[tuple[str, Callable[[sqlite3.Connection], None]], ...]:
    # Imports are local so database.py remains the dependency root of services.
    from app.services.content_review import ContentReviewService
    from app.services.catalogue import CatalogueService
    from app.services.control_plane_action_tokens import ControlPlaneActionTokenService
    from app.services.extension_registry import ExtensionRegistry
    from app.services.integration_provenance import ensure_schema as ensure_provenance_schema
    from app.services.integration_capability_evidence import IntegrationCapabilityEvidenceService
    from app.services.integration_connections import ConnectionProfileService
    from app.services.integration_definition_identity import IntegrationDefinitionIdentityService
    from app.services.integration_drafts import IntegrationDraftStore
    from app.services.integration_external_resources import ExternalResourceInventoryService
    from app.services.integration_gateway import IntegrationGateway
    from app.services.integration_jobs import IntegrationJobLifecycleService
    from app.services.integration_oauth import OAuthService
    from app.services.integration_operations import IntegrationOperationExecutionStore
    from app.services.integration_provider_selection import IntegrationProviderSelectionService
    from app.services.integration_state import IntegrationScheduleStateService, IntegrationStateService
    from app.services.integration_worker_invocations import IntegrationWorkerInvocationStore
    from app.services.integration_runner import IntegrationRunnerService
    from app.services.interaction_schema import init_interaction_schema
    from app.services.line_engine import ProductionLineStore
    from app.services.line_runner import ContractStepRunStore
    from app.services.job_queue import ensure_schema as ensure_runtime_job_schema
    from app.services.mcp_oauth import MCPOAuthService
    from app.services.operation_trace import OperationTraceService
    from app.services.media_links import MediaLinksService
    from app.services.operator_automation import OperatorAutomationService
    from app.services.operator_objectives import OperatorObjectiveService
    from app.services.operator_policy import OperatorPolicyEngine
    from app.services.operator_remediation import ChangePlanService
    from app.services.operator_runtime.schema import init_operator_schema
    from app.services.platform_services import PlatformStore
    from app.services.resource_bindings import ResourceBindingService
    from app.services.resource_outbox import ResourceOutboxService
    from app.services.resource_reconciliation import ResourceReconciliationService
    from app.services.resource_setup_requirements import ResourceSetupRequirementService
    from app.services.tenant_schema import init_tenant_schema
    from app.services import choice_channel, image_review_channel

    def tenant_relations(db: sqlite3.Connection) -> None:
        init_tenant_schema(db)

    def platform_schema(db: sqlite3.Connection) -> None:
        PlatformStore.ensure_schema(db)

    def operation_schema(db: sqlite3.Connection) -> None:
        IntegrationOperationExecutionStore(PlatformStore(), tenant_id="ten_default")._ensure_schema(db)

    def choice_schema(db: sqlite3.Connection) -> None:
        db.executescript(choice_channel._SCHEMA_SQL)

    def review_schema(db: sqlite3.Connection) -> None:
        db.executescript(image_review_channel._SCHEMA_SQL)

    return (
        ("tenants", _ensure_tenants_first),
        ("users", ensure_users_schema),
        ("sessions", ensure_sessions_schema),
        ("application_logs", ensure_application_logs_schema),
        ("tenant_schema", tenant_relations),
        ("platform_services", platform_schema),
        ("media_links", MediaLinksService.ensure_schema),
        ("ai_provider_settings", ensure_ai_schema),
        ("integration_connections", ConnectionProfileService.ensure_schema),
        ("integration_definition_identity", IntegrationDefinitionIdentityService.ensure_schema),
        ("integration_drafts", IntegrationDraftStore.ensure_schema),
        ("integration_external_resources", ExternalResourceInventoryService.ensure_schema),
        ("integration_gateway", IntegrationGateway.ensure_schema),
        ("integration_jobs", IntegrationJobLifecycleService.ensure_schema),
        ("integration_oauth", OAuthService.ensure_schema),
        ("integration_operations", operation_schema),
        ("integration_provider_selection", IntegrationProviderSelectionService.ensure_schema),
        ("integration_state", IntegrationStateService.ensure_schema),
        ("integration_schedule_state", IntegrationScheduleStateService.ensure_schema),
        ("integration_worker_invocations", IntegrationWorkerInvocationStore.ensure_schema),
        ("integration_runner_tokens", IntegrationRunnerService.ensure_schema),
        ("integration_capability_evidence", IntegrationCapabilityEvidenceService.ensure_schema),
        ("runtime_job_queue", ensure_runtime_job_schema),
        ("line_engine", ProductionLineStore.ensure_schema),
        ("line_runner", ContractStepRunStore.ensure_schema),
        ("resource_bindings", ResourceBindingService.ensure_schema),
        ("resource_outbox", ResourceOutboxService.ensure_schema),
        ("resource_reconciliation", ResourceReconciliationService.ensure_schema),
        ("resource_setup_requirements", ResourceSetupRequirementService.ensure_schema),
        ("extension_registry", ExtensionRegistry.ensure_schema),
        ("integration_provenance", ensure_provenance_schema),
        ("control_plane_action_tokens", ControlPlaneActionTokenService.ensure_schema),
        ("operator_runtime", init_operator_schema),
        ("operator_objectives", OperatorObjectiveService.ensure_schema),
        ("operator_automation", OperatorAutomationService.ensure_schema),
        ("operator_remediation", ChangePlanService.ensure_schema),
        ("operator_policy", OperatorPolicyEngine.ensure_schema),
        ("interaction_schema", init_interaction_schema),
        ("choice_channel", choice_schema),
        ("image_review_channel", review_schema),
        ("mcp_oauth", MCPOAuthService.ensure_schema),
        ("operation_trace", OperationTraceService.ensure_schema),
        ("content_review", ContentReviewService.ensure_schema),
        ("catalogue", CatalogueService.ensure_schema),
    )


CORE_SCHEMA_ORDER = (
    "tenants", "users", "sessions", "application_logs", "tenant_schema",
    "platform_services", "media_links", "ai_provider_settings", "integration_connections",
    "integration_definition_identity", "integration_drafts", "integration_external_resources",
    "integration_gateway", "integration_jobs", "integration_oauth",
    "integration_operations", "integration_provider_selection", "integration_state",
    "integration_schedule_state", "integration_worker_invocations",
    "integration_runner_tokens",
    "integration_capability_evidence", "runtime_job_queue", "line_engine", "line_runner",
    "resource_bindings", "resource_outbox", "resource_reconciliation",
    "resource_setup_requirements", "extension_registry", "integration_provenance",
    "control_plane_action_tokens",
    "operator_runtime", "operator_objectives", "operator_automation",
    "operator_remediation", "operator_policy", "interaction_schema", "choice_channel",
    "image_review_channel", "mcp_oauth", "operation_trace",
    "content_review", "catalogue",
)


def init_db() -> None:
    path = str(get_db_path().resolve())
    with _SCHEMA_LOCK:
        if path in _INITIALIZED and Path(path).is_file():
            return
        _INITIALIZED.discard(path)
        if path in _INITIALIZING:
            return
        _INITIALIZING.add(path)
        try:
            with closing(get_connection()) as db:
                db.execute("BEGIN IMMEDIATE")
                for _name, ensure in _schema_steps():
                    ensure(db)
                db.commit()
            _INITIALIZED.add(path)
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise
        finally:
            _INITIALIZING.discard(path)


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def users_exist() -> bool:
    init_db()
    with closing(get_connection()) as db:
        return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def role_at_least(role: str, minimum: str) -> bool:
    return _ROLE_RANK.get(role, -1) >= _ROLE_RANK.get(minimum, 999)


def _public_user(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value.pop("password_hash", None)
    value.pop("token_hash", None)
    return value


def get_user_by_email(email: str) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db:
        return _public_user(db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone())


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db:
        return _public_user(db.execute("SELECT * FROM users WHERE id=?", (int(user_id),)).fetchone())


def list_users() -> list[dict[str, Any]]:
    init_db()
    with closing(get_connection()) as db:
        return [_public_user(row) or {} for row in db.execute("SELECT * FROM users ORDER BY id").fetchall()]


def create_user(email: str, password: str, role: str = "viewer") -> dict[str, Any]:
    normalized = email.strip().lower()
    selected_role = str(role).strip()
    if not normalized or "@" not in normalized:
        raise ValueError("A valid email is required.")
    if selected_role not in USER_ROLES:
        raise ValueError("Unknown user role.")
    init_db()
    try:
        with closing(get_connection()) as db, db:
            cursor = db.execute(
                "INSERT INTO users(email,password_hash,role) VALUES(?,?,?)",
                (normalized, hash_password(password), selected_role),
            )
            user_id = int(cursor.lastrowid)
            db.execute(
                "INSERT OR IGNORE INTO tenant_memberships(tenant_id,user_id,role) VALUES(?,?,?)",
                ("ten_default", user_id, "owner" if selected_role == "admin" else selected_role),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError("An account with that email already exists.") from exc
    return get_user_by_id(user_id) or {}


_TIMING_HASH: str | None = None


def _timing_equaliser_hash() -> str:
    global _TIMING_HASH
    if _TIMING_HASH is None:
        _TIMING_HASH = hash_password(generate_session_token())
    return _TIMING_HASH


def authenticate_user(email: str, password: str) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db:
        row = db.execute("SELECT * FROM users WHERE email=? AND is_active=1", (email.strip().lower(),)).fetchone()
        if row is None:
            # Spend the same scrypt work as a real check so response time does
            # not reveal which email addresses have accounts.
            verify_password(password, _timing_equaliser_hash())
            return None
        if not verify_password(password, str(row["password_hash"])):
            return None
        return _public_user(row)


def describe_login_failure(email: str) -> str:
    init_db()
    with closing(get_connection()) as db:
        row = db.execute("SELECT is_active FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    return "no_such_account" if row is None else "account_inactive" if not row["is_active"] else "bad_password"


def count_admins(*, active_only: bool = True) -> int:
    init_db()
    clause = " AND is_active=1" if active_only else ""
    with closing(get_connection()) as db:
        return int(db.execute(f"SELECT COUNT(*) FROM users WHERE role='admin'{clause}").fetchone()[0])


def update_user_role(user_id: int, role: str) -> dict[str, Any] | None:
    if role not in USER_ROLES: raise ValueError("Unknown user role.")
    init_db()
    with closing(get_connection()) as db, db:
        db.execute("UPDATE users SET role=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (role, int(user_id)))
        db.execute("UPDATE tenant_memberships SET role=? WHERE user_id=? AND tenant_id='ten_default'",
                   ("owner" if role == "admin" else role, int(user_id)))
    return get_user_by_id(user_id)


def set_user_active(user_id: int, is_active: bool) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db, db:
        db.execute("UPDATE users SET is_active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(is_active), int(user_id)))
        if not is_active: db.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))
    return get_user_by_id(user_id)


def delete_user_account(user_id: int, *, actor_id: int) -> dict[str, Any]:
    """Delete an account atomically, retaining the email for existing history."""
    init_db()
    selected_id = int(user_id)
    if selected_id == int(actor_id):
        raise ValueError("You cannot delete your own account.")
    with closing(get_connection()) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id,email,role,is_active FROM users WHERE id=?", (selected_id,)).fetchone()
        if row is None:
            db.rollback()
            raise LookupError("User not found.")
        if row["role"] == "admin" and row["is_active"]:
            remaining = int(db.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1 AND id<>?", (selected_id,),
            ).fetchone()[0])
            if remaining == 0:
                db.rollback()
                raise ValueError("Cannot delete the last active admin.")
        # Snapshot the address before cascading memberships and sessions. The
        # run and application-log tables keep their existing actor text.
        email = str(row["email"])
        db.execute("DELETE FROM users WHERE id=?", (selected_id,))
        db.commit()
    return {"id": selected_id, "email": email, "deleted": True}


def set_user_email(user_id: int, email: str, *, keep_session_token: str | None = None) -> dict[str, Any] | None:
    normalized = email.strip().lower()
    init_db()
    try:
        with closing(get_connection()) as db, db:
            cursor = db.execute(
                "UPDATE users SET email=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (normalized, int(user_id)),
            )
            if cursor.rowcount == 0:
                return None
            if keep_session_token:
                db.execute(
                    "DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
                    (int(user_id), hash_token(keep_session_token)),
                )
            else:
                db.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))
    except sqlite3.IntegrityError as exc:
        raise ValueError("An account with that email already exists.") from exc
    return get_user_by_id(user_id)


def set_user_password(user_id: int, password: str, *, keep_session_token: str | None = None) -> None:
    init_db()
    with closing(get_connection()) as db, db:
        db.execute("UPDATE users SET password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (hash_password(password), int(user_id)))
        if keep_session_token:
            db.execute(
                "DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
                (int(user_id), hash_token(keep_session_token)),
            )
        else:
            db.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))


def create_session(user_id: int, ttl_hours: int | None = None) -> str:
    from app.services.tenant_schema import DEFAULT_TENANT_ID
    token = generate_session_token()
    expires = datetime.now(UTC) + timedelta(hours=ttl_hours or get_settings().session_ttl_hours)
    init_db()
    with closing(get_connection()) as db, db:
        db.execute("INSERT INTO sessions(token_hash,user_id,active_tenant_id,expires_at) VALUES(?,?,?,?)",
                   (hash_token(token), int(user_id), DEFAULT_TENANT_ID, expires.isoformat()))
    return token


def resolve_session(token: str) -> dict[str, Any] | None:
    if not token: return None
    init_db()
    with closing(get_connection()) as db:
        row = db.execute("""SELECT u.*,s.active_tenant_id,s.expires_at FROM sessions s
            JOIN users u ON u.id=s.user_id WHERE s.token_hash=?""", (hash_token(token),)).fetchone()
    if row is None or not row["is_active"]: return None
    try:
        if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(UTC): return None
    except ValueError: return None
    return _public_user(row)


def delete_session(token: str) -> None:
    init_db()
    with closing(get_connection()) as db, db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(token),))


def delete_sessions_for_user(user_id: int) -> None:
    init_db()
    with closing(get_connection()) as db, db:
        db.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))


def delete_other_sessions_for_user(user_id: int, current_token: str) -> None:
    init_db()
    with closing(get_connection()) as db, db:
        db.execute(
            "DELETE FROM sessions WHERE user_id=? AND token_hash<>?",
            (int(user_id), hash_token(current_token)),
        )


def purge_expired_sessions() -> int:
    init_db()
    with closing(get_connection()) as db, db:
        cursor = db.execute("DELETE FROM sessions WHERE expires_at<=?", (datetime.now(UTC).isoformat(),))
        return int(cursor.rowcount)


def get_ai_settings(connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    init_db()
    owns = connection is None
    db = connection or get_connection()
    try:
        row = db.execute("SELECT settings_json FROM ai_settings WHERE id=1").fetchone()
        value = json.loads(str(row[0])) if row else {}
        if not isinstance(value, dict): value = {}
        return {"provider": "mock", "model": "mock-brief-v1", "base_url": "https://api.openai.com/v1",
                "api_keys": {}, **value}
    finally:
        if owns: db.close()


def save_ai_settings(settings: dict[str, Any]) -> dict[str, Any]:
    init_db()
    payload = {key: settings.get(key) for key in ("provider", "model", "base_url", "api_keys")}
    with closing(get_connection()) as db, db:
        db.execute("INSERT INTO ai_settings(id,settings_json) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET settings_json=excluded.settings_json,updated_at=CURRENT_TIMESTAMP",
                   (json.dumps(payload, sort_keys=True),))
    return get_ai_settings()


def _ai_profile_as_settings(profile: dict[str, Any]) -> dict[str, Any]:
    provider = str(profile.get("provider") or "")
    key = str(profile.get("api_key") or "")
    return {"profile_id": profile.get("id"), "provider": provider,
            "model": str(profile.get("model") or ""), "base_url": str(profile.get("base_url") or ""),
            "api_keys": {provider: key} if key else {}, "credential_ref": str(profile.get("credential_ref") or "")}


def list_ai_provider_profiles() -> list[dict[str, Any]]:
    init_db()
    with closing(get_connection()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM ai_provider_profiles ORDER BY priority,id").fetchall()]


def get_ai_provider_profile(profile_id: int) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db:
        return _dict(db.execute("SELECT * FROM ai_provider_profiles WHERE id=?", (int(profile_id),)).fetchone())


def save_ai_provider_profile(payload: dict[str, Any], *, profile_id: int | None = None) -> dict[str, Any]:
    init_db()
    values = (str(payload.get("name") or ""), str(payload.get("provider") or "mock"),
              str(payload.get("protocol") or "openai_chat"), str(payload.get("model") or ""),
              str(payload.get("base_url") or ""), str(payload.get("api_key") or ""),
              str(payload.get("credential_ref") or ""), int(payload.get("priority") or 100),
              int(bool(payload.get("enabled", True))), str(payload.get("reasoning_effort") or ""))
    with closing(get_connection()) as db, db:
        if profile_id is None:
            cursor=db.execute("""INSERT INTO ai_provider_profiles(name,provider,protocol,model,base_url,api_key,credential_ref,priority,enabled,reasoning_effort)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", values)
            profile_id=int(cursor.lastrowid)
        else:
            db.execute("""UPDATE ai_provider_profiles SET name=?,provider=?,protocol=?,model=?,base_url=?,api_key=?,credential_ref=?,priority=?,enabled=?,reasoning_effort=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (*values,int(profile_id)))
    return get_ai_provider_profile(int(profile_id)) or {}


def delete_ai_provider_profile(profile_id: int) -> bool:
    init_db()
    with closing(get_connection()) as db, db:
        return bool(db.execute("DELETE FROM ai_provider_profiles WHERE id=?", (int(profile_id),)).rowcount)


def insert_ai_artifact(*, artifact_type: str, content: str, project_id: str = "", tenant_id: str = "", metadata: dict[str, Any] | None = None, **_: Any) -> int:
    init_db()
    with closing(get_connection()) as db, db:
        cursor=db.execute("INSERT INTO ai_artifacts(tenant_id,project_id,artifact_type,content,metadata_json) VALUES(?,?,?,?,?)",
            (tenant_id,project_id,artifact_type,content,json.dumps(metadata or {},sort_keys=True)))
        return int(cursor.lastrowid)


def get_latest_ai_artifact(*, project_id: str = "", artifact_type: str = "", **_: Any) -> dict[str, Any] | None:
    init_db()
    with closing(get_connection()) as db:
        row=db.execute("SELECT * FROM ai_artifacts WHERE project_id=? AND artifact_type=? ORDER BY id DESC LIMIT 1",(project_id,artifact_type)).fetchone()
        return _dict(row)


def __getattr__(name: str) -> Any:
    """Fail clearly if a removed private data API is called by stale code."""
    def removed(*_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError(f"database.{name} is not part of the Lynewright schema")
    return removed
