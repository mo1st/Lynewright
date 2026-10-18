"""Provider-neutral execution for data-defined production lines."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID


_MAX_JSON_BYTES = 64 * 1024
_TEMPLATE = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+\??)\s*\}\}")
_STAGE_KINDS = {"operation", "stepped_operation", "async_operation", "gate:choice", "gate:review", "notify", "condition"}
_DB_LOCK = __import__("threading").RLock()


def _encode(value: Any, label: str) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise PlatformValidationError(f"{label} must contain JSON values only.") from exc
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise PlatformValidationError(f"{label} exceeds the 64 KiB limit.")
    return encoded


def _compact_result(value: dict[str, Any]) -> dict[str, Any]:
    try:
        _encode(value, "Stage output")
        return value
    except PlatformValidationError:
        compact = {"status": str(value.get("status") or ""),
                   "output_ref": str(value.get("output_ref") or value.get("ref") or ""),
                   "truncated": True}
        if isinstance(value.get("progress"), dict):
            compact["progress"] = value["progress"]
        if isinstance(value.get("waiting"), dict):
            compact["waiting"] = value["waiting"]
        return compact


def _path_value(context: dict[str, Any], path: str) -> Any:
    optional = path.endswith("?")
    if optional:
        path = path[:-1]
    is_action_input = path.startswith("action.") and isinstance(context.get("action"), dict)
    if not (path == "run.vars" or path.startswith("run.vars.") or path.startswith("stages.") or is_action_input):
        raise PlatformValidationError(f"Unsupported template reference: {path}")
    value: Any = {"run": {"vars": context.get("vars", {})}, "stages": context.get("stages", {}), "action": context.get("action", {})}
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            if optional:
                return None
            raise PlatformValidationError(f"Template reference has no value: {path}")
    return value


def _resolve(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {str(key): _resolve(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, context) for item in value]
    if not isinstance(value, str):
        return value
    match = _TEMPLATE.fullmatch(value)
    if match:
        return _path_value(context, match.group(1))
    return _TEMPLATE.sub(lambda found: str(_path_value(context, found.group(1))), value)


def _template_paths(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [path for item in value.values() for path in _template_paths(item)]
    if isinstance(value, list):
        return [path for item in value for path in _template_paths(item)]
    if isinstance(value, str):
        return [match.group(1) for match in _TEMPLATE.finditer(value)]
    return []


def _dynamic(value: Any) -> bool:
    return isinstance(value, str) and _TEMPLATE.search(value) is not None


def _matches_schema_type(value: Any, schema_type: Any) -> bool:
    types = schema_type if isinstance(schema_type, list) else [schema_type]
    for kind in types:
        if kind == "null" and value is None:
            return True
        if kind == "object" and isinstance(value, dict):
            return True
        if kind == "array" and isinstance(value, list):
            return True
        if kind == "string" and isinstance(value, str):
            return True
        if kind == "boolean" and isinstance(value, bool):
            return True
        if kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
    return False


def _validate_arguments(stage_id: str, operation: str, args_template: Any, defaults: Any, schema: Any) -> None:
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return
    if isinstance(args_template, str) and _TEMPLATE.fullmatch(args_template):
        return
    values = dict(defaults) if isinstance(defaults, dict) else {}
    if isinstance(args_template, dict):
        values.update(args_template)
    properties = schema.get("properties") or {}
    for key in values:
        if key not in properties and schema.get("additionalProperties") is False:
            raise PlatformValidationError(f"Stage {stage_id} supplies an unknown argument for {operation}: {key}.")
    missing = [key for key in schema.get("required", []) if key not in values and key not in {"project_id", "idempotency_key"}]
    if missing:
        raise PlatformValidationError(f"Stage {stage_id} is missing required arguments for {operation}: {', '.join(missing)}.")
    for key, value in values.items():
        if _dynamic(value):
            continue
        field_schema = properties.get(key)
        if isinstance(field_schema, dict) and field_schema.get("type") and not _matches_schema_type(value, field_schema["type"]):
            raise PlatformValidationError(f"Stage {stage_id} argument {key} does not match the schema for {operation}.")
        if isinstance(field_schema, dict) and isinstance(field_schema.get("enum"), list) and value not in field_schema["enum"]:
            raise PlatformValidationError(f"Stage {stage_id} argument {key} is outside the allowed values for {operation}.")


def _media_schema_fields(schema: Any, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    if not isinstance(schema, dict):
        return []
    fields: list[tuple[tuple[str, ...], str]] = []
    fmt = schema.get("format")
    if fmt in {"lynewright-media", "lynewright-upload"}:
        fields.append((prefix, str(fmt)))
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, child in properties.items():
            fields.extend(_media_schema_fields(child, (*prefix, str(key))))
    return fields


def _nested_get(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for part in path:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _nested_set(value: dict[str, Any], path: tuple[str, ...], replacement: Any) -> None:
    current: Any = value
    for part in path[:-1]:
        if not isinstance(current, dict) or not isinstance(current.get(part), dict):
            return
        current = current[part]
    if path and isinstance(current, dict):
        current[path[-1]] = replacement


def _wire_media_arguments(
    args: dict[str, Any], args_template: Any, schema: Any, *, tenant_id: str,
    project_id: str, run_id: str, stage_id: str, actor: Any, internal: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    from app.services.integration_artifacts import extract_media_refs
    from app.services.integration_media_transfer import UPLOAD_MEDIA_SCHEME
    from app.services.media_links import MediaLinksService

    fields = _media_schema_fields(schema)
    if not fields:
        return args
    links = MediaLinksService(tenant_id=tenant_id)
    result = dict(args)
    for path, media_format in fields:
        raw_template = _nested_get(args_template, path)
        if not isinstance(raw_template, str):
            continue
        template_match = _TEMPLATE.fullmatch(raw_template)
        if template_match is None:
            continue
        source_path = template_match.group(1).removesuffix("?")
        if not (
            source_path.startswith("run.vars.")
            or (source_path.startswith("stages.") and ".output" in source_path)
        ):
            continue
        value = _nested_get(result, path)
        if isinstance(value, str) and value.startswith("managed://"):
            value = {"media_url": value}
        refs = extract_media_refs(value)
        if len(refs) != 1 or not refs[0].get("media_id"):
            raise PlatformValidationError(
                f"Stage {stage_id} argument {'.'.join(path)} must resolve to exactly one managed media reference."
            )
        media_id = str(refs[0]["media_id"])
        if media_format == "lynewright-media":
            token = links.issue(
                media_id, ttl_seconds=int(timeout_seconds + 0.999999) + 60, purpose="line-operation",
                actor=actor, project_id=project_id, run_id=run_id,
            )
            replacement = links.public_url(token, tenant_id=tenant_id, internal=internal)
        else:
            replacement = UPLOAD_MEDIA_SCHEME + media_id
        _nested_set(result, path, replacement)
    return result


def _operation_directory() -> dict[tuple[str, str], dict[str, Any]]:
    from app.services.integration_host import contributions
    from app.services.operator_dispatch import discover_contract_operations

    return {
        (contract, operation): declared
        for contract, operation, declared in discover_contract_operations(contributions)
    }


def _catalogue_operation(
    catalogue: Any, *, actor: Any, project_id: str, operation_id: str = "",
    contract: str = "", operation: str = "", integration_id: str = "",
    connection_id: str = "",
) -> dict[str, Any]:
    try:
        return catalogue.resolve_operation(
            actor, project_id=project_id, operation_id=operation_id,
            contract=contract, operation=operation, integration_id=integration_id,
            connection_id=connection_id,
        )["descriptor"]
    except PlatformOperationError as exc:
        raise PlatformValidationError(str(exc)) from exc


def _async_status_template(stage_id: str, job_id_path: str, status_argument_path: str) -> dict[str, Any]:
    parts = [part for part in status_argument_path.split(".") if part]
    if not parts:
        raise PlatformValidationError(f"Stage {stage_id} async status_argument_path must be a dotted input path.")
    template: dict[str, Any] = {}
    current = template
    for part in parts[:-1]:
        child: dict[str, Any] = {}
        current[part] = child
        current = child
    current[parts[-1]] = "{{stages." + stage_id + ".output." + job_id_path + "}}"
    return template


def validate_definition(
    definition: Any, *,
    operation_directory: Any = None,
    actor: Any = None,
    project_id: str = "",
) -> dict[str, Any]:
    if not isinstance(definition, dict):
        raise PlatformValidationError("A line definition must be an object.")
    stages = definition.get("stages")
    if not isinstance(stages, list) or not stages or len(stages) > 100:
        raise PlatformValidationError("A line definition needs between 1 and 100 stages.")
    ids: set[str] = set()
    operations = operation_directory if operation_directory is not None else _operation_directory()
    for stage in stages:
        if not isinstance(stage, dict):
            raise PlatformValidationError("Each stage must be an object.")
        stage_id = str(stage.get("id") or "").strip()
        kind = str(stage.get("kind") or "").strip()
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", stage_id) or stage_id in ids:
            raise PlatformValidationError("Stage ids must be unique, non-empty identifiers.")
        ids.add(stage_id)
        if kind not in _STAGE_KINDS:
            raise PlatformValidationError(f"Unsupported stage kind: {kind}")
        for path in _template_paths(stage):
            path = path[:-1] if path.endswith("?") else path
            if not (path == "run.vars" or path.startswith("run.vars.") or path.startswith("stages.")):
                raise PlatformValidationError(f"Unsupported template reference: {path}")
            if path.startswith("stages."):
                referenced_stage = path.split(".", 2)[1] if len(path.split(".")) > 1 else ""
                if referenced_stage not in ids:
                    raise PlatformValidationError(f"Stage {stage_id} references a stage that must run earlier: {referenced_stage}.")
        if kind in {"operation", "stepped_operation", "async_operation"}:
            contract, operation = str(stage.get("contract") or "").strip(), str(stage.get("operation") or "").strip()
            operation_id = str(stage.get("operation_id") or "").strip()
            if not contract or not operation:
                raise PlatformValidationError(f"Stage {stage_id} must name a contract and operation.")
            if hasattr(operations, "resolve_operation"):
                declared = _catalogue_operation(
                    operations, actor=actor, project_id=project_id, operation_id=operation_id,
                    contract=contract, operation=operation,
                )
                stage["operation_id"] = str(declared["operation_id"])
                argument_schema = declared.get("input_schema")
                if declared.get("kind") == "mcp" and any(
                    media_format == "lynewright-upload" for _path, media_format in _media_schema_fields(argument_schema)
                ):
                    raise PlatformValidationError(
                        f"Stage {stage_id} uses lynewright-upload, but MCP tools do not accept uploads; use lynewright-media."
                    )
            else:
                key = (contract, operation)
                if key not in operations:
                    if isinstance(operations, dict):
                        raise PlatformValidationError(f"Stage {stage_id} references an operation that is not in the operation_directory.")
                    raise PlatformValidationError(f"Stage {stage_id} references an operation that is not in the operation_directory.")
                declared = operations[key] if isinstance(operations, dict) else {}
                argument_schema = declared.get("schema") if isinstance(declared, dict) else None
            args_template = stage.get("args_template", {})
            if not isinstance(args_template, (dict, str)):
                raise PlatformValidationError(f"Stage {stage_id} args_template must be an object or a context reference.")
            if not isinstance(operations, set):
                _validate_arguments(stage_id, operation, args_template, stage.get("args_defaults"), argument_schema)
            if kind == "async_operation":
                status_operation = stage.get("status_operation")
                async_declaration = declared.get("async") if isinstance(declared.get("async"), dict) else None
                if not isinstance(status_operation, dict) and async_declaration and hasattr(operations, "resolve_operation"):
                    status_operation_id = str(async_declaration.get("status_operation_id") or "")
                    job_id_path = str(async_declaration.get("job_id_path") or "job_id")
                    if status_operation_id:
                        status_contract = str(async_declaration.get("status_contract") or "lynewright.openapi")
                        status_operation_name = str(async_declaration.get("status_operation") or status_operation_id)
                        status_descriptor = _catalogue_operation(
                            operations, actor=actor, project_id=project_id,
                            contract=status_contract, operation=status_operation_name,
                            integration_id=str(declared.get("integration_id") or ""),
                            connection_id=str(declared.get("connection_id") or ""),
                        )
                        argument_path = str(async_declaration.get("status_argument_path") or "path.job_id")
                        stage["status_args_template"] = _async_status_template(stage_id, job_id_path, argument_path)
                        stage["status_ref_field"] = argument_path.split(".")[-1]
                    else:
                        status_contract = str(async_declaration.get("status_contract") or "")
                        status_operation_name = str(async_declaration.get("status_operation") or "")
                        status_descriptor = _catalogue_operation(
                            operations, actor=actor, project_id=project_id,
                            contract=status_contract, operation=status_operation_name,
                            integration_id=str(declared.get("integration_id") or ""),
                            connection_id=str(declared.get("connection_id") or ""),
                        )
                        argument = str(async_declaration.get("status_argument") or "")
                        stage["status_ref_field"] = argument or "ref"
                        stage["status_args_template"] = (
                            {argument: f"{{{{stages.{stage_id}.output.{job_id_path}}}}}"}
                            if argument else {}
                        )
                    stage["status_operation"] = {
                        "operation_id": status_descriptor["operation_id"],
                        "contract": status_descriptor["contract"],
                        "operation": status_descriptor["operation"],
                    }
                    stage["async_ref_path"] = job_id_path
                    status_operation = stage["status_operation"]
                if not isinstance(status_operation, dict):
                    raise PlatformValidationError(f"Stage {stage_id} needs a status_operation object or a mapped async declaration.")
                status_contract = str(status_operation.get("contract") or "").strip()
                status_name = str(status_operation.get("operation") or "").strip()
                status_id = str(status_operation.get("operation_id") or "").strip()
                if hasattr(operations, "resolve_operation"):
                    status_descriptor = _catalogue_operation(
                        operations, actor=actor, project_id=project_id,
                        operation_id=status_id, contract=status_contract, operation=status_name,
                    )
                    status_schema = status_descriptor.get("input_schema")
                    if not status_contract or not status_name:
                        raise PlatformValidationError(f"Stage {stage_id} status operation must name a contract and operation.")
                    status_operation["operation_id"] = str(status_descriptor["operation_id"])
                else:
                    status_key = (status_contract, status_name)
                    if status_key not in operations or not isinstance(stage.get("status_args_template", {}), (dict, str)):
                        raise PlatformValidationError(f"Stage {stage_id} status operation is not in the operation_directory.")
                    status_schema = operations[status_key].get("schema") if isinstance(operations, dict) else None
                if not stage.get("async_ref_path") and not stage.get("status_args_template"):
                    raise PlatformValidationError(f"Stage {stage_id} needs a reference or status arguments for polling.")
                if not isinstance(stage.get("status_args_template", {}), (dict, str)):
                    raise PlatformValidationError(f"Stage {stage_id} status arguments must be an object or context reference.")
                if not isinstance(operations, set):
                    _validate_arguments(stage_id, status_name, stage.get("status_args_template", {}), None, status_schema)
        if kind == "gate:choice":
            if not isinstance(stage.get("options_template"), (list, str)):
                raise PlatformValidationError(f"Stage {stage_id} needs an options_template list.")
            options_limit = stage.get("options_limit", 8)
            if isinstance(options_limit, bool) or not isinstance(options_limit, int) or not 1 <= options_limit <= 8:
                raise PlatformValidationError(f"Stage {stage_id} options_limit must be an integer from 1 to 8.")
        if kind == "gate:review" and not isinstance(stage.get("items_template"), (list, str)):
            raise PlatformValidationError(f"Stage {stage_id} needs an items_template list.")
        if kind in {"operation", "stepped_operation", "async_operation"}:
            timeout = stage.get("timeout_seconds", 30)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= 300:
                raise PlatformValidationError(f"Stage {stage_id} timeout_seconds must be between 1 and 300.")
        if kind == "condition":
            condition = stage.get("when")
            if not isinstance(condition, dict) or not isinstance(condition.get("path"), str):
                raise PlatformValidationError(f"Stage {stage_id} needs a simple condition path.")
            condition_path = str(condition["path"])
            if not (condition_path == "run.vars" or condition_path.startswith("run.vars.") or condition_path.startswith("stages.")):
                raise PlatformValidationError(f"Unsupported template reference: {condition_path}")
            if condition_path.startswith("stages."):
                referenced_stage = condition_path.split(".", 2)[1] if len(condition_path.split(".")) > 1 else ""
                if referenced_stage not in ids:
                    raise PlatformValidationError(f"Stage {stage_id} references a stage that must run earlier: {referenced_stage}.")
            if condition.get("action", "skip") not in {"skip", "stop"}:
                raise PlatformValidationError(f"Stage {stage_id} condition action must be skip or stop.")
    normalized = dict(definition)
    normalized["stages"] = stages
    _encode(normalized, "Line definition")
    return normalized


class ProductionLineStore:
    """Tenant-scoped SQLite persistence for definitions, runs, and events."""

    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = str(tenant_id or DEFAULT_TENANT_ID)
        with self._connection() as db:
            self.ensure_schema(db)

    @contextmanager
    def _connection(self):
        with _DB_LOCK:
            with self.store.connection() as db:
                yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS production_lines (
                tenant_id TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                definition_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (tenant_id, id)
            );
            CREATE INDEX IF NOT EXISTS production_lines_scope ON production_lines(tenant_id, project_id, enabled);
            CREATE TABLE IF NOT EXISTS line_runs (
                tenant_id TEXT NOT NULL, id TEXT NOT NULL, line_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL, current_stage TEXT NOT NULL DEFAULT '', vars_json TEXT NOT NULL DEFAULT '{}',
                actor_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                archived_at TEXT,
                PRIMARY KEY (tenant_id, id)
            );
            CREATE INDEX IF NOT EXISTS line_runs_scope ON line_runs(tenant_id, line_id, status, created_at);
            CREATE TABLE IF NOT EXISTS line_stage_runs (
                tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, stage_id TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                started_at REAL, ended_at REAL, duration_s REAL, output_ref TEXT NOT NULL DEFAULT '',
                output_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', runner_key TEXT NOT NULL DEFAULT '',
                runner_phase TEXT NOT NULL DEFAULT 'start', runner_payload_json TEXT NOT NULL DEFAULT '{}',
                progress_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (tenant_id, run_id, stage_id)
            );
            CREATE TABLE IF NOT EXISTS line_run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
                line_id TEXT NOT NULL DEFAULT '', event_type TEXT NOT NULL, stage_id TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}', actor_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS line_run_events_timeline ON line_run_events(tenant_id, run_id, id);
            """
        )
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(line_stage_runs)").fetchall()}
        if "runner_phase" not in columns:
            db.execute("ALTER TABLE line_stage_runs ADD COLUMN runner_phase TEXT NOT NULL DEFAULT 'start'")
        if "runner_payload_json" not in columns:
            db.execute("ALTER TABLE line_stage_runs ADD COLUMN runner_payload_json TEXT NOT NULL DEFAULT '{}'")
        if "progress_json" not in columns:
            db.execute("ALTER TABLE line_stage_runs ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}'")
        run_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(line_runs)").fetchall()}
        if "archived_at" not in run_columns:
            db.execute("ALTER TABLE line_runs ADD COLUMN archived_at TEXT")

    @staticmethod
    def _object(value: str | None, fallback: Any = None) -> Any:
        try:
            return json.loads(value or "null")
        except (TypeError, ValueError):
            return fallback

    def list_lines(self, *, project_id: str | None = None, enabled: bool | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM production_lines WHERE tenant_id=?", [self.tenant_id]
        if project_id is not None:
            sql += " AND project_id=?"
            args.append(str(project_id))
        if enabled is not None:
            sql += " AND enabled=?"
            args.append(int(enabled))
        sql += " ORDER BY name,id"
        with self._connection() as db:
            rows = db.execute(sql, args).fetchall()
        return [self._line(row) for row in rows]

    def get_line(self, line_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute("SELECT * FROM production_lines WHERE tenant_id=? AND id=?", (self.tenant_id, line_id)).fetchone()
        if row is None:
            raise PlatformNotFoundError("Production line was not found.")
        return self._line(row)

    def put_line(self, line_id: str, *, name: str, project_id: str, definition: dict[str, Any], enabled: bool, actor_id: str,
                 operation_directory: Any = None, operation_actor: Any = None,
                 create_only: bool = False) -> dict[str, Any]:
        line_id, name, project_id = str(line_id or "").strip(), str(name or "").strip(), str(project_id or "").strip()
        if not line_id or len(line_id) > 160 or not name or len(name) > 200 or len(project_id) > 160:
            raise PlatformValidationError("Line id, name, or project id is invalid.")
        if not isinstance(enabled, bool):
            raise PlatformValidationError("Line enabled must be a boolean.")
        definition = validate_definition(
            definition, operation_directory=operation_directory,
            actor=operation_actor, project_id=project_id,
        )
        encoded = _encode(definition, "Line definition")
        now = time.time()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version FROM production_lines WHERE tenant_id=? AND id=?", (self.tenant_id, line_id)).fetchone()
            if create_only and row is not None:
                db.rollback()
                raise PlatformConflictError("Production line id already exists.")
            version = int(row["version"]) + 1 if row else 1
            db.execute(
                """INSERT INTO production_lines(tenant_id,id,project_id,name,version,definition_json,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,id) DO UPDATE SET project_id=excluded.project_id,
                   name=excluded.name,version=excluded.version,definition_json=excluded.definition_json,
                   enabled=excluded.enabled,updated_at=excluded.updated_at""",
                (self.tenant_id, line_id, project_id, name, version, encoded, int(enabled), now, now),
            )
            self._event(db, line_id=line_id, event_type="line.created" if row is None else "line.updated", actor_id=actor_id,
                        detail={"version": version})
            db.commit()
        return self.get_line(line_id)

    def delete_line(self, line_id: str, *, actor_id: str) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM production_lines WHERE tenant_id=? AND id=?", (self.tenant_id, line_id)).fetchone()
            if row is None:
                db.rollback()
                raise PlatformNotFoundError("Production line was not found.")
            active = db.execute("SELECT 1 FROM line_runs WHERE tenant_id=? AND line_id=? AND status IN ('queued','running','waiting') LIMIT 1", (self.tenant_id, line_id)).fetchone()
            if active:
                db.rollback()
                raise PlatformConflictError("A line with active runs cannot be removed.")
            db.execute("DELETE FROM production_lines WHERE tenant_id=? AND id=?", (self.tenant_id, line_id))
            self._event(db, line_id=line_id, event_type="line.deleted", actor_id=actor_id, detail={})
            db.commit()

    @staticmethod
    def _line(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": str(row["id"]), "tenant_id": str(row["tenant_id"]), "project_id": str(row["project_id"]),
                "name": str(row["name"]), "version": int(row["version"]),
                "definition": ProductionLineStore._object(row["definition_json"], {}), "enabled": bool(row["enabled"]),
                "created_at": float(row["created_at"]), "updated_at": float(row["updated_at"])}

    def _event(self, db: sqlite3.Connection, *, event_type: str, actor_id: str = "", detail: Any,
               run_id: str = "", line_id: str = "", stage_id: str = "") -> None:
        db.execute("INSERT INTO line_run_events(tenant_id,run_id,line_id,event_type,stage_id,detail_json,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (self.tenant_id, run_id, line_id, event_type, stage_id, _encode(detail, "Event detail"), actor_id, time.time()))


class ProductionLineEngine:
    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store or PlatformStore()
        self.db = ProductionLineStore(self.store, tenant_id=tenant_id)
        self.tenant_id = self.db.tenant_id

    def seed_directory(self, directory: str | Path) -> int:
        added = 0
        for path in Path(directory).glob("*/data/lines/*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                self.db.get_line(str(value["id"]))
            except PlatformNotFoundError:
                operation_directory = _operation_directory()
                try:
                    definition = validate_definition(value["definition"], operation_directory=operation_directory)
                except PlatformValidationError as exc:
                    if "not in the operation_directory" not in str(exc):
                        raise
                    trusted_keys = {
                        (str(stage.get("contract") or ""), str(stage.get("operation") or ""))
                        for stage in value["definition"].get("stages", []) if isinstance(stage, dict)
                        and stage.get("contract") and stage.get("operation")
                    }
                    operation_directory = trusted_keys
                    definition = validate_definition(value["definition"], operation_directory=operation_directory)
                self.db.put_line(str(value["id"]), name=str(value["name"]), project_id="",
                                 definition=definition, enabled=bool(value.get("enabled", True)), actor_id="system", operation_directory=operation_directory)
                added += 1
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return added

    def start(self, line_id: str, *, variables: dict[str, Any], project_id: str = "", actor: Any = None) -> dict[str, Any]:
        line = self.db.get_line(line_id)
        if not line["enabled"]:
            raise PlatformConflictError("Production line is disabled.")
        if line["project_id"] and line["project_id"] != project_id:
            raise PlatformNotFoundError("Production line was not found.")
        if not isinstance(variables, dict):
            raise PlatformValidationError("Run variables must be an object.")
        _encode(variables, "Run variables")
        run_id = uuid.uuid4().hex
        now = time.time()
        actor_data = self._actor(actor)
        with self.db._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO line_runs(tenant_id,id,line_id,project_id,status,current_stage,vars_json,actor_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (self.tenant_id, run_id, line_id, project_id, "queued", "", _encode({"vars": variables, "stages": {}}, "Run context"), _encode(actor_data, "Actor"), now, now))
            self.db._event(db, run_id=run_id, line_id=line_id, event_type="run.started", actor_id=actor_data.get("user_id", ""), detail={"project_id": project_id})
            db.commit()
        self._advance(run_id, actor=actor)
        return self.get_run(run_id)

    @staticmethod
    def _actor(actor: Any) -> dict[str, Any]:
        if isinstance(actor, dict):
            return {key: str(value) for key, value in actor.items() if key in {"user_id", "name", "client_id"}}
        if actor is None:
            return {}
        return {"user_id": str(getattr(actor, "client_id", "")), "name": str(getattr(actor, "name", ""))}

    def _run_row(self, run_id: str) -> sqlite3.Row:
        with self.db._connection() as db:
            row = db.execute("SELECT * FROM line_runs WHERE tenant_id=? AND id=?", (self.tenant_id, run_id)).fetchone()
        if row is None:
            raise PlatformNotFoundError("Production line run was not found.")
        return row

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self._run_row(run_id)
        with self.db._connection() as db:
            stages = db.execute("SELECT * FROM line_stage_runs WHERE tenant_id=? AND run_id=? ORDER BY started_at,stage_id", (self.tenant_id, run_id)).fetchall()
            events = db.execute("SELECT * FROM line_run_events WHERE tenant_id=? AND run_id=? ORDER BY id", (self.tenant_id, run_id)).fetchall()
        return {"id": str(row["id"]), "line_id": str(row["line_id"]), "project_id": str(row["project_id"]),
                "status": str(row["status"]), "current_stage": str(row["current_stage"]),
                "context": self.db._object(row["vars_json"], {}), "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]), "archived_at": str(row["archived_at"] or ""),
                "stages": [{"stage_id": str(s["stage_id"]), "status": str(s["status"]), "attempts": int(s["attempts"]),
                            "started_at": s["started_at"], "ended_at": s["ended_at"], "duration_s": s["duration_s"],
                            "output_ref": str(s["output_ref"]), "output": self.db._object(s["output_json"], {}),
                            "progress": self.db._object(s["progress_json"], {}), "error": str(s["error"])} for s in stages],
                "timeline": [{"id": int(e["id"]), "type": str(e["event_type"]), "stage_id": str(e["stage_id"]),
                              "detail": self.db._object(e["detail_json"], {}), "actor_id": str(e["actor_id"]),
                              "created_at": float(e["created_at"])} for e in events]}

    def list_runs(self, *, line_id: str | None = None, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, args = "SELECT id FROM line_runs WHERE tenant_id=? AND archived_at IS NULL", [self.tenant_id]
        if line_id:
            sql += " AND line_id=?"; args.append(line_id)
        if project_id:
            sql += " AND project_id=?"; args.append(project_id)
        sql += " ORDER BY created_at DESC LIMIT ?"; args.append(max(1, min(int(limit), 500)))
        with self.db._connection() as db:
            ids = [str(row["id"]) for row in db.execute(sql, args).fetchall()]
        return [self.get_run(run_id) for run_id in ids]

    def archive_run(self, run_id: str, *, actor_id: str) -> None:
        terminal = {"done", "failed", "rejected", "cancelled", "canceled"}
        with self.db._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,archived_at FROM line_runs WHERE tenant_id=? AND id=?",
                (self.tenant_id, run_id),
            ).fetchone()
            if row is None:
                db.rollback()
                raise PlatformNotFoundError("Production run was not found.")
            if row["status"] not in terminal:
                db.rollback()
                raise PlatformConflictError("Only completed, failed, rejected, or cancelled runs can be archived.")
            if row["archived_at"]:
                db.rollback()
                raise PlatformConflictError("This run is already archived.")
            archived_at = datetime.now(UTC).isoformat()
            db.execute(
                "UPDATE line_runs SET archived_at=?,updated_at=? WHERE tenant_id=? AND id=?",
                (archived_at, time.time(), self.tenant_id, run_id),
            )
            line_id = str(db.execute(
                "SELECT line_id FROM line_runs WHERE tenant_id=? AND id=?", (self.tenant_id, run_id),
            ).fetchone()[0])
            self.db._event(db, run_id=run_id, line_id=line_id, event_type="run.archived", actor_id=actor_id, detail={"archived_at": archived_at})
            db.commit()

    def _set_run(self, run_id: str, *, status: str | None = None, current_stage: str | None = None, context: dict[str, Any] | None = None) -> None:
        updates, args = [], []
        for column, value in (("status", status), ("current_stage", current_stage), ("vars_json", _encode(context, "Run context") if context is not None else None)):
            if value is not None:
                updates.append(f"{column}=?"); args.append(value)
        updates.append("updated_at=?"); args.append(time.time()); args.extend([self.tenant_id, run_id])
        with self.db._connection() as db, db:
            db.execute(f"UPDATE line_runs SET {','.join(updates)} WHERE tenant_id=? AND id=?", args)
        if status in {"done", "failed", "rejected", "cancelled", "canceled"}:
            from app.services.media_links import MediaLinksService

            MediaLinksService(self.store, tenant_id=self.tenant_id).revoke_run(
                run_id, reason=f"line_run_{status}",
            )

    def _stage(self, run_id: str, stage_id: str) -> dict[str, Any]:
        line = self.db.get_line(self._run_row(run_id)["line_id"])
        return next((stage for stage in line["definition"]["stages"] if stage["id"] == stage_id), {})

    def _advance(self, run_id: str, *, actor: Any = None) -> None:
        row = self._run_row(run_id)
        if row["status"] in {"cancelled", "failed", "done"}:
            return
        line = self.db.get_line(str(row["line_id"]))
        context = self.db._object(row["vars_json"], {"vars": {}, "stages": {}})
        stages = line["definition"]["stages"]
        completed = {item["stage_id"] for item in self.get_run(run_id)["stages"] if item["status"] in {"done", "skipped"}}
        next_stage = next((stage for stage in stages if stage["id"] not in completed), None)
        if next_stage is None:
            self._set_run(run_id, status="done", current_stage="")
            self._event(run_id, "run.done", detail={})
            return
        stage_id, kind = str(next_stage["id"]), str(next_stage["kind"])
        self._set_run(run_id, status="running", current_stage=stage_id)
        self._ensure_stage(run_id, stage_id)
        if kind in {"operation", "stepped_operation", "async_operation"}:
            try:
                self._schedule_stage(run_id, next_stage, actor=actor)
            except Exception as exc:
                self._fail_stage(run_id, stage_id, exc)
        elif kind in {"gate:choice", "gate:review"}:
            try:
                key = "options_template" if kind == "gate:choice" else "items_template"
                items = _resolve(next_stage[key], context)
                if not isinstance(items, list):
                    raise PlatformValidationError("Gate items must resolve to a list.")
                if kind == "gate:review":
                    from app.services.review_media import attach_review_media

                    items = attach_review_media(
                        items, tenant_id=self.tenant_id, project_id=str(row["project_id"] or ""),
                        actor=actor, run_id=run_id, store=self.store,
                    )
                self._stage_update(run_id, stage_id, status="waiting", output={"items": items})
                self._set_run(run_id, status="waiting")
                self._event(run_id, "stage.waiting", stage_id=stage_id, detail={"kind": kind, "items": items})
                if kind == "gate:choice" and (not items or row["project_id"]):
                    self._send_choice(run_id, stage_id, items)
            except Exception as exc:
                self._fail_stage(run_id, stage_id, exc)
        elif kind == "condition":
            condition = next_stage["when"]
            try:
                actual = _path_value(context, str(condition["path"]))
                matched = actual == condition.get("equals") if "equals" in condition else bool(actual) == bool(condition.get("truthy", True))
            except PlatformValidationError:
                matched = False
            action = condition.get("action", "skip")
            if matched:
                self._stage_update(run_id, stage_id, status="done", output={"matched": True})
            elif action == "stop":
                self._stage_update(run_id, stage_id, status="done", output={"matched": False, "stopped": True})
                self._set_run(run_id, status="done")
                self._event(run_id, "run.stopped", stage_id=stage_id, detail={})
                return
            else:
                self._stage_update(run_id, stage_id, status="skipped", output={"matched": False})
            self._advance(run_id, actor=actor)
        elif kind == "notify":
            try:
                message = str(_resolve(next_stage.get("message_template", ""), context))
            except Exception as exc:
                self._fail_stage(run_id, stage_id, exc)
                return
            self._event(run_id, "notification", stage_id=stage_id, detail={"message": message})
            project_id = str(row["project_id"] or "")
            from app.services.application_logging import log_event
            log_event(
                level="info", category="line", source="lynewright.production_line",
                message=message,
                details={"line_id": str(row["line_id"]), "run_id": run_id,
                         "stage_id": stage_id},
                project_id=project_id or None, tenant_id=self.tenant_id,
            )
            if project_id:
                try:
                    from app.services.notification_gateway import send_notification
                    asyncio.run(send_notification(tenant_id=self.tenant_id, project_id=project_id,
                        event_class="operator_update", title=str(next_stage.get("title") or line["name"]),
                        body=message, intent="IMMEDIATE", dedupe_key=f"line:{run_id}:{stage_id}", actor="system"))
                except Exception:
                    pass
            self._stage_update(run_id, stage_id, status="done", output={"message": message})
            self._advance(run_id, actor=actor)

    def _ensure_stage(self, run_id: str, stage_id: str) -> None:
        with self.db._connection() as db, db:
            db.execute("INSERT OR IGNORE INTO line_stage_runs(tenant_id,run_id,stage_id,status,attempts) VALUES(?,?,?,'queued',0)", (self.tenant_id, run_id, stage_id))

    def _stage_update(self, run_id: str, stage_id: str, *, status: str, output: Any = None, error: str = "",
                      runner_key: str | None = None, runner_phase: str | None = None,
                      runner_payload: dict[str, Any] | None = None, increment: bool = False) -> None:
        now = time.time()
        with self.db._connection() as db, db:
            old = db.execute("SELECT started_at,attempts,progress_json FROM line_stage_runs WHERE tenant_id=? AND run_id=? AND stage_id=?", (self.tenant_id, run_id, stage_id)).fetchone()
            started = old["started_at"] or now
            attempts = int(old["attempts"] or 0) + int(increment)
            ended = now if status in {"done", "failed", "skipped", "cancelled"} else None
            duration = max(0, now - float(started)) if ended is not None else None
            output_ref = str(output.get("output_ref") or output.get("ref") or "") if isinstance(output, dict) else ""
            payload_json = _encode(runner_payload, "Stage operation arguments") if runner_payload is not None else None
            progress = output.get("progress") if isinstance(output, dict) else None
            progress_json = _encode(progress, "Stage progress") if isinstance(progress, dict) else old["progress_json"]
            db.execute("UPDATE line_stage_runs SET status=?,attempts=?,started_at=?,ended_at=?,duration_s=?,output_json=?,progress_json=?,error=?,runner_key=COALESCE(?,runner_key),runner_phase=COALESCE(?,runner_phase),runner_payload_json=COALESCE(?,runner_payload_json),output_ref=COALESCE(NULLIF(?,''),output_ref) WHERE tenant_id=? AND run_id=? AND stage_id=?",
                       (status, attempts, started, ended, duration, _encode(output or {}, "Stage output"), progress_json, error[:1000], runner_key, runner_phase, payload_json, output_ref, self.tenant_id, run_id, stage_id))

    def _stage_runner(self, run_id: str, stage_id: str) -> dict[str, Any] | None:
        with self.db._connection() as db:
            row = db.execute(
                "SELECT runner_key,runner_phase,runner_payload_json FROM line_stage_runs WHERE tenant_id=? AND run_id=? AND stage_id=?",
                (self.tenant_id, run_id, stage_id),
            ).fetchone()
        return dict(row) if row else None

    def _fail_stage(self, run_id: str, stage_id: str, error: BaseException) -> None:
        message = str(error)[:1000] or type(error).__name__
        self._stage_update(run_id, stage_id, status="failed", error=message)
        self._set_run(run_id, status="failed", current_stage=stage_id)
        self._event(run_id, "stage.failed", stage_id=stage_id, detail={"error": message})

    def _event(self, run_id: str, event_type: str, *, stage_id: str = "", detail: Any, actor_id: str = "") -> None:
        row = self._run_row(run_id)
        with self.db._connection() as db, db:
            self.db._event(db, run_id=run_id, line_id=str(row["line_id"]), event_type=event_type,
                           stage_id=stage_id, detail=detail, actor_id=actor_id)

    def _schedule_stage(self, run_id: str, stage: dict[str, Any], *, actor: Any = None, phase: str = "start", payload: dict[str, Any] | None = None, resume: bool = False, retry: bool = False) -> dict[str, Any]:
        from app.services import line_runner
        row = self._run_row(run_id)
        context = self.db._object(row["vars_json"], {"vars": {}, "stages": {}})
        kind = str(stage["kind"])
        if phase.startswith("poll"):
            op_spec = stage["status_operation"]
            contract, operation = str(op_spec["contract"]), str(op_spec["operation"])
            operation_id = str(op_spec.get("operation_id") or "")
            args = payload if payload is not None else _resolve(stage.get("status_args_template", {}), context)
        else:
            contract, operation = str(stage["contract"]), str(stage["operation"])
            operation_id = str(stage.get("operation_id") or "")
            args = payload if payload is not None else _resolve(stage.get("args_template", {}), context)
        if isinstance(args, dict) and isinstance(stage.get("args_defaults"), dict) and phase == "start":
            args = {**dict(stage["args_defaults"]), **args}
        if isinstance(args, str) and phase == "start":
            args = _resolve(args, context)
        if not isinstance(args, dict):
            raise PlatformValidationError("Resolved operation arguments must be an object.")
        operation_actor = self._operation_actor(
            contract, operation, run_id, project_id=str(row["project_id"] or ""),
            operation_id=operation_id,
        )
        if not operation_id:
            from app.services.integration_operations import build_tenant_operation_catalog
            from app.services.platform_capabilities import CapabilityActor

            authority = CapabilityActor(
                client_id="line-operation-resolver", name="Production line operation resolver",
                client_type="automation", permission_mode="full", granted_capabilities=frozenset(),
            )
            resolved = build_tenant_operation_catalog(self.tenant_id).resolve_operation(
                authority, project_id=str(row["project_id"] or ""), contract=contract, operation=operation,
            )
            operation_id = str(resolved["operation_id"])
        else:
            from app.services.integration_operations import build_tenant_operation_catalog
            from app.services.platform_capabilities import CapabilityActor

            authority = CapabilityActor(
                client_id="line-operation-resolver", name="Production line operation resolver",
                client_type="automation", permission_mode="full", granted_capabilities=frozenset(),
            )
            resolved = build_tenant_operation_catalog(self.tenant_id).resolve_operation(
                authority, project_id=str(row["project_id"] or ""), operation_id=operation_id,
                contract=contract, operation=operation,
            )
        descriptor = resolved["descriptor"]
        if phase == "start":
            connection = resolved["entry"].get("connection") or {}
            source_transport = str(resolved["entry"].get("transport") or "")
            internal = bool(
                str(connection.get("origin_kind") or "") == "internal_endpoint"
                or source_transport in {"service", "native_runtime"}
            )
            args = _wire_media_arguments(
                args, stage.get("args_template", {}), descriptor.get("input_schema"),
                tenant_id=self.tenant_id, project_id=str(row["project_id"] or ""),
                run_id=run_id, stage_id=str(stage["id"]), actor=operation_actor,
                internal=internal, timeout_seconds=float(stage.get("timeout_seconds", 30)),
            )
        if retry and phase == "start" and self._operation_accepts_retry(contract, operation):
            # A stepped operation may keep its own run state; once failed it
            # stays failed unless told this is a retry (found live 2026-10-07:
            # Retry replayed the operation's stored failure).
            args = {**args, "retry": True}
        run_key = line_runner.run_key_for(contract, operation, args, scope=f"line:{run_id}:{stage['id']}:{phase}")
        # The line owns operation grants; an external chooser only supplies a
        # decision and must not widen the automation actor's capabilities.
        self._stage_update(run_id, str(stage["id"]), status="running", runner_key=run_key,
                           runner_phase=phase, runner_payload=args, increment=phase == "start")
        self._set_run(run_id, status="running", current_stage=str(stage["id"]))
        line_runner.schedule_step(contract, operation, args, run_key=run_key, project_id=str(row["project_id"] or ""),
            actor=operation_actor, tenant_id=self.tenant_id, store=self.store, resume=resume, retry=retry,
            metadata={"line_run_id": run_id, "stage_id": str(stage["id"]), "phase": phase,
                      "operation_catalog_id": operation_id,
                      "stage_timeout_seconds": float(stage.get("timeout_seconds", 30)),
                      "stop_after_one": kind == "operation" or (kind == "async_operation" and phase == "start")})
        self._event(run_id, "stage.queued", stage_id=str(stage["id"]), detail={"operation": f"{contract}.{operation}", "phase": phase})
        return {"run_key": run_key, "status": "queued"}

    @staticmethod
    def _operation_accepts_retry(contract: str, operation: str) -> bool:
        try:
            from app.services.operator_dispatch import discover_contract_operations
            from app.services.integration_host import contributions
            declared = next((item for c, o, item in discover_contract_operations(contributions) if c == contract and o == operation), {})
        except Exception:
            return False
        properties = declared.get("properties") if isinstance(declared, dict) else None
        return isinstance(properties, dict) and "retry" in properties

    def _operation_actor(
        self, contract: str, operation: str, run_id: str, *,
        project_id: str = "", operation_id: str = "",
    ) -> Any:
        from app.services.integration_operations import build_tenant_operation_catalog
        from app.services.platform_capabilities import CapabilityActor

        catalogue = build_tenant_operation_catalog(self.tenant_id)
        authority = CapabilityActor(
            client_id="line-operation-resolver", name="Production line operation resolver",
            client_type="automation", permission_mode="full", granted_capabilities=frozenset(),
        )
        resolved = catalogue.resolve_operation(
            authority, project_id=project_id, operation_id=operation_id,
            contract=contract, operation=operation,
        )
        descriptor = resolved["descriptor"]
        grants = {str(descriptor.get("capability") or "operation:invoke")}
        try:
            integration = catalogue.management.integrations.get(str(descriptor.get("integration_id") or ""))
            grants.update(str(value) for value in integration.get("requested_capabilities") or [])
        except Exception:
            pass
        return CapabilityActor(
            client_id=f"line:{run_id}", name="Production line", client_type="automation",
            permission_mode="restricted", granted_capabilities=frozenset(grants),
        )

    def retry_stage(self, run_id: str, stage_id: str, *, actor: Any = None, actor_id: str = "") -> dict[str, Any]:
        run = self.get_run(run_id)
        item = next((entry for entry in run["stages"] if entry["stage_id"] == stage_id), None)
        if item is None or item["status"] != "failed":
            raise PlatformConflictError("Only a failed stage can be retried.")
        stage = self._stage(run_id, stage_id)
        self._stage_update(run_id, stage_id, status="queued", error="")
        self._set_run(run_id, status="running", current_stage=stage_id)
        self._event(run_id, "stage.retry_requested", stage_id=stage_id, detail={}, actor_id=actor_id)
        self._schedule_stage(run_id, stage, actor=actor, retry=True)
        return self.get_run(run_id)

    def continue_run(self, run_id: str, *, value: dict[str, Any] | None = None, actor: Any = None, actor_id: str = "") -> dict[str, Any]:
        if value is not None and not isinstance(value, dict):
            raise PlatformValidationError("Continuation value must be an object.")
        run = self.get_run(run_id)
        if run["status"] != "waiting" or not run["current_stage"]:
            raise PlatformConflictError("Run is not waiting for input or was already chosen.")
        stage_id = run["current_stage"]
        stage = self._stage(run_id, stage_id)
        entry = next(item for item in run["stages"] if item["stage_id"] == stage_id)
        if stage["kind"] in {"gate:choice", "gate:review"}:
            output = dict(value or {})
            selected = output.get("choice")
            if isinstance(selected, dict):
                output = {**selected, **output}
            with self.db._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                changed_run = db.execute(
                    "UPDATE line_runs SET status='running',updated_at=? WHERE tenant_id=? AND id=? AND status='waiting' AND current_stage=?",
                    (time.time(), self.tenant_id, run_id, stage_id),
                ).rowcount
                changed_stage = db.execute(
                    "UPDATE line_stage_runs SET status='done',ended_at=?,output_json=? WHERE tenant_id=? AND run_id=? AND stage_id=? AND status='waiting'",
                    (time.time(), _encode(output, "Stage output"), self.tenant_id, run_id, stage_id),
                ).rowcount if changed_run else 0
                if not changed_run or not changed_stage:
                    db.rollback()
                    raise PlatformConflictError("Run is not waiting for input or was already chosen.")
                db.commit()
            self._merge_stage_output(run_id, stage_id, output)
            self._event(run_id, "stage.continued", stage_id=stage_id, detail=output, actor_id=actor_id)
            review_items = output.get("items")
            rejected_items = isinstance(review_items, list) and any(
                isinstance(item, dict)
                and str(item.get("decision") or "").strip().lower() in {"refused", "rejected"}
                for item in review_items
            )
            if stage["kind"] == "gate:review" and (
                output.get("approved") is False or rejected_items
            ):
                self._set_run(run_id, status="rejected", current_stage=stage_id)
                self._event(run_id, "run.rejected", stage_id=stage_id,
                            detail={"reason": "review gate refused"}, actor_id=actor_id)
                return self.get_run(run_id)
            self._advance(run_id, actor=actor)
        elif (saved := self._stage_runner(run_id, stage_id)) and saved.get("runner_key"):
            self._event(run_id, "stage.continued", stage_id=stage_id, detail={}, actor_id=actor_id)
            phase = str(saved.get("runner_phase") or "start")
            payload = self.db._object(saved.get("runner_payload_json"), {})
            self._schedule_stage(run_id, stage, actor=actor, phase=phase, payload=payload, resume=True)
        else:
            raise PlatformConflictError("Waiting stage cannot be continued.")
        return self.get_run(run_id)

    def _send_choice(self, run_id: str, stage_id: str, items: list[Any]) -> None:
        row = self._run_row(run_id)
        if not items:
            self._event(run_id, "choice.telegram_skipped", stage_id=stage_id,
                        detail={"reason": "no_options", "count": 0})
            return
        stage = self._stage(run_id, stage_id)
        options_limit = int(stage.get("options_limit", 8))
        from app.services import choice_channel

        line = self.db.get_line(str(row["line_id"]))
        options = []
        for index, raw in enumerate(items[:options_limit]):
            item = raw if isinstance(raw, dict) else {"value": raw}
            label = item.get("label") or item.get("title") or item.get("name") or f"Option {index + 1}"
            options.append({"key": item, "label": str(label)})
        result = choice_channel.request_choice(
            tenant_id=self.tenant_id, project_id=str(row["project_id"]),
            set_key=f"production-line:{run_id}:{stage_id}", title=line["name"],
            text=f"Choose an option for {line['name']}.", options=options,
            submit={"contract": "lynewright.production.lines", "operation": "continue",
                    "payload": {"line_run_id": run_id}}, allow_none=False,
        )
        self._event(run_id, "choice.telegram_requested", stage_id=stage_id,
                    detail={"session_id": result.get("session_id"), "status": result.get("status")})

    def cancel(self, run_id: str, *, actor_id: str = "") -> dict[str, Any]:
        row = self._run_row(run_id)
        if row["status"] in {"done", "failed", "cancelled"}:
            raise PlatformConflictError("Run is already terminal.")
        if row["current_stage"]:
            active = self._stage_runner(run_id, str(row["current_stage"]))
            if active and active.get("runner_key"):
                from app.services.line_runner import cancel_run

                cancel_run(str(active["runner_key"]), tenant_id=self.tenant_id, store=self.store)
        self._set_run(run_id, status="cancelled")
        if row["current_stage"]:
            self._stage_update(run_id, str(row["current_stage"]), status="cancelled")
        self._event(run_id, "run.cancelled", detail={}, actor_id=actor_id)
        return self.get_run(run_id)

    def skip_stage(self, run_id: str, stage_id: str, *, reason: str, actor: Any = None, actor_id: str = "") -> dict[str, Any]:
        if not reason.strip():
            raise PlatformValidationError("A reason is required to skip a stage.")
        run = self.get_run(run_id)
        if run["status"] not in {"running", "waiting"} or run["current_stage"] != stage_id:
            raise PlatformConflictError("Only the current stage can be skipped.")
        active = self._stage_runner(run_id, stage_id)
        if active and active.get("runner_key"):
            from app.services.line_runner import cancel_run

            cancel_run(str(active["runner_key"]), tenant_id=self.tenant_id, store=self.store)
        self._stage_update(run_id, stage_id, status="skipped", output={"reason": reason.strip()})
        self._event(run_id, "stage.skipped", stage_id=stage_id, detail={"reason": reason.strip()}, actor_id=actor_id)
        self._advance(run_id, actor=actor)
        return self.get_run(run_id)

    def _merge_stage_output(self, run_id: str, stage_id: str, output: Any) -> None:
        row = self._run_row(run_id)
        context = self.db._object(row["vars_json"], {"vars": {}, "stages": {}})
        context.setdefault("stages", {})[stage_id] = {"output": output}
        self._set_run(run_id, context=context)


def handle_runner_update(run: dict[str, Any]) -> None:
    """Apply a persisted queue update to its owning line stage."""
    metadata = run.get("metadata") or {}
    run_id, stage_id = str(metadata.get("line_run_id") or ""), str(metadata.get("stage_id") or "")
    if not run_id or not stage_id:
        return
    engine = ProductionLineEngine(tenant_id=str(run.get("tenant_id") or DEFAULT_TENANT_ID))
    try:
        current = engine.get_run(run_id)
    except PlatformNotFoundError:
        return
    if current["status"] == "cancelled":
        return
    status = str(run.get("status") or "")
    stage = engine._stage(run_id, stage_id)
    stage_run = next((item for item in current["stages"] if item["stage_id"] == stage_id), None)
    if stage_run is None or stage_run["status"] in {"done", "failed", "skipped", "cancelled"}:
        return
    if current["current_stage"] != stage_id:
        return
    phase = str(metadata.get("phase") or "start")
    result = _compact_result(run.get("result")) if isinstance(run.get("result"), dict) else {}
    if status == "queued":
        engine._stage_update(run_id, stage_id, status="running", output=result, runner_key=str(run.get("run_key") or ""))
        engine._merge_stage_output(run_id, stage_id, result)
        engine._event(run_id, "stage.progress", stage_id=stage_id, detail={"result": result})
        return
    if status == "waiting":
        engine._stage_update(run_id, stage_id, status="waiting", output=result, runner_key=str(run.get("run_key") or ""))
        engine._set_run(run_id, status="waiting", current_stage=stage_id)
        engine._event(run_id, "stage.waiting", stage_id=stage_id, detail=result)
        return
    if status == "failed":
        engine._stage_update(run_id, stage_id, status="failed", error=str(run.get("error") or "Operation failed."), output=result)
        engine._set_run(run_id, status="failed", current_stage=stage_id)
        engine._event(run_id, "stage.failed", stage_id=stage_id, detail={"error": str(run.get("error") or "Operation failed.")})
        return
    if status != "succeeded":
        return
    if str(stage.get("kind")) == "async_operation" and phase == "start":
        engine._merge_stage_output(run_id, stage_id, result)
        current = engine.get_run(run_id)
        status_path = str(stage.get("async_ref_path") or "")
        reference = result if status_path else None
        for part in status_path.split(".") if status_path else []:
            reference = reference.get(part) if isinstance(reference, dict) else None
        poll_args = _resolve(stage.get("status_args_template", {}), current["context"])
        if reference is not None:
            poll_args = dict(poll_args) if isinstance(poll_args, dict) else {}
            if not poll_args:
                ref_field = str(stage.get("status_ref_field") or "ref")
                poll_args[ref_field] = reference
        if reference is not None or poll_args:
            if isinstance(poll_args, dict):
                current["context"].setdefault("_poll_payloads", {})[stage_id] = poll_args
                engine._set_run(run_id, context=current["context"])
                try:
                    engine._schedule_stage(run_id, stage, phase="poll", payload=poll_args)
                except Exception as exc:
                    engine._fail_stage(run_id, stage_id, exc)
                return
    if str(stage.get("kind")) == "async_operation" and phase.startswith("poll"):
        result_status = str(result.get("status") or "").lower()
        if result_status in {"queued", "pending", "accepted", "processing", "running"}:
            context = engine.get_run(run_id)["context"]
            count = int(context.setdefault("_poll_counts", {}).get(stage_id, 0)) + 1
            context["_poll_counts"][stage_id] = count
            engine._set_run(run_id, context=context)
            poll_payload = context.get("_poll_payloads", {}).get(stage_id, {})
            engine._schedule_stage(run_id, stage, phase=f"poll-{count}", payload=poll_payload)
            return
    engine._stage_update(run_id, stage_id, status="done", output=result)
    engine._merge_stage_output(run_id, stage_id, result)
    engine._event(run_id, "stage.done", stage_id=stage_id, detail={"result": result})
    engine._advance(run_id)


__all__ = ["ProductionLineEngine", "ProductionLineStore", "handle_runner_update", "validate_definition"]
