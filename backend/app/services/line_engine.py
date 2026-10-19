"""Provider-neutral execution for data-defined production lines."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
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
_STAGE_KINDS = {"operation", "stepped_operation", "async_operation", "for_each", "map", "gate:choice", "gate:review", "notify", "condition"}
_MAX_FOR_EACH_ITEMS = 500
_MAX_FOR_EACH_CONCURRENCY = 10
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
    is_item_input = path == "item" or path.startswith("item.") or path == "item_index"
    if not (path == "run.vars" or path.startswith("run.vars.") or path.startswith("stages.") or is_action_input or is_item_input):
        raise PlatformValidationError(f"Unsupported template reference: {path}")
    value: Any = {
        "run": {"vars": context.get("vars", {})}, "stages": context.get("stages", {}),
        "action": context.get("action", {}), "item": context.get("item", {}),
        "item_index": context.get("item_index"),
    }
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


def _optional_path_value(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def _map_sort_key(value: Any) -> tuple[int, Any]:
    if value is None:
        return (4, "")
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    if isinstance(value, str):
        return (2, value)
    return (3, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _map_item_path(item: Any, path: str, item_index: int) -> Any:
    path = path.removesuffix("?")
    if path == "item":
        return item
    if path == "item_index":
        return item_index
    if not path.startswith("item."):
        raise PlatformValidationError(f"Map field reference {path} must use item or item_index.")
    _found, value = _optional_path_value(item, path[5:])
    return value


def _apply_map(stage: dict[str, Any], items: Any) -> dict[str, Any]:
    if not isinstance(items, list):
        raise PlatformValidationError("map items must resolve to a list.")
    fields = stage.get("fields")
    if not isinstance(fields, dict):
        raise PlatformValidationError("map fields must be an object.")
    mapped: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        output: dict[str, Any] = {}
        for key, spec in fields.items():
            if isinstance(spec, str) and (match := _TEMPLATE.fullmatch(spec)):
                output[str(key)] = _map_item_path(item, match.group(1), index)
            else:
                output[str(key)] = spec
        mapped.append(output)

    condition = stage.get("where")
    if isinstance(condition, dict):
        path = str(condition["path"])
        op = str(condition["op"])
        expected = condition.get("value")
        filtered: list[dict[str, Any]] = []
        for item in mapped:
            exists, actual = _optional_path_value(item, path)
            matched = False
            if op == "exists":
                matched = exists
            elif op == "=":
                actual = actual if exists else None
                matched = type(actual) is type(expected) and actual == expected
            elif op == "!=":
                actual = actual if exists else None
                matched = type(actual) is not type(expected) or actual != expected
            elif exists and isinstance(actual, (int, float, str)) and not isinstance(actual, bool):
                try:
                    if op == ">":
                        matched = actual > expected
                    elif op == ">=":
                        matched = actual >= expected
                    elif op == "<":
                        matched = actual < expected
                    elif op == "<=":
                        matched = actual <= expected
                except TypeError:
                    matched = False
            if matched:
                filtered.append(item)
        mapped = filtered

    sort_spec = stage.get("sort")
    if isinstance(sort_spec, dict):
        path = str(sort_spec["path"])
        reverse = sort_spec["order"] == "desc"
        found_items: list[tuple[Any, dict[str, Any]]] = []
        missing_items: list[dict[str, Any]] = []
        for item in mapped:
            exists, value = _optional_path_value(item, path)
            if exists and value is not None:
                found_items.append((_map_sort_key(value), item))
            else:
                missing_items.append(item)
        found_items.sort(key=lambda pair: pair[0], reverse=reverse)
        mapped = [item for _key, item in found_items] + missing_items

    if stage.get("limit") is not None:
        mapped = mapped[:int(stage["limit"])]
    return {"items": mapped}


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
            or source_path == "item" or source_path.startswith("item.")
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
    _allow_item_templates: bool = False,
) -> dict[str, Any]:
    if not isinstance(definition, dict):
        raise PlatformValidationError("A line definition must be an object.")
    unknown_definition_fields = set(definition) - {"stages", "review_by"}
    if unknown_definition_fields:
        fields = ", ".join(sorted(str(field) for field in unknown_definition_fields))
        raise PlatformValidationError(f"A line definition contains unsupported fields: {fields}.")
    review_by = definition.get("review_by", "dashboard")
    if review_by not in {"dashboard", "dashboard_or_assistant"}:
        raise PlatformValidationError("review_by must be dashboard or dashboard_or_assistant.")
    stages = definition.get("stages")
    if not isinstance(stages, list) or not stages or len(stages) > 100:
        raise PlatformValidationError("A line definition needs between 1 and 100 stages.")
    ids: set[str] = set()
    stage_kinds: dict[str, str] = {}
    operations = operation_directory if operation_directory is not None else _operation_directory()
    for stage in stages:
        if not isinstance(stage, dict):
            raise PlatformValidationError("Each stage must be an object.")
        stage_id = str(stage.get("id") or "").strip()
        kind = str(stage.get("kind") or "").strip()
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", stage_id) or stage_id in ids:
            raise PlatformValidationError("Stage ids must be unique, non-empty identifiers.")
        if kind not in _STAGE_KINDS:
            raise PlatformValidationError(f"Unsupported stage kind: {kind}")
        common_fields = {"id", "kind", "label", "title", "description"}
        stage_fields = {
            "for_each": common_fields | {
                "items", "operation", "operation_name", "operation_id", "contract", "input",
                "args_template", "args_defaults", "timeout_seconds", "status_operation",
                "status_args_template", "status_ref_field", "async_ref_path", "concurrency",
                "max_items", "on_item_error", "operation_kind",
            },
            "map": common_fields | {"items", "fields", "where", "sort", "limit"},
            "operation": common_fields | {
                "contract", "operation", "operation_id", "args_template", "args_defaults",
                "timeout_seconds", "status_operation", "status_args_template", "status_ref_field", "async_ref_path",
            },
            "stepped_operation": common_fields | {
                "contract", "operation", "operation_id", "args_template", "args_defaults",
                "timeout_seconds", "status_operation", "status_args_template", "status_ref_field", "async_ref_path",
            },
            "async_operation": common_fields | {
                "contract", "operation", "operation_id", "args_template", "args_defaults",
                "timeout_seconds", "status_operation", "status_args_template", "status_ref_field", "async_ref_path",
            },
            "gate:choice": common_fields | {"options_template", "options_limit"},
            "gate:review": common_fields | {"items_template", "item_review"},
            "notify": common_fields | {"message_template"},
            "condition": common_fields | {"when"},
        }[kind]
        unknown_stage_fields = set(stage) - stage_fields
        if unknown_stage_fields:
            fields = ", ".join(sorted(str(field) for field in unknown_stage_fields))
            raise PlatformValidationError(f"Stage {stage_id} contains unsupported fields: {fields}.")
        ids.add(stage_id)
        stage_kinds[stage_id] = kind
        item_template_fields = {
            "for_each": {"input", "args_template", "args_defaults", "status_args_template"},
            "map": {"fields"},
        }.get(kind, set())
        if _allow_item_templates:
            item_template_fields = {"args_template", "args_defaults", "status_args_template"}
        for field, value in stage.items():
            if any(
                path.removesuffix("?") == "item" or path.removesuffix("?").startswith("item.") or path.removesuffix("?") == "item_index"
                for path in _template_paths(value)
            ) and field not in item_template_fields:
                raise PlatformValidationError(f"Item templates are outside the item scope for stage {stage_id}.")
        for path in _template_paths(stage):
            path = path[:-1] if path.endswith("?") else path
            item_reference = path == "item" or path.startswith("item.") or path == "item_index"
            if not (path == "run.vars" or path.startswith("run.vars.") or path.startswith("stages.") or item_reference):
                raise PlatformValidationError(f"Unsupported template reference: {path}")
            if path.startswith("stages."):
                referenced_stage = path.split(".", 2)[1] if len(path.split(".")) > 1 else ""
                if referenced_stage not in ids:
                    raise PlatformValidationError(f"Stage {stage_id} references a stage that must run earlier: {referenced_stage}.")
        if kind == "for_each":
            items_template = stage.get("items")
            if not isinstance(items_template, str) or _TEMPLATE.fullmatch(items_template) is None:
                raise PlatformValidationError(f"Stage {stage_id} items must be a template reference.")
            items_path = _TEMPLATE.fullmatch(items_template).group(1).removesuffix("?")
            if not (items_path == "run.vars" or items_path.startswith("run.vars.") or items_path.startswith("stages.")):
                raise PlatformValidationError(f"Stage {stage_id} items must reference run.vars or a previous stage.")
            input_template = stage.get("input", {})
            if not isinstance(input_template, dict):
                raise PlatformValidationError(f"Stage {stage_id} input must be an object.")
            operation_ref = str(stage.get("operation") or "").strip()
            contract = str(stage.get("contract") or "").strip()
            operation = str(stage.get("operation_name") or "").strip()
            operation_id = str(stage.get("operation_id") or "").strip()
            if operation_ref and not operation and not operation_id:
                if contract:
                    operation = operation_ref
                elif hasattr(operations, "resolve_operation"):
                    operation_id = operation_ref
                elif "." in operation_ref:
                    contract, operation = operation_ref.rsplit(".", 1)
                else:
                    operation_id = operation_ref
            elif operation_ref and not operation:
                operation = operation_ref
            operation_stage = {
                "id": stage_id,
                "kind": "operation",
                "contract": contract,
                "operation": operation,
                "operation_id": operation_id,
                "args_template": input_template,
                "args_defaults": stage.get("args_defaults", {}),
                "timeout_seconds": stage.get("timeout_seconds", 30),
            }
            for field in ("status_operation", "status_args_template", "status_ref_field", "async_ref_path"):
                if field in stage:
                    operation_stage[field] = stage[field]
            declared = None
            if hasattr(operations, "resolve_operation"):
                try:
                    declared = _catalogue_operation(
                        operations, actor=actor, project_id=project_id, operation_id=operation_id,
                        contract=contract, operation=operation,
                    )
                except PlatformValidationError:
                    if operation_id != operation_ref or contract or operation or "." not in operation_ref:
                        raise
                    contract, operation = operation_ref.rsplit(".", 1)
                    operation_id = ""
                    declared = _catalogue_operation(
                        operations, actor=actor, project_id=project_id, contract=contract, operation=operation,
                    )
                if not contract:
                    contract = str(declared.get("contract") or "")
                if not operation:
                    operation = str(declared.get("operation") or "")
                operation_id = str(declared.get("operation_id") or operation_id)
                operation_stage.update({
                    "contract": contract, "operation": operation, "operation_id": operation_id,
                })
                operation_kind = "async_operation" if isinstance(declared.get("async"), dict) else "operation"
                if operation_kind != "async_operation":
                    line_operations = operations.operations_for_lines(actor, project_id=project_id)
                    line_operation = next((item for item in line_operations if item.get("operation_id") == operation_id), {})
                    if declared.get("run_actions") or line_operation.get("run_actions"):
                        operation_kind = "stepped_operation"
                operation_stage["kind"] = operation_kind
            else:
                key = (contract, operation)
                if not contract or not operation or key not in operations:
                    raise PlatformValidationError(f"Stage {stage_id} references an operation that is not in the operation_directory.")
                declared = operations[key] if isinstance(operations, dict) else {}
                operation_stage["kind"] = str((declared or {}).get("stage_kind") or ("async_operation" if isinstance((declared or {}).get("async"), dict) else "stepped_operation" if (declared or {}).get("run_actions") else "operation"))
            operation_stage["kind"] = str(operation_stage["kind"])
            if operation_stage["kind"] not in {"operation", "stepped_operation", "async_operation"}:
                raise PlatformValidationError(f"Stage {stage_id} operation kind is not supported.")
            normalized_operation = validate_definition(
                {"stages": [operation_stage]}, operation_directory=operations,
                actor=actor, project_id=project_id, _allow_item_templates=True,
            )["stages"][0]
            stage["contract"] = normalized_operation["contract"]
            stage["operation"] = normalized_operation["operation"]
            stage["operation_id"] = normalized_operation.get("operation_id", "")
            stage["operation_kind"] = normalized_operation["kind"]
            stage["args_template"] = input_template
            for field in ("status_operation", "status_args_template", "status_ref_field", "async_ref_path", "timeout_seconds"):
                if field in normalized_operation:
                    stage[field] = normalized_operation[field]
            concurrency = stage.get("concurrency", 1)
            max_items = stage.get("max_items", 100)
            if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= _MAX_FOR_EACH_CONCURRENCY:
                raise PlatformValidationError(f"Stage {stage_id} concurrency must be an integer from 1 to {_MAX_FOR_EACH_CONCURRENCY}.")
            if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= _MAX_FOR_EACH_ITEMS:
                raise PlatformValidationError(f"Stage {stage_id} max_items must be an integer from 1 to {_MAX_FOR_EACH_ITEMS}.")
            if stage.get("on_item_error", "continue") not in {"continue", "stop"}:
                raise PlatformValidationError(f"Stage {stage_id} on_item_error must be continue or stop.")
        if kind == "map":
            items_template = stage.get("items")
            if not isinstance(items_template, str) or _TEMPLATE.fullmatch(items_template) is None:
                raise PlatformValidationError(f"Stage {stage_id} items must be a template reference.")
            items_path = _TEMPLATE.fullmatch(items_template).group(1).removesuffix("?")
            if not (items_path == "run.vars" or items_path.startswith("run.vars.") or items_path.startswith("stages.")):
                raise PlatformValidationError(f"Stage {stage_id} items must reference run.vars or a previous stage.")
            fields = stage.get("fields")
            if not isinstance(fields, dict) or len(fields) > 100:
                raise PlatformValidationError(f"Stage {stage_id} fields must be an object with at most 100 fields.")
            for name, spec in fields.items():
                if not isinstance(name, str) or not name or len(name) > 100:
                    raise PlatformValidationError(f"Stage {stage_id} field names must be non-empty strings up to 100 characters.")
                references = _template_paths(spec)
                if references:
                    if not isinstance(spec, str) or _TEMPLATE.fullmatch(spec) is None:
                        raise PlatformValidationError(f"Stage {stage_id} fields support only a direct item template or a JSON constant.")
                    reference = _TEMPLATE.fullmatch(spec).group(1).removesuffix("?")
                    if not (reference == "item" or reference.startswith("item.") or reference == "item_index"):
                        raise PlatformValidationError(f"Stage {stage_id} fields may reference only item or item_index.")
                else:
                    _encode(spec, f"Stage {stage_id} map field")
            where = stage.get("where")
            if where is not None:
                if not isinstance(where, dict) or set(where) - {"path", "op", "value"}:
                    raise PlatformValidationError(f"Stage {stage_id} where must contain path, op, and an optional value.")
                where_path, where_op = where.get("path"), where.get("op")
                if not isinstance(where_path, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", where_path):
                    raise PlatformValidationError(f"Stage {stage_id} where path must be a relative field path.")
                if not isinstance(where_op, str) or where_op not in ("=", "!=", ">", ">=", "<", "<=", "exists"):
                    raise PlatformValidationError(f"Stage {stage_id} where op is unsupported.")
                if (where_op == "exists" and "value" in where) or (where_op != "exists" and "value" not in where):
                    raise PlatformValidationError(f"Stage {stage_id} where value is required except for exists.")
                if "value" in where:
                    _encode(where["value"], f"Stage {stage_id} filter value")
            sort = stage.get("sort")
            if sort is not None:
                if not isinstance(sort, dict) or set(sort) != {"path", "order"}:
                    raise PlatformValidationError(f"Stage {stage_id} sort must contain path and order.")
                if not isinstance(sort.get("path"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", sort["path"]):
                    raise PlatformValidationError(f"Stage {stage_id} sort path must be a relative field path.")
                if sort.get("order") not in ("asc", "desc"):
                    raise PlatformValidationError(f"Stage {stage_id} sort order must be asc or desc.")
            limit = stage.get("limit")
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_FOR_EACH_ITEMS):
                raise PlatformValidationError(f"Stage {stage_id} limit must be an integer from 1 to {_MAX_FOR_EACH_ITEMS}.")
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
                unknown_status_fields = set(status_operation) - {"contract", "operation", "operation_id"}
                if unknown_status_fields:
                    fields = ", ".join(sorted(str(field) for field in unknown_status_fields))
                    raise PlatformValidationError(f"Stage {stage_id} status_operation contains unsupported fields: {fields}.")
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
        if kind == "gate:review":
            stage["item_review"] = any(
                len(parts := path.removesuffix("?").split(".")) >= 3
                and parts[0] == "stages" and parts[2] == "items"
                and stage_kinds.get(parts[1]) == "for_each"
                for path in _template_paths(stage.get("items_template"))
            )
        if kind in {"operation", "stepped_operation", "async_operation"}:
            timeout = stage.get("timeout_seconds", 30)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= 300:
                raise PlatformValidationError(f"Stage {stage_id} timeout_seconds must be between 1 and 300.")
        if kind == "condition":
            condition = stage.get("when")
            if not isinstance(condition, dict) or not isinstance(condition.get("path"), str):
                raise PlatformValidationError(f"Stage {stage_id} needs a simple condition path.")
            unknown_condition_fields = set(condition) - {"path", "op", "value", "equals", "truthy", "action"}
            if unknown_condition_fields:
                fields = ", ".join(sorted(str(field) for field in unknown_condition_fields))
                raise PlatformValidationError(f"Stage {stage_id} condition contains unsupported fields: {fields}.")
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
            CREATE TABLE IF NOT EXISTS line_stage_items (
                tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, stage_id TEXT NOT NULL,
                item_index INTEGER NOT NULL, status TEXT NOT NULL, input_json TEXT NOT NULL,
                output_json TEXT NOT NULL DEFAULT 'null', error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                started_at REAL, ended_at REAL, runner_key TEXT NOT NULL DEFAULT '',
                runner_phase TEXT NOT NULL DEFAULT 'start', runner_payload_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (tenant_id, run_id, stage_id, item_index)
            );
            CREATE INDEX IF NOT EXISTS line_stage_items_status ON line_stage_items(tenant_id, run_id, stage_id, status, item_index);
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

    def assert_assistant_review_allowed(self, run_id: str) -> dict[str, Any]:
        """Apply the saved line review policy before an assistant decides a gate."""
        run = self.get_run(run_id)
        if run["status"] != "waiting" or not run["current_stage"]:
            raise PlatformConflictError("This run is not waiting for a review decision.")
        line = self.db.get_line(str(run["line_id"]))
        if line["definition"].get("review_by", "dashboard") != "dashboard_or_assistant":
            raise PlatformOperationError(
                "This line allows review decisions only in the dashboard. Open /reviews to decide."
            )
        stage = self._stage(run_id, str(run["current_stage"]))
        output = next((item.get("output") for item in run["stages"] if item["stage_id"] == run["current_stage"]), {})
        waiting = output.get("waiting", {}) if isinstance(output, dict) else {}
        if stage.get("kind") != "gate:review" and waiting.get("kind") != "review":
            raise PlatformConflictError("This run is not waiting for a review decision.")
        return run

    def list_runs(
        self, *, line_id: str | None = None, project_id: str | None = None,
        status: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT id FROM line_runs WHERE tenant_id=? AND archived_at IS NULL", [self.tenant_id]
        if line_id:
            sql += " AND line_id=?"; args.append(line_id)
        if project_id:
            sql += " AND project_id=?"; args.append(project_id)
        if status:
            # Filter before LIMIT so older runs in this status are not cut off.
            sql += " AND status=?"; args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"; args.append(max(1, min(int(limit), 500)))
        with self.db._connection() as db:
            ids = [str(row["id"]) for row in db.execute(sql, args).fetchall()]
        return [self.get_run(run_id) for run_id in ids]

    def archive_run(self, run_id: str, *, actor_id: str) -> None:
        terminal = {"done", "done_with_errors", "failed", "rejected", "cancelled", "canceled"}
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
            db.execute("DELETE FROM line_stage_items WHERE tenant_id=? AND run_id=?", (self.tenant_id, run_id))
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
            previous = db.execute("SELECT status FROM line_runs WHERE tenant_id=? AND id=?", (self.tenant_id, run_id)).fetchone()
            db.execute(f"UPDATE line_runs SET {','.join(updates)} WHERE tenant_id=? AND id=?", args)
        if status in {"done", "done_with_errors", "failed", "rejected", "cancelled", "canceled"}:
            from app.services.media_links import MediaLinksService

            MediaLinksService(self.store, tenant_id=self.tenant_id).revoke_run(
                run_id, reason=f"line_run_{status}",
            )
        # Notify only after the run's links are revoked, so nothing observes a
        # finished run whose review links still work.
        if status is not None and (previous is None or str(previous["status"]) != status):
            self._notify_status_change(run_id, status)

    _STATUS_EVENTS = {
        "waiting": ("review_waiting", "Review waiting"),
        "failed": ("run_failed", "Run failed"),
        "done": ("run_done", "Run finished"),
        "done_with_errors": ("run_done", "Run finished with errors"),
    }

    def _waiting_for_review(self, run_id: str, stage_id: str) -> bool:
        # Choice gates send their own Telegram prompt; only reviews notify here.
        if self._stage(run_id, stage_id).get("kind") == "gate:review":
            return True
        with self.db._connection() as db:
            row = db.execute(
                "SELECT output_json FROM line_stage_runs WHERE tenant_id=? AND run_id=? AND stage_id=?",
                (self.tenant_id, run_id, stage_id),
            ).fetchone()
        try:
            output = json.loads(str(row["output_json"])) if row is not None else {}
        except ValueError:
            return False
        waiting = output.get("waiting") if isinstance(output, dict) else None
        return isinstance(waiting, dict) and waiting.get("kind") == "review"

    def _notify_status_change(self, run_id: str, status: str) -> None:
        """Tell linked chats about reviews, failures and finished runs.

        Sent from a short-lived thread: the engine is called from sync workers,
        request threads and async MCP tools, and a notification must never slow
        down or break a run.
        """
        event = self._STATUS_EVENTS.get(status)
        if event is None:
            return
        try:
            row = self._run_row(run_id)
            project_id = str(row["project_id"] or "")
            if not project_id:
                return
            stage_id = str(row["current_stage"] or "")
            if status == "waiting" and not self._waiting_for_review(run_id, stage_id):
                return
            line = self.db.get_line(str(row["line_id"]))
        except Exception:
            return
        event_class, label = event
        from app.config import get_settings

        base = get_settings().public_url.rstrip("/")
        where = f"{base}/reviews" if status == "waiting" else f"{base}/runs/{run_id}"
        body = f"{line['name']}" + (f" · stage {stage_id}" if stage_id else "") + "\n" + where
        tenant_id = self.tenant_id

        def send() -> None:
            try:
                from app.services.notification_gateway import send_notification

                asyncio.run(send_notification(
                    tenant_id=tenant_id, project_id=project_id, event_class=event_class,
                    title=label, body=body, intent="IMMEDIATE",
                    dedupe_key=f"line-status:{run_id}:{stage_id}:{status}", actor="system",
                ))
            except Exception:
                pass

        threading.Thread(target=send, name=f"line-notify-{run_id[:8]}", daemon=True).start()

    def _stage(self, run_id: str, stage_id: str) -> dict[str, Any]:
        line = self.db.get_line(self._run_row(run_id)["line_id"])
        return next((stage for stage in line["definition"]["stages"] if stage["id"] == stage_id), {})

    def _stage_item_rows(self, run_id: str, stage_id: str, *, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM line_stage_items WHERE tenant_id=? AND run_id=? AND stage_id=?"
        values: list[Any] = [self.tenant_id, run_id, stage_id]
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            values.extend(statuses)
        query += " ORDER BY item_index"
        with self.db._connection() as db:
            rows = db.execute(query, values).fetchall()
        return [{
            "item_index": int(row["item_index"]), "status": str(row["status"]),
            "input": self.db._object(row["input_json"], None),
            "output": self.db._object(row["output_json"], None), "error": str(row["error"]),
            "attempts": int(row["attempts"]), "started_at": row["started_at"], "ended_at": row["ended_at"],
            "runner_key": str(row["runner_key"] or ""), "runner_phase": str(row["runner_phase"] or "start"),
            "runner_payload": self.db._object(row["runner_payload_json"], {}),
        } for row in rows]

    def _stage_item_update(
        self, run_id: str, stage_id: str, item_index: int, *, status: str,
        output: Any = None, keep_output: bool = False, error: str = "",
        runner_key: str | None = None, runner_phase: str | None = None,
        runner_payload: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        terminal = status in {"succeeded", "failed", "skipped", "cancelled"}
        with self.db._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT output_json,started_at FROM line_stage_items WHERE tenant_id=? AND run_id=? AND stage_id=? AND item_index=?",
                (self.tenant_id, run_id, stage_id, item_index),
            ).fetchone()
            if old is None:
                db.rollback()
                raise PlatformNotFoundError("Stage item was not found.")
            encoded = str(old["output_json"] or "null") if keep_output else json.dumps(
                output, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True,
            )
            if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
                db.rollback()
                raise PlatformValidationError("Stage item output exceeds the 64 KiB limit.")
            payload_json = _encode(runner_payload, "Stage item operation arguments") if runner_payload is not None else None
            started = old["started_at"] or (now if status in {"running", "waiting"} else None)
            db.execute(
                """UPDATE line_stage_items SET status=?,output_json=?,error=?,started_at=?,ended_at=?,
                   runner_key=COALESCE(?,runner_key),runner_phase=COALESCE(?,runner_phase),
                   runner_payload_json=COALESCE(?,runner_payload_json)
                   WHERE tenant_id=? AND run_id=? AND stage_id=? AND item_index=?""",
                (status, encoded, str(error or "")[:1000], started, now if terminal else None,
                 runner_key, runner_phase, payload_json, self.tenant_id, run_id, stage_id, item_index),
            )
            db.commit()

    def _claim_stage_item(self, run_id: str, stage_id: str) -> dict[str, Any] | None:
        with self.db._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT item_index,input_json,attempts FROM line_stage_items WHERE tenant_id=? AND run_id=? AND stage_id=? AND status='queued' ORDER BY item_index LIMIT 1",
                (self.tenant_id, run_id, stage_id),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            now = time.time()
            changed = db.execute(
                "UPDATE line_stage_items SET status='running',attempts=attempts+1,started_at=?,ended_at=NULL,error='' WHERE tenant_id=? AND run_id=? AND stage_id=? AND item_index=? AND status='queued'",
                (now, self.tenant_id, run_id, stage_id, int(row["item_index"])),
            ).rowcount
            db.commit()
        if not changed:
            return None
        return {
            "item_index": int(row["item_index"]),
            "input": self.db._object(row["input_json"], None),
            "attempts": int(row["attempts"] or 0) + 1,
        }

    @staticmethod
    def _for_each_operation_stage(stage: dict[str, Any]) -> dict[str, Any]:
        result = dict(stage)
        result["kind"] = str(stage.get("operation_kind") or "operation")
        result["args_template"] = stage.get("input", stage.get("args_template", {}))
        return result

    def _item_context(self, run_id: str, stage_id: str, item_index: int) -> dict[str, Any]:
        row = self._run_row(run_id)
        context = self.db._object(row["vars_json"], {"vars": {}, "stages": {}})
        with self.db._connection() as db:
            item = db.execute(
                "SELECT input_json FROM line_stage_items WHERE tenant_id=? AND run_id=? AND stage_id=? AND item_index=?",
                (self.tenant_id, run_id, stage_id, item_index),
            ).fetchone()
        if item is None:
            raise PlatformNotFoundError("Stage item was not found.")
        return {**context, "item": self.db._object(item["input_json"], None), "item_index": item_index}

    def _template_context(self, run_id: str, context: dict[str, Any], value: Any) -> dict[str, Any]:
        stages = dict(context.get("stages") or {})
        changed = False
        for path in _template_paths(value):
            parts = path.removesuffix("?").split(".")
            if len(parts) < 3 or parts[0] != "stages" or parts[2] not in {"items", "failed"}:
                continue
            source_id, key = parts[1], parts[2]
            source_stage = self._stage(run_id, source_id)
            if source_stage.get("kind") != "for_each":
                continue
            rows = self._stage_item_rows(run_id, source_id)
            selected = [row for row in rows if row["status"] == ("succeeded" if key == "items" else "failed")]
            if key == "items":
                materialized = [row["output"] for row in selected]
            else:
                materialized = [{"index": row["item_index"], "input": row["input"], "error": row["error"]} for row in selected]
            _encode(materialized, "Stage item template results")
            record = dict(stages.get(source_id) or {})
            record[key] = materialized
            stages[source_id] = record
            changed = True
        return {**context, "stages": stages} if changed else context

    def _for_each_counts(self, run_id: str, stage_id: str) -> dict[str, int]:
        rows = self._stage_item_rows(run_id, stage_id)
        return {
            "total": len(rows),
            "succeeded": sum(row["status"] == "succeeded" for row in rows),
            "failed": sum(row["status"] == "failed" for row in rows),
            "skipped": sum(row["status"] in {"skipped", "cancelled"} for row in rows),
        }

    def _for_each_output(self, run_id: str, stage_id: str) -> dict[str, Any]:
        return {**self._for_each_counts(run_id, stage_id), "results_ref": f"{run_id}:{stage_id}"}

    def _start_for_each(self, run_id: str, stage: dict[str, Any], *, actor: Any = None) -> None:
        stage_id = str(stage["id"])
        rows = self._stage_item_rows(run_id, stage_id)
        if not rows:
            run = self._run_row(run_id)
            context = self.db._object(run["vars_json"], {"vars": {}, "stages": {}})
            resolved = _resolve(stage["items"], self._template_context(run_id, context, stage["items"]))
            if not isinstance(resolved, list):
                raise PlatformValidationError("for_each items must resolve to a list.")
            if len(resolved) > int(stage.get("max_items", 100)):
                raise PlatformValidationError(f"for_each received {len(resolved)} items; max_items is {int(stage.get('max_items', 100))}.")
            with self.db._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for index, item in enumerate(resolved):
                    db.execute(
                        "INSERT OR IGNORE INTO line_stage_items(tenant_id,run_id,stage_id,item_index,status,input_json) VALUES(?,?,?,?,'queued',?)",
                        (self.tenant_id, run_id, stage_id, index, _encode(item, "Stage item input")),
                    )
                db.commit()
        self._stage_update(run_id, stage_id, status="running", output=self._for_each_output(run_id, stage_id))
        self._set_run(run_id, status="running", current_stage=stage_id)
        self._dispatch_for_each(run_id, stage, actor=actor)

    def _dispatch_for_each(self, run_id: str, stage: dict[str, Any], *, actor: Any = None) -> None:
        if self._run_row(run_id)["status"] in {"cancelled", "failed"}:
            return
        stage_id = str(stage["id"])
        rows = self._stage_item_rows(run_id, stage_id)
        if stage.get("on_item_error", "continue") == "stop" and any(row["status"] == "failed" for row in rows):
            for row in rows:
                if row["status"] == "queued":
                    self._stage_item_update(run_id, stage_id, row["item_index"], status="skipped", keep_output=True)
            rows = self._stage_item_rows(run_id, stage_id)
        active = sum(row["status"] in {"running", "waiting"} for row in rows)
        concurrency = min(int(stage.get("concurrency", 1)), _MAX_FOR_EACH_CONCURRENCY)
        operation_stage = self._for_each_operation_stage(stage)
        while active < concurrency:
            item = self._claim_stage_item(run_id, stage_id)
            if item is None:
                break
            active += 1
            try:
                self._schedule_stage(
                    run_id, operation_stage, actor=actor, item_index=item["item_index"],
                    retry=int(item.get("attempts") or 1) > 1,
                )
            except Exception as exc:
                self._stage_item_update(
                    run_id, stage_id, item["item_index"], status="failed", error=str(exc), keep_output=True,
                )
                self._event(run_id, "stage.item_failed", stage_id=stage_id,
                            detail={"index": item["item_index"], "error": str(exc)[:500]})
                active -= 1
                if stage.get("on_item_error", "continue") == "stop":
                    for queued in self._stage_item_rows(run_id, stage_id, statuses=("queued",)):
                        self._stage_item_update(run_id, stage_id, queued["item_index"], status="skipped", keep_output=True)
                    break
        self._refresh_for_each(run_id, stage, actor=actor)

    def _refresh_for_each(self, run_id: str, stage: dict[str, Any], *, actor: Any = None) -> None:
        stage_id = str(stage["id"])
        rows = self._stage_item_rows(run_id, stage_id)
        summary = self._for_each_output(run_id, stage_id)
        queued = any(row["status"] == "queued" for row in rows)
        active = any(row["status"] == "running" for row in rows)
        waiting = any(row["status"] == "waiting" for row in rows)
        failed = summary["failed"] > 0
        if queued or active or waiting:
            status = "waiting" if waiting and not active else "running"
            self._stage_update(run_id, stage_id, status=status, output=summary)
            self._set_run(run_id, status=status, current_stage=stage_id)
            return
        if (failed or summary["skipped"] > 0) and stage.get("on_item_error", "continue") == "stop":
            message = "for_each stopped after an item failed."
            self._stage_update(run_id, stage_id, status="failed", output=summary, error=message)
            self._set_run(run_id, status="failed", current_stage=stage_id)
            self._event(run_id, "stage.failed", stage_id=stage_id, detail={"error": message})
            return
        if failed and summary["succeeded"] == 0 and summary["skipped"] == 0:
            message = "for_each failed because every item failed."
            self._stage_update(run_id, stage_id, status="failed", output=summary, error=message)
            self._set_run(run_id, status="failed", current_stage=stage_id)
            self._event(run_id, "stage.failed", stage_id=stage_id, detail={"error": message})
            return
        status = "done_with_errors" if failed else "done"
        self._stage_update(run_id, stage_id, status=status, output=summary)
        self._merge_stage_output(run_id, stage_id, summary)
        self._event(run_id, "stage.done_with_errors" if failed else "stage.done", stage_id=stage_id, detail=summary)
        self._advance(run_id, actor=actor)

    def stage_items(self, run_id: str, stage_id: str, *, status_filter: str = "all") -> dict[str, Any]:
        self._run_row(run_id)
        stage = self._stage(run_id, stage_id)
        if stage.get("kind") != "for_each":
            raise PlatformNotFoundError("Stage items were not found.")
        if status_filter not in {"all", "failed", "succeeded"}:
            raise PlatformValidationError("Stage item status filter must be all, failed, or succeeded.")
        rows = self._stage_item_rows(run_id, stage_id)
        if status_filter != "all":
            rows = [row for row in rows if row["status"] == status_filter]
        run_row = self._run_row(run_id)
        actor = self.db._object(run_row["actor_json"], {})
        items = []
        for row in rows:
            output = row["output"]
            if isinstance(output, dict):
                from app.services.review_media import attach_review_media
                media = attach_review_media(
                    [output], tenant_id=self.tenant_id, project_id=str(run_row["project_id"] or ""),
                    actor=actor, run_id=run_id, store=self.store,
                )
                if media:
                    output = media[0]
            items.append({key: row[key] for key in (
                "item_index", "status", "input", "output", "error", "attempts", "started_at", "ended_at",
            )})
            items[-1]["output"] = output
        return {**self._for_each_output(run_id, stage_id), "items": items}

    def retry_failed_items(self, run_id: str, stage_id: str, *, actor: Any = None, actor_id: str = "") -> dict[str, Any]:
        run = self.get_run(run_id)
        stage = self._stage(run_id, stage_id)
        stage_run = next((row for row in run["stages"] if row["stage_id"] == stage_id), None)
        if stage.get("kind") != "for_each" or stage_run is None or stage_run["status"] not in {"failed", "done_with_errors"}:
            raise PlatformConflictError("Only failed items in a completed for_each stage can be retried.")
        if run["status"] not in {"failed", "done_with_errors"}:
            raise PlatformConflictError("Failed items can be retried after the run finishes.")
        if not any(row["status"] == "failed" for row in self._stage_item_rows(run_id, stage_id)):
            raise PlatformConflictError("This stage has no failed items to retry.")
        with self.db._connection() as db, db:
            db.execute(
                "UPDATE line_stage_items SET status='queued',error='',ended_at=NULL WHERE tenant_id=? AND run_id=? AND stage_id=? AND status='failed'",
                (self.tenant_id, run_id, stage_id),
            )
        self._stage_update(run_id, stage_id, status="running", output=self._for_each_output(run_id, stage_id), error="")
        self._set_run(run_id, status="running", current_stage=stage_id)
        self._event(run_id, "stage.failed_items_retry_requested", stage_id=stage_id, detail={}, actor_id=actor_id)
        self._dispatch_for_each(run_id, stage, actor=actor)
        return self.get_run(run_id)

    def _handle_for_each_update(self, run_id: str, stage: dict[str, Any], update: dict[str, Any]) -> None:
        stage_id = str(stage["id"])
        item_index = int((update.get("metadata") or {}).get("item_index"))
        metadata = update.get("metadata") if isinstance(update.get("metadata"), dict) else {}
        phase = str(metadata.get("phase") or "start")
        result = update.get("result") if isinstance(update.get("result"), dict) else {}
        status = str(update.get("status") or "")
        if status in {"queued", "waiting", "succeeded"}:
            try:
                _encode(result, "Stage item output")
            except PlatformValidationError as exc:
                status = "failed"
                result = {}
                update = {**update, "error": str(exc)}
        item = next((row for row in self._stage_item_rows(run_id, stage_id) if row["item_index"] == item_index), None)
        if item is None or item["status"] in {"succeeded", "failed", "skipped", "cancelled"}:
            return
        if status == "queued":
            self._stage_item_update(run_id, stage_id, item_index, status="running", output=result)
        elif status == "waiting":
            self._stage_item_update(run_id, stage_id, item_index, status="waiting", output=result)
            self._event(run_id, "stage.item_waiting", stage_id=stage_id, detail={"index": item_index})
        elif status == "failed":
            error = str(update.get("error") or result.get("error") or "Operation failed.")
            self._stage_item_update(run_id, stage_id, item_index, status="failed", output=result, error=error)
            self._event(run_id, "stage.item_failed", stage_id=stage_id,
                        detail={"index": item_index, "error": error[:500]})
        elif status == "succeeded":
            if stage.get("operation_kind") == "async_operation" and phase == "start":
                self._stage_item_update(run_id, stage_id, item_index, status="running", output=result)
                item_context = self._item_context(run_id, stage_id, item_index)
                item_context = {**item_context, "stages": {**dict(item_context.get("stages") or {}), stage_id: {"output": result}}}
                status_path = str(stage.get("async_ref_path") or "")
                reference: Any = result if status_path else None
                for part in status_path.split(".") if status_path else []:
                    reference = reference.get(part) if isinstance(reference, dict) else None
                poll_args = _resolve(stage.get("status_args_template", {}), item_context)
                if reference is not None:
                    poll_args = dict(poll_args) if isinstance(poll_args, dict) else {}
                    if not poll_args:
                        poll_args[str(stage.get("status_ref_field") or "ref")] = reference
                if reference is not None or poll_args:
                    self._schedule_stage(
                        run_id, self._for_each_operation_stage(stage), phase="poll", payload=poll_args,
                        item_index=item_index,
                    )
                    return
            if stage.get("operation_kind") == "async_operation" and phase.startswith("poll"):
                result_status = str(result.get("status") or "").lower()
                if result_status in {"queued", "pending", "accepted", "processing", "running"}:
                    previous = int(phase.split("-", 1)[1]) if "-" in phase and phase.split("-", 1)[1].isdigit() else 0
                    payload = item.get("runner_payload") if isinstance(item.get("runner_payload"), dict) else {}
                    self._schedule_stage(
                        run_id, self._for_each_operation_stage(stage), phase=f"poll-{previous + 1}",
                        payload=payload, item_index=item_index,
                    )
                    return
            self._stage_item_update(run_id, stage_id, item_index, status="succeeded", output=result)
            self._event(run_id, "stage.item_succeeded", stage_id=stage_id, detail={"index": item_index})
        self._dispatch_for_each(run_id, stage)

    def _advance(self, run_id: str, *, actor: Any = None) -> None:
        row = self._run_row(run_id)
        if row["status"] in {"cancelled", "failed", "done", "done_with_errors"}:
            return
        line = self.db.get_line(str(row["line_id"]))
        context = self.db._object(row["vars_json"], {"vars": {}, "stages": {}})
        stages = line["definition"]["stages"]
        stage_runs = self.get_run(run_id)["stages"]
        completed = {item["stage_id"] for item in stage_runs if item["status"] in {"done", "done_with_errors", "skipped"}}
        next_stage = next((stage for stage in stages if stage["id"] not in completed), None)
        if next_stage is None:
            partial = any(item["status"] == "done_with_errors" for item in stage_runs)
            final_status = "done_with_errors" if partial else "done"
            self._set_run(run_id, status=final_status, current_stage="")
            self._event(run_id, "run.done_with_errors" if partial else "run.done", detail={})
            return
        stage_id, kind = str(next_stage["id"]), str(next_stage["kind"])
        self._set_run(run_id, status="running", current_stage=stage_id)
        self._ensure_stage(run_id, stage_id)
        if kind in {"operation", "stepped_operation", "async_operation"}:
            try:
                self._schedule_stage(run_id, next_stage, actor=actor)
            except Exception as exc:
                self._fail_stage(run_id, stage_id, exc)
        elif kind == "for_each":
            try:
                self._start_for_each(run_id, next_stage, actor=actor)
            except Exception as exc:
                self._fail_stage(run_id, stage_id, exc)
        elif kind == "map":
            try:
                items = _resolve(next_stage["items"], self._template_context(run_id, context, next_stage["items"]))
                output = _apply_map(next_stage, items)
                self._stage_update(run_id, stage_id, status="done", output=output)
                self._merge_stage_output(run_id, stage_id, output)
                self._event(run_id, "stage.done", stage_id=stage_id, detail={"items": len(output["items"])})
                self._advance(run_id, actor=actor)
            except Exception as exc:
                self._fail_stage(run_id, stage_id, exc)
        elif kind in {"gate:choice", "gate:review"}:
            try:
                key = "options_template" if kind == "gate:choice" else "items_template"
                items = _resolve(next_stage[key], self._template_context(run_id, context, next_stage[key]))
                if not isinstance(items, list):
                    raise PlatformValidationError("Gate items must resolve to a list.")
                if kind == "gate:review" and next_stage.get("item_review"):
                    normalized_items = []
                    for index, value in enumerate(items):
                        item = dict(value) if isinstance(value, dict) else {"value": value}
                        item.setdefault("id", str(index))
                        normalized_items.append(item)
                    items = normalized_items
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
                actual = _path_value(self._template_context(run_id, context, condition["path"]), str(condition["path"]))
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
                message = str(_resolve(next_stage.get("message_template", ""), self._template_context(run_id, context, next_stage.get("message_template", ""))))
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
            ended = now if status in {"done", "done_with_errors", "failed", "skipped", "cancelled"} else None
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

    def _schedule_stage(self, run_id: str, stage: dict[str, Any], *, actor: Any = None, phase: str = "start", payload: dict[str, Any] | None = None, resume: bool = False, retry: bool = False, item_index: int | None = None) -> dict[str, Any]:
        from app.services import line_runner
        row = self._run_row(run_id)
        context = self.db._object(row["vars_json"], {"vars": {}, "stages": {}})
        if item_index is not None:
            context = self._item_context(run_id, str(stage["id"]), int(item_index))
        kind = str(stage["kind"])
        if phase.startswith("poll"):
            op_spec = stage["status_operation"]
            contract, operation = str(op_spec["contract"]), str(op_spec["operation"])
            operation_id = str(op_spec.get("operation_id") or "")
            status_template = stage.get("status_args_template", {})
            args = payload if payload is not None else _resolve(status_template, self._template_context(run_id, context, status_template))
        else:
            contract, operation = str(stage["contract"]), str(stage["operation"])
            operation_id = str(stage.get("operation_id") or "")
            args_template = stage.get("args_template", {})
            args = payload if payload is not None else _resolve(args_template, self._template_context(run_id, context, args_template))
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
        item_scope = f":{item_index}" if item_index is not None else ""
        run_key = line_runner.run_key_for(contract, operation, args, scope=f"line:{run_id}:{stage['id']}{item_scope}:{phase}")
        # The line owns operation grants; an external chooser only supplies a
        # decision and must not widen the automation actor's capabilities.
        if item_index is None:
            self._stage_update(run_id, str(stage["id"]), status="running", runner_key=run_key,
                               runner_phase=phase, runner_payload=args, increment=phase == "start")
        else:
            self._stage_item_update(run_id, str(stage["id"]), int(item_index), status="running",
                                    keep_output=True, runner_key=run_key, runner_phase=phase,
                                    runner_payload=args)
        self._set_run(run_id, status="running", current_stage=str(stage["id"]))
        line_runner.schedule_step(contract, operation, args, run_key=run_key, project_id=str(row["project_id"] or ""),
            actor=operation_actor, tenant_id=self.tenant_id, store=self.store, resume=resume, retry=retry,
            metadata={"line_run_id": run_id, "stage_id": str(stage["id"]), "phase": phase,
                      "operation_catalog_id": operation_id,
                      **({"item_index": int(item_index)} if item_index is not None else {}),
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
        stage = self._stage(run_id, stage_id)
        if stage.get("kind") == "for_each" and item is not None and item["status"] in {"failed", "done_with_errors"}:
            if item["status"] == "done_with_errors" and run["status"] != "done_with_errors":
                raise PlatformConflictError("A partially completed run must finish before the whole stage is retried.")
            with self.db._connection() as db, db:
                db.execute("DELETE FROM line_stage_items WHERE tenant_id=? AND run_id=? AND stage_id=?", (self.tenant_id, run_id, stage_id))
            self._stage_update(run_id, stage_id, status="queued", output={}, error="")
            self._set_run(run_id, status="running", current_stage=stage_id)
            self._event(run_id, "stage.retry_requested", stage_id=stage_id, detail={}, actor_id=actor_id)
            self._start_for_each(run_id, stage, actor=actor)
            return self.get_run(run_id)
        if item is None or item["status"] != "failed":
            raise PlatformConflictError("Only a failed stage can be retried.")
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
        if stage["kind"] == "for_each":
            waiting = self._stage_item_rows(run_id, stage_id, statuses=("waiting",))
            if not waiting:
                raise PlatformConflictError("No stage item is waiting for input.")
            item = waiting[0]
            item_index = int(item["item_index"])
            self._stage_item_update(run_id, stage_id, item_index, status="running", keep_output=True)
            self._set_run(run_id, status="running", current_stage=stage_id)
            operation_stage = self._for_each_operation_stage(stage)
            self._schedule_stage(
                run_id, operation_stage, actor=actor, phase=str(item["runner_phase"] or "start"),
                payload=item["runner_payload"], resume=True, item_index=item_index,
            )
        elif stage["kind"] in {"gate:choice", "gate:review"}:
            output = dict(value or {})
            if stage["kind"] == "gate:review" and stage.get("item_review"):
                source_items = entry.get("output", {}).get("items", [])
                source_by_id = {
                    str(item.get("id") or index): item
                    for index, item in enumerate(source_items) if isinstance(item, dict)
                } if isinstance(source_items, list) else {}
                decisions = output.get("items") if isinstance(output.get("items"), list) else []
                approved, rejected = [], []
                for index, decision in enumerate(decisions):
                    if not isinstance(decision, dict):
                        continue
                    item_id = str(decision.get("id") or index)
                    item = dict(source_by_id.get(item_id) or {"id": item_id})
                    item["decision"] = str(decision.get("decision") or "approved")
                    if decision.get("reason"):
                        item["reason"] = str(decision["reason"])
                    (rejected if item["decision"].lower() in {"refused", "rejected"} else approved).append(item)
                output["approved"], output["rejected"] = approved, rejected
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
            if stage["kind"] == "gate:review" and not stage.get("item_review") and (
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
        if row["status"] in {"done", "done_with_errors", "failed", "rejected", "cancelled", "canceled"}:
            raise PlatformConflictError("Run is already terminal.")
        if row["current_stage"]:
            stage_id = str(row["current_stage"])
            current_stage = self._stage(run_id, stage_id)
            if current_stage.get("kind") == "for_each":
                from app.services.line_runner import cancel_run
                for item in self._stage_item_rows(run_id, stage_id):
                    if item["status"] in {"running", "waiting"} and item["runner_key"]:
                        cancel_run(item["runner_key"], tenant_id=self.tenant_id, store=self.store)
                    if item["status"] in {"queued", "running", "waiting"}:
                        self._stage_item_update(run_id, stage_id, item["item_index"], status="cancelled", keep_output=True)
            else:
                active = self._stage_runner(run_id, stage_id)
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
    if stage_run is None or stage_run["status"] in {"done", "done_with_errors", "failed", "skipped", "cancelled"}:
        return
    if current["current_stage"] != stage_id:
        return
    if metadata.get("item_index") is not None:
        if stage.get("kind") == "for_each":
            engine._handle_for_each_update(run_id, stage, run)
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
