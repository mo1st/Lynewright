"""Reviewed, reusable Operator workflow plans.

Workflow drafts are deliberately data-only. They reuse the same action-plan
contract and reducer as an interactive run; this service only owns lifecycle
and review state, never a second tool runtime.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from app.services.operator_runtime import store as opstore
from app.services.operator_runtime.action_plan import validate_action_plan
from app.services.operator_runtime.schema import init_operator_schema
from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformValidationError


class OperatorWorkflowDraftService:
    def __init__(self, *, store: Any, tenant_id: str) -> None:
        self.store = store
        self.tenant_id = str(tenant_id)

    @staticmethod
    def _json(raw: Any, fallback: Any) -> Any:
        try:
            value = json.loads(str(raw or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback
        return value

    @classmethod
    def _public(cls, row: Any) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "tenant_id": str(row["tenant_id"]),
            "project_id": str(row["project_id"]),
            "name": str(row["name"]),
            "objective_template": str(row["objective_template"]),
            "plan": cls._json(row["plan_json"], {}),
            "contract_revisions": cls._json(row["contract_revisions_json"], {}),
            "status": str(row["status"]),
            "version": int(row["version"] or 1),
            "reviewed_by": str(row["reviewed_by"] or ""),
            "reviewed_at": row["reviewed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row(self, db: Any, workflow_id: str) -> Any:
        row = db.execute(
            "SELECT * FROM operator_workflow_drafts WHERE tenant_id=? AND id=?",
            (self.tenant_id, str(workflow_id)),
        ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Operator workflow draft was not found.")
        return row

    def create(
        self, *, project_id: str, name: str, objective_template: str,
        plan: dict[str, Any], contract_revisions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(project_id).strip() or not str(name).strip() or not str(objective_template).strip():
            raise PlatformValidationError("Workflow project, name, and objective_template are required.")
        try:
            normalized = validate_action_plan(plan)
        except Exception as exc:
            raise PlatformValidationError(f"Workflow plan is invalid: {str(exc)[:500]}") from exc
        workflow_id = "owf_" + secrets.token_hex(12)
        with self.store.connection() as db, db:
            init_operator_schema(db)
            try:
                db.execute(
                    """INSERT INTO operator_workflow_drafts
                       (id,tenant_id,project_id,name,objective_template,plan_json,contract_revisions_json)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        workflow_id, self.tenant_id, str(project_id), str(name).strip(),
                        str(objective_template).strip(), opstore.bound_json_text(normalized, limit=64_000),
                        opstore.bound_json_text(contract_revisions or {}, limit=16_000),
                    ),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise PlatformConflictError("A workflow draft with this name already exists.") from exc
                raise
            return self._public(self._row(db, workflow_id))

    def list(self, *, project_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses = ["tenant_id=?"]
        params: list[Any] = [self.tenant_id]
        if project_id:
            clauses.append("project_id=?")
            params.append(str(project_id))
        if status:
            if status not in {"draft", "reviewed", "enabled", "stale"}:
                raise PlatformValidationError("Unknown workflow status.")
            clauses.append("status=?")
            params.append(status)
        with self.store.connection() as db:
            init_operator_schema(db)
            rows = db.execute(
                f"SELECT * FROM operator_workflow_drafts WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id DESC",
                params,
            ).fetchall()
            return [self._public(row) for row in rows]

    def get(self, workflow_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            init_operator_schema(db)
            return self._public(self._row(db, workflow_id))

    def review(self, workflow_id: str, *, reviewer: str, approve: bool) -> dict[str, Any]:
        with self.store.connection() as db, db:
            init_operator_schema(db)
            row = self._row(db, workflow_id)
            current = str(row["status"])
            if current not in {"draft", "reviewed"}:
                raise PlatformConflictError(f"Workflow is {current}; only a draft can be reviewed.")
            status = "reviewed" if approve else "draft"
            db.execute(
                """UPDATE operator_workflow_drafts
                   SET status=?, reviewed_by=?, reviewed_at=CASE WHEN ?='reviewed' THEN CURRENT_TIMESTAMP ELSE NULL END,
                       updated_at=CURRENT_TIMESTAMP, version=version+1 WHERE tenant_id=? AND id=?""",
                (status, str(reviewer or "")[:160], status, self.tenant_id, str(workflow_id)),
            )
            return self._public(self._row(db, workflow_id))

    def enable(self, workflow_id: str) -> dict[str, Any]:
        with self.store.connection() as db, db:
            init_operator_schema(db)
            row = self._row(db, workflow_id)
            if str(row["status"]) != "reviewed":
                raise PlatformConflictError("A workflow must be reviewed before it can be enabled.")
            db.execute(
                "UPDATE operator_workflow_drafts SET status='enabled',updated_at=CURRENT_TIMESTAMP,version=version+1 WHERE tenant_id=? AND id=?",
                (self.tenant_id, str(workflow_id)),
            )
            return self._public(self._row(db, workflow_id))


__all__ = ["OperatorWorkflowDraftService"]
