"""Shared, revisioned planning and preview-line authoring service."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from typing import Any

from app.models.automation_plans import AutomationPlanInput
from app.services.line_engine import ProductionLineEngine, validate_definition
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformValidationError,
    PlatformStore,
)

_SENSITIVE_KEY = re.compile(r"(?:secret|password|token|api[_-]?key|access[_-]?key|authorization|client[_-]?secret)", re.I)
_MAX_PLAN_BYTES = 256_000
_AUTHORIZATION_TTL_SECONDS = 30 * 24 * 60 * 60


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PlatformValidationError("Plan data must contain only finite JSON values.") from exc


def _check_json_size(value: Any) -> None:
    if len(_canonical(value).encode("utf-8")) > _MAX_PLAN_BYTES:
        raise PlatformValidationError("Automation plan data exceeds the 256 KB limit.")


def _reject_inline_secrets(value: Any, *, path: str = "plan", depth: int = 0) -> None:
    if depth > 24:
        raise PlatformValidationError("Automation plan data is nested too deeply.")
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            if _SENSITIVE_KEY.search(name):
                raise PlatformValidationError(
                    f"Plan field {path}.{name} looks like a secret. Use a configured credential reference."
                )
            _reject_inline_secrets(item, path=f"{path}.{name}", depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 1_000:
            raise PlatformValidationError("Automation plan arrays are limited to 1,000 items.")
        for index, item in enumerate(value):
            _reject_inline_secrets(item, path=f"{path}[{index}]", depth=depth + 1)


def public_operation_cards(candidates: list[dict[str, Any]], *, max_bytes: int = 256_000) -> list[dict[str, Any]]:
    """Project operation descriptors to bounded schema cards safe for assistants."""
    fields = (
        "operation_id", "revision", "integration_id", "contract", "operation",
        "capability", "risk_class", "description", "output_schema", "kind",
    )
    items: list[dict[str, Any]] = []
    total_bytes = 0
    for candidate in candidates[:50]:
        card = {key: candidate.get(key) for key in fields}
        card["schema"] = candidate.get("input_schema")
        card_bytes = len(_canonical(card).encode("utf-8"))
        if total_bytes + card_bytes > max(1, int(max_bytes)):
            break
        items.append(card)
        total_bytes += card_bytes
    return items


class AutomationPlanService:
    """Persists immutable revisions and creates only disabled preview lines."""

    def __init__(self, store: PlatformStore | None = None) -> None:
        self.store = store or PlatformStore()
        with self.store.connection() as db:
            self.ensure_schema(db)

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS automation_plans (
                tenant_id TEXT NOT NULL,
                id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                owner_id TEXT NOT NULL DEFAULT '',
                originating_client_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                current_revision INTEGER NOT NULL DEFAULT 1,
                expires_at_epoch INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id,id)
            );
            CREATE INDEX IF NOT EXISTS idx_automation_plans_project
                ON automation_plans(tenant_id,project_id,status,updated_at DESC);
            CREATE TABLE IF NOT EXISTS automation_plan_revisions (
                tenant_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                plan_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id,plan_id,revision),
                UNIQUE (tenant_id,plan_id,plan_hash),
                FOREIGN KEY (tenant_id,plan_id) REFERENCES automation_plans(tenant_id,id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS automation_authorizations (
                tenant_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                plan_hash TEXT NOT NULL,
                approved_by TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at_epoch INTEGER NOT NULL,
                PRIMARY KEY (tenant_id,plan_id,revision),
                FOREIGN KEY (tenant_id,plan_id,revision)
                    REFERENCES automation_plan_revisions(tenant_id,plan_id,revision) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_automation_authorizations_expiry
                ON automation_authorizations(tenant_id,expires_at_epoch);
            CREATE TRIGGER IF NOT EXISTS automation_plan_revisions_no_update
                BEFORE UPDATE ON automation_plan_revisions
                BEGIN SELECT RAISE(ABORT,'automation plan revisions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS automation_plan_revisions_no_delete
                BEFORE DELETE ON automation_plan_revisions
                BEGIN SELECT RAISE(ABORT,'automation plan revisions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS automation_authorizations_no_update
                BEFORE UPDATE ON automation_authorizations
                BEGIN SELECT RAISE(ABORT,'automation approvals are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS automation_authorizations_no_delete
                BEFORE DELETE ON automation_authorizations
                BEGIN SELECT RAISE(ABORT,'automation approvals are append-only'); END;
            """
        )
        plan_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(automation_plans)").fetchall()}
        if "expires_at_epoch" not in plan_columns:
            db.execute("ALTER TABLE automation_plans ADD COLUMN expires_at_epoch INTEGER NOT NULL DEFAULT 0")

    def create_or_revise(
        self, value: AutomationPlanInput | dict[str, Any], *, tenant_id: str,
        created_by: str, originating_client_id: str,
        operation_directory: Any, operation_actor: Any,
    ) -> dict[str, Any]:
        try:
            payload = value if isinstance(value, AutomationPlanInput) else AutomationPlanInput.model_validate(value)
        except Exception as exc:
            raise PlatformValidationError(str(exc)) from exc
        tenant_id = str(tenant_id or "").strip()
        project_id = str(payload.project_id).strip()
        objective = str(payload.objective).strip()
        line_name = str(payload.line_name).strip()
        if not tenant_id or not project_id or not objective or not line_name:
            raise PlatformValidationError("Tenant, project, objective, and line name are required.")
        _reject_inline_secrets(payload.definition, path="definition")
        _reject_inline_secrets(payload.constraints, path="constraints")
        _check_json_size({
            "project_id": project_id, "objective": objective, "line_name": line_name,
            "definition": payload.definition, "constraints": payload.constraints,
        })

        normalized = validate_definition(
            payload.definition, operation_directory=operation_directory,
            actor=operation_actor, project_id=project_id,
        )
        plan_id = payload.plan_id or f"apl_{secrets.token_hex(16)}"
        expected_revision = payload.expected_revision
        revision = (expected_revision + 1) if expected_revision is not None else 1
        line_id = f"preview-{plan_id[4:]}-{revision}"
        revision_payload = {
            "schema_version": 1,
            "project_id": project_id,
            "objective": objective,
            "constraints": payload.constraints,
            "line": {"id": line_id, "name": line_name, "definition": normalized},
            "operation_snapshots": self._operation_snapshots(
                normalized, operation_directory=operation_directory,
                operation_actor=operation_actor, project_id=project_id,
            ),
        }
        _check_json_size(revision_payload)
        plan_hash = hashlib.sha256(_canonical(revision_payload).encode("utf-8")).hexdigest()

        with self.store.connection() as db, db:
            if expected_revision is None:
                db.execute(
                    "INSERT INTO automation_plans(tenant_id,id,project_id,owner_id,originating_client_id,status,current_revision,expires_at_epoch) "
                    "VALUES(?,?,?,?,?,'awaiting_authorization',1,?)",
                    (tenant_id, plan_id, project_id, str(created_by or ""), str(originating_client_id or ""),
                     int(time.time()) + _AUTHORIZATION_TTL_SECONDS),
                )
            else:
                row = db.execute(
                    "SELECT current_revision,status,project_id FROM automation_plans WHERE tenant_id=? AND id=?",
                    (tenant_id, plan_id),
                ).fetchone()
                if row is None or str(row["project_id"]) != project_id:
                    raise PlatformNotFoundError("Automation plan was not found in this project.")
                if int(row["current_revision"]) != expected_revision:
                    raise PlatformConflictError("Automation plan changed. Read the latest revision before revising it.")
                if str(row["status"]) in {"provisioning", "active"}:
                    raise PlatformConflictError("An automation plan cannot be revised while it is being applied or active.")
                db.execute(
                    "UPDATE automation_plans SET status='awaiting_authorization',current_revision=?,expires_at_epoch=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE tenant_id=? AND id=? AND current_revision=?",
                    (revision, int(time.time()) + _AUTHORIZATION_TTL_SECONDS, tenant_id, plan_id, expected_revision),
                )
            db.execute(
                "INSERT INTO automation_plan_revisions(tenant_id,plan_id,revision,plan_hash,payload_json,created_by) "
                "VALUES(?,?,?,?,?,?)",
                (tenant_id, plan_id, revision, plan_hash, _canonical(revision_payload), str(created_by or "")),
            )
        return self.get_plan(plan_id, tenant_id=tenant_id, project_id=project_id)

    @staticmethod
    def _operation_snapshots(
        definition: dict[str, Any], *, operation_directory: Any,
        operation_actor: Any, project_id: str,
    ) -> list[dict[str, Any]]:
        operation_ids: set[str] = set()
        for stage in definition.get("stages", []):
            if not isinstance(stage, dict):
                continue
            operation_id = str(stage.get("operation_id") or "").strip()
            if operation_id:
                operation_ids.add(operation_id)
            status = stage.get("status_operation")
            if isinstance(status, dict) and status.get("operation_id"):
                operation_ids.add(str(status["operation_id"]))
        snapshots = []
        for operation_id in sorted(operation_ids):
            resolved = operation_directory.resolve_operation(
                operation_actor, project_id=project_id, operation_id=operation_id,
            )
            descriptor = resolved["descriptor"]
            snapshots.append({
                key: descriptor.get(key) for key in (
                    "operation_id", "revision", "integration_id", "connection_id",
                    "contract", "operation", "capability", "risk_class",
                )
            })
        return snapshots

    def get_plan(self, plan_id: str, *, tenant_id: str, project_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            plan = db.execute(
                "SELECT * FROM automation_plans WHERE tenant_id=? AND id=? AND project_id=?",
                (str(tenant_id), str(plan_id), str(project_id)),
            ).fetchone()
            if plan is None:
                raise PlatformNotFoundError("Automation plan was not found in this project.")
            revision = db.execute(
                "SELECT * FROM automation_plan_revisions WHERE tenant_id=? AND plan_id=? AND revision=?",
                (str(tenant_id), str(plan_id), int(plan["current_revision"])),
            ).fetchone()
            authorization = db.execute(
                "SELECT * FROM automation_authorizations WHERE tenant_id=? AND plan_id=? AND revision=?",
                (str(tenant_id), str(plan_id), int(plan["current_revision"])),
            ).fetchone()
        if revision is None:
            raise PlatformConflictError("Automation plan revision is unavailable.")
        payload = json.loads(str(revision["payload_json"]))
        expires_at = int(authorization["expires_at_epoch"]) if authorization is not None else None
        now = int(time.time())
        line_preview = payload["line"] | {"enabled": False, "persisted": False}
        if str(plan["status"]) == "ready":
            try:
                saved_line = ProductionLineEngine(self.store, tenant_id=str(tenant_id)).db.get_line(
                    str(payload["line"]["id"]),
                )
            except PlatformNotFoundError:
                pass
            else:
                if str(saved_line["project_id"]) == str(project_id):
                    line_preview["enabled"] = bool(saved_line["enabled"])
                    line_preview["persisted"] = True
        return {
            "id": str(plan["id"]),
            "project_id": str(plan["project_id"]),
            "status": str(plan["status"]),
            "revision": int(plan["current_revision"]),
            "expires_at_epoch": int(plan["expires_at_epoch"]),
            "plan_hash": str(revision["plan_hash"]),
            "objective": payload["objective"],
            "constraints": payload["constraints"],
            "line_preview": line_preview,
            "operation_snapshots": payload["operation_snapshots"],
            "authorization": {
                "approved": bool(authorization is not None and expires_at is not None and expires_at > now
                                  and int(plan["expires_at_epoch"]) > now
                                  and str(authorization["plan_hash"]) == str(revision["plan_hash"])),
                "approved_by": str(authorization["approved_by"]) if authorization is not None else "",
                "approved_at": str(authorization["created_at"]) if authorization is not None else "",
                "expires_at_epoch": expires_at,
            },
        }

    def list_plans(self, *, tenant_id: str, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id FROM automation_plans WHERE tenant_id=? AND project_id=? "
                "ORDER BY updated_at DESC,id LIMIT ?",
                (str(tenant_id), str(project_id), max(1, min(int(limit), 100))),
            ).fetchall()
        return [self.get_plan(str(row["id"]), tenant_id=tenant_id, project_id=project_id) for row in rows]

    def authorize(
        self, plan_id: str, *, tenant_id: str, project_id: str,
        revision: int, plan_hash: str, approved_by: str,
    ) -> dict[str, Any]:
        with self.store.connection() as db, db:
            plan = db.execute(
                "SELECT current_revision,status FROM automation_plans WHERE tenant_id=? AND id=? AND project_id=?",
                (str(tenant_id), str(plan_id), str(project_id)),
            ).fetchone()
            if plan is None:
                raise PlatformNotFoundError("Automation plan was not found in this project.")
            if int(plan["current_revision"]) != int(revision):
                raise PlatformConflictError("Only the current automation plan revision can be approved.")
            if str(plan["status"]) in {"provisioning", "ready", "active"}:
                raise PlatformConflictError("This plan is already being applied or has been applied.")
            plan_expiration = db.execute(
                "SELECT expires_at_epoch FROM automation_plans WHERE tenant_id=? AND id=?",
                (str(tenant_id), str(plan_id)),
            ).fetchone()
            if plan_expiration is None or int(plan_expiration["expires_at_epoch"]) <= int(time.time()):
                raise PlatformConflictError("This automation plan has expired. Create a new revision before approving it.")
            stored = db.execute(
                "SELECT plan_hash FROM automation_plan_revisions WHERE tenant_id=? AND plan_id=? AND revision=?",
                (str(tenant_id), str(plan_id), int(revision)),
            ).fetchone()
            if stored is None or not secrets.compare_digest(str(stored["plan_hash"]), str(plan_hash)):
                raise PlatformConflictError("The plan changed. Review its current revision before approving it.")
            prior = db.execute(
                "SELECT * FROM automation_authorizations WHERE tenant_id=? AND plan_id=? AND revision=?",
                (str(tenant_id), str(plan_id), int(revision)),
            ).fetchone()
            if prior is None:
                scope = {"actions": ["create_disabled_preview_line"], "project_id": str(project_id)}
                db.execute(
                    "INSERT OR IGNORE INTO automation_authorizations(tenant_id,plan_id,revision,plan_hash,approved_by,scope_json,expires_at_epoch) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (str(tenant_id), str(plan_id), int(revision), str(plan_hash), str(approved_by),
                     _canonical(scope), int(time.time()) + _AUTHORIZATION_TTL_SECONDS),
                )
            db.execute(
                "UPDATE automation_plans SET status='authorized',updated_at=CURRENT_TIMESTAMP "
                "WHERE tenant_id=? AND id=? AND current_revision=? AND status!='ready'",
                (str(tenant_id), str(plan_id), int(revision)),
            )
        return self.get_plan(plan_id, tenant_id=tenant_id, project_id=project_id)

    def apply_preview(
        self, plan_id: str, *, tenant_id: str, project_id: str,
        revision: int, plan_hash: str, created_by: str,
        operation_directory: Any, operation_actor: Any,
    ) -> dict[str, Any]:
        plan = self.get_plan(plan_id, tenant_id=tenant_id, project_id=project_id)
        if plan["revision"] != int(revision) or not secrets.compare_digest(plan["plan_hash"], str(plan_hash)):
            raise PlatformConflictError("The requested plan revision is no longer current.")
        if plan["status"] == "ready":
            return plan
        if int(plan["expires_at_epoch"]) <= int(time.time()):
            raise PlatformConflictError("This automation plan has expired. Create a new revision before applying it.")
        if not plan["authorization"]["approved"]:
            raise PlatformConflictError("A tenant administrator must approve this plan revision before it can be applied.")

        self._verify_operation_snapshots(
            plan["operation_snapshots"], operation_directory=operation_directory,
            operation_actor=operation_actor, project_id=project_id,
        )
        with self.store.connection() as db, db:
            authorization = db.execute(
                """SELECT p.current_revision,p.expires_at_epoch AS plan_expires,
                          a.plan_hash,a.expires_at_epoch AS authorization_expires
                   FROM automation_plans p JOIN automation_authorizations a
                     ON a.tenant_id=p.tenant_id AND a.plan_id=p.id AND a.revision=?
                   WHERE p.tenant_id=? AND p.id=? AND p.project_id=?""",
                (int(revision), str(tenant_id), str(plan_id), str(project_id)),
            ).fetchone()
            now = int(time.time())
            if (authorization is None or int(authorization["current_revision"]) != int(revision)
                    or int(authorization["plan_expires"]) <= now
                    or int(authorization["authorization_expires"]) <= now
                    or not secrets.compare_digest(str(authorization["plan_hash"]), str(plan_hash))):
                raise PlatformConflictError("This plan approval expired or no longer covers the current revision.")
            cursor = db.execute(
                "UPDATE automation_plans SET status='provisioning',updated_at=CURRENT_TIMESTAMP "
                "WHERE tenant_id=? AND id=? AND project_id=? AND current_revision=? AND status!='ready'",
                (str(tenant_id), str(plan_id), str(project_id), int(revision)),
            )
            if cursor.rowcount != 1:
                current = db.execute(
                    "SELECT status,current_revision FROM automation_plans WHERE tenant_id=? AND id=?",
                    (str(tenant_id), str(plan_id)),
                ).fetchone()
                if current is None:
                    raise PlatformNotFoundError("Automation plan was not found.")
                if int(current["current_revision"]) != int(revision):
                    raise PlatformConflictError("The requested plan revision is no longer current.")
                if str(current["status"]) != "ready":
                    raise PlatformConflictError("This automation plan is already being applied.")

        preview = plan["line_preview"]
        engine = ProductionLineEngine(self.store, tenant_id=tenant_id)
        try:
            engine.db.put_line(
                str(preview["id"]), name=str(preview["name"]), project_id=str(project_id),
                definition=preview["definition"], enabled=False, actor_id=str(created_by or "automation-plan"),
                operation_directory=operation_directory, operation_actor=operation_actor, create_only=True,
            )
        except PlatformConflictError:
            # A retry after a lost response is safe only when the exact disabled
            # line from this revision already exists.
            try:
                line = engine.db.get_line(str(preview["id"]))
            except PlatformNotFoundError:
                self._set_status(tenant_id, plan_id, revision, "authorized")
                raise
            if (line["project_id"] != str(project_id) or line["name"] != preview["name"]
                    or line["definition"] != preview["definition"] or line["enabled"]):
                self._set_status(tenant_id, plan_id, revision, "failed")
                raise PlatformConflictError("The preview line id is already used by different line data.")
        except Exception:
            self._set_status(tenant_id, plan_id, revision, "authorized")
            raise
        self._set_status(tenant_id, plan_id, revision, "ready")
        return self.get_plan(plan_id, tenant_id=tenant_id, project_id=project_id)

    @staticmethod
    def _verify_operation_snapshots(
        snapshots: list[dict[str, Any]], *, operation_directory: Any,
        operation_actor: Any, project_id: str,
    ) -> None:
        for snapshot in snapshots:
            resolved = operation_directory.resolve_operation(
                operation_actor, project_id=project_id,
                operation_id=str(snapshot.get("operation_id") or ""),
            )
            descriptor = resolved["descriptor"]
            if any(descriptor.get(key) != snapshot.get(key) for key in snapshot):
                raise PlatformConflictError(
                    "An installed operation changed after plan approval. Create a new plan revision and review it."
                )

    def _set_status(self, tenant_id: str, plan_id: str, revision: int, status: str) -> None:
        with self.store.connection() as db, db:
            db.execute(
                "UPDATE automation_plans SET status=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE tenant_id=? AND id=? AND current_revision=?",
                (str(status), str(tenant_id), str(plan_id), int(revision)),
            )
