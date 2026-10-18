"""Provider-neutral operation catalog for configured integrations.

This is the first runtime slice of the universal-integration plan.  It does
not know about Buffer, video, social, or any other business domain.  The
existing MCP contract-mapping, OpenAPI discovery, and manifest-compiled
service-contract bridges supply the transport-specific entries; this module
exposes them as stable operation descriptors and performs the same project,
capability, schema, and idempotency checks for every operation.

GitHub source inspection, builds, and external runtimes are intentionally not
implemented here.  This module only makes already configured integrations
callable through one generic surface.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from app.services.capability_context import CapabilityInvocationContext
from app.services.operation_outcomes import terminal_operation_failure
from app.services.operation_trace import OperationTraceService
from app.services.platform_capabilities import allowed_capabilities
from app.services.platform_services import PlatformOperationError, PlatformValidationError
from app.services.tool_models import CoreToolDefinition


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _search_tokens(value: Any) -> tuple[str, ...]:
    """Normalize human queries and declared operation metadata alike.

    Integrations commonly expose dotted, underscored or hyphenated names
    (``media.video.render``). Splitting those names makes a natural-language
    query such as ``video render`` discover the same operation without adding
    provider or domain-specific aliases.
    """
    # ``\w`` is Unicode-aware, so this keeps Arabic (and other non-Latin)
    # operator queries discoverable.  Underscores remain separators because
    # integration operation identifiers commonly use them as word boundaries.
    return tuple(re.findall(r"[^\W_]+", str(value or "").casefold(), flags=re.UNICODE))


def _required_error(schema: dict[str, Any], value: dict[str, Any]) -> str:
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    missing = [str(item) for item in required if isinstance(item, str) and item not in value]
    if missing:
        return "Required integration operation fields are missing: " + ", ".join(missing)
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if schema.get("additionalProperties") is False:
        unexpected = [key for key in value if key not in properties]
        if unexpected:
            return "Unsupported integration operation fields: " + ", ".join(sorted(unexpected))
    for key, child_schema in properties.items():
        child_value = value.get(key)
        if isinstance(child_value, dict) and isinstance(child_schema, dict):
            nested_error = _required_error(child_schema, child_value)
            if nested_error:
                return nested_error
    return ""


def _schema_validation_error(schema: dict[str, Any], value: dict[str, Any]) -> str:
    """Validate generic operation arguments with the declared JSON Schema.

    Operation preparation is the common mutation boundary for MCP, OpenAPI and
    service contributions. Applying the full declared schema here prevents the
    model from spending repair turns on omitted enums, patterns or alternatives.
    """
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return f"Integration operation schema is invalid: {str(exc.message)[:240]}"
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda error: (list(error.absolute_path), error.message))
    if not errors:
        return ""
    error = errors[0]
    path = ".".join(str(part) for part in error.absolute_path)
    prefix = f"arguments.{path}" if path else "arguments"
    return f"{prefix} is invalid: {error.message}"


def _media_upload_fields(schema: Any, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    if not isinstance(schema, dict):
        return []
    fields: list[tuple[tuple[str, ...], str]] = []
    if schema.get("format") in {"lynewright-media", "lynewright-upload"}:
        fields.append((prefix, str(schema["format"])))
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, nested in properties.items():
            fields.extend(_media_upload_fields(nested, (*prefix, str(key))))
    return fields


def _media_path_get(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _media_path_set(value: dict[str, Any], path: tuple[str, ...], replacement: Any) -> None:
    current: Any = value
    for key in path[:-1]:
        if not isinstance(current, dict) or not isinstance(current.get(key), dict):
            return
        current = current[key]
    if path and isinstance(current, dict):
        current[path[-1]] = replacement


def _annotate_media_schema(schema: dict[str, Any], hints: Any) -> dict[str, Any]:
    result = json.loads(json.dumps(schema))
    media = hints.get("media") if isinstance(hints, dict) else None
    if not isinstance(media, dict):
        return result
    for raw_path, media_format in media.items():
        if media_format not in {"lynewright-media", "lynewright-upload"}:
            continue
        path = tuple(str(raw_path).split("."))
        if not path or any(not part for part in path):
            continue
        current: Any = result
        for part in path[:-1]:
            properties = current.get("properties") if isinstance(current, dict) else None
            current = properties.get(part) if isinstance(properties, dict) else None
        if isinstance(current, dict):
            properties = current.get("properties")
            leaf = path[-1]
            if isinstance(properties, dict) and isinstance(properties.get(leaf), dict):
                properties[leaf]["format"] = str(media_format)
            elif leaf == "body" and isinstance(current, dict):
                current["format"] = str(media_format)
    return result


def _materialize_media_uploads(
    schema: Any, arguments: dict[str, Any], *, store: Any, tenant_id: str, project_id: str,
    transport: str,
) -> dict[str, Any]:
    fields = _media_upload_fields(schema)
    upload_fields = {path for path, media_format in fields if media_format == "lynewright-upload"}
    if upload_fields and transport == "mcp":
        raise PlatformValidationError("MCP tools do not accept file uploads; use a lynewright-media link.")
    result = dict(arguments)
    for path in upload_fields:
        marker = _media_path_get(result, path)
        if not isinstance(marker, str) or not marker.startswith("lynewright-upload://"):
            raise PlatformValidationError(
                f"Upload argument {'.'.join(path)} must be a managed media reference from a previous line stage."
            )
        media_id = marker.removeprefix("lynewright-upload://").strip()
        if not media_id or "/" in media_id or "\\" in media_id:
            raise PlatformValidationError("Managed media upload reference is invalid.")
        from app.services.integration_media_transfer import managed_media_upload

        upload = managed_media_upload(
            store, tenant_id=tenant_id, project_id=project_id, media_id=media_id,
        )
        _media_path_set(result, path, upload)

    def reject_unbound_markers(node: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            for key, nested in node.items():
                reject_unbound_markers(nested, (*path, str(key)))
        elif isinstance(node, list):
            for index, nested in enumerate(node):
                reject_unbound_markers(nested, (*path, str(index)))
        elif isinstance(node, str) and node.startswith("lynewright-upload://") and path not in upload_fields:
            raise PlatformValidationError("Managed media upload references are allowed only in declared lynewright-upload arguments.")

    reject_unbound_markers(result)
    return result


def _resource_hint(field: str, schema: dict[str, Any] | None = None) -> str:
    """Infer a provider-neutral resource type from a declared input field.

    Integrations may provide the explicit ``x-lynewright-resource-type`` schema
    extension.  The suffix fallback keeps older MCP/OpenAPI mappings useful
    without adding provider/category branches (``account_id`` -> ``account``).
    """
    schema = schema if isinstance(schema, dict) else {}
    explicit = schema.get("x-lynewright-resource-type") or schema.get("x-resource-type")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().casefold()[:120]
    raw = str(field or "").strip()
    # Check camel-case before normalizing: ``accountId`` used to be folded to
    # ``accountid`` and silently missed the compatibility hint.
    if raw.endswith("Id") and len(raw) > 2:
        return raw[:-2].rstrip("_").casefold()[:120]
    value = raw.casefold()
    if value.endswith("_id") and len(value) > 3:
        return value[:-3]
    return ""


def _schema_view(schema: dict[str, Any], root: dict[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Resolve safe local schema composition for requirement discovery.

    External references are intentionally left alone: resolving them could add
    network behavior to preparation and make approval inputs non-deterministic.
    Ambiguous `oneOf`/`anyOf` variants are likewise not guessed.
    """
    if depth > 12 or not isinstance(schema, dict):
        return schema if isinstance(schema, dict) else {}
    result = dict(schema)
    ref = result.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target: Any = root
        for part in ref[2:].split("/"):
            if not isinstance(target, dict):
                target = None
                break
            target = target.get(part.replace("~1", "/").replace("~0", "~"))
        if isinstance(target, dict):
            base = _schema_view(target, root, depth=depth + 1)
            result = {**base, **{key: value for key, value in result.items() if key != "$ref"}}
    all_of = result.get("allOf")
    if isinstance(all_of, list):
        properties = dict(result.get("properties") or {}) if isinstance(result.get("properties"), dict) else {}
        required = list(result.get("required") or []) if isinstance(result.get("required"), list) else []
        for item in all_of:
            if not isinstance(item, dict):
                continue
            child = _schema_view(item, root, depth=depth + 1)
            if isinstance(child.get("properties"), dict):
                properties.update(child["properties"])
            if isinstance(child.get("required"), list):
                required.extend(str(name) for name in child["required"] if isinstance(name, str))
        if properties:
            result["properties"] = properties
        if required:
            result["required"] = list(dict.fromkeys(required))
    return result


def _contract_metadata(*sources: Any) -> dict[str, Any]:
    """Normalize optional result/semantic metadata from any transport.

    MCP mappings, OpenAPI discovery, service manifests, and native tools use
    different historical names. This adapter is deliberately key-based rather
    than provider-based so a new integration only declares its contract.
    """
    result: dict[str, Any] = {}
    aliases = {
        "output_schema": ("output_schema", "result_schema", "response_schema"),
        "input_semantics": ("input_semantics", "input_contract"),
        "output_semantics": ("output_semantics", "output_contract", "result_contract"),
        "completion_contract": ("completion_contract", "completion"),
        "artifact_contract": ("artifact_contract", "artifacts"),
        "contract_status": ("contract_status",),
    }
    for target, keys in aliases.items():
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in keys:
                value = source.get(key)
                if target == "contract_status":
                    if isinstance(value, str) and value.strip():
                        result[target] = value.strip()[:32]
                        break
                elif isinstance(value, dict):
                    result[target] = dict(value)
                    break
            if target in result:
                break
    if "artifact_contract" not in result:
        for source in sources:
            hint = source.get("output_hint") if isinstance(source, dict) else None
            media = hint.get("media") if isinstance(hint, dict) else None
            if isinstance(media, dict) and media:
                result["artifact_contract"] = {"media": dict(media)}
                break
    result.setdefault("contract_status", "declared")
    return result


def _resource_fields(
    schema: dict[str, Any], *, path: tuple[str, ...] = (), root: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return required resource fields at any declared object depth."""
    root = root or schema
    view = _schema_view(schema, root)
    properties = view.get("properties") if isinstance(view.get("properties"), dict) else {}
    required = view.get("required") if isinstance(view.get("required"), list) else []
    result: list[dict[str, Any]] = []
    for field in required:
        if not isinstance(field, str):
            continue
        child = properties.get(field) if isinstance(properties, dict) else None
        child_view = _schema_view(child, root) if isinstance(child, dict) else None
        current_path = path + (field,)
        if not path and field in {"project_id", "idempotency_key"}:
            continue
        hint = _resource_hint(field, child_view if isinstance(child_view, dict) else None)
        if hint:
            result.append({
                "field": ".".join(current_path), "path": list(current_path),
                "name": field, "resource_type": hint,
            })
        if isinstance(child_view, dict) and isinstance(child_view.get("properties"), dict):
            result.extend(_resource_fields(child, path=current_path, root=root))
    return result


def _path_value(value: dict[str, Any], path: list[str]) -> tuple[bool, Any]:
    current: Any = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _set_path(value: dict[str, Any], path: list[str], item: Any) -> None:
    current = value
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    if path:
        current[path[-1]] = item


def _merge_missing(value: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    for key, child in patch.items():
        if key not in result:
            result[key] = child
        elif isinstance(result.get(key), dict) and isinstance(child, dict):
            result[key] = _merge_missing(result[key], child)
    return result


def _missing_required_paths(
    schema: dict[str, Any], value: dict[str, Any], *, path: tuple[str, ...] = (), root: dict[str, Any] | None = None,
) -> list[str]:
    root = root or schema
    view = _schema_view(schema, root)
    properties = view.get("properties") if isinstance(view.get("properties"), dict) else {}
    required = view.get("required") if isinstance(view.get("required"), list) else []
    missing: list[str] = []
    for field in required:
        if not isinstance(field, str):
            continue
        current_path = path + (field,)
        child = properties.get(field) if isinstance(properties, dict) else None
        if field not in value:
            missing.append(".".join(current_path))
            continue
        child_value = value.get(field)
        if isinstance(child, dict) and isinstance(child_value, dict):
            missing.extend(_missing_required_paths(child, child_value, path=current_path, root=root))
    return missing


@dataclass(frozen=True, slots=True)
class IntegrationOperationDescriptor:
    """Normalized metadata for one callable operation revision."""

    operation_id: str
    integration_id: str
    connection_id: str
    contract: str
    operation: str
    capability: str
    risk_class: str
    read_only: bool
    destructive: bool
    input_schema: dict[str, Any]
    source: str = "mcp_contract_mapping"
    output_schema: dict[str, Any] | None = None
    input_semantics: dict[str, Any] | None = None
    output_semantics: dict[str, Any] | None = None
    completion_contract: dict[str, Any] | None = None
    artifact_contract: dict[str, Any] | None = None
    contract_status: str = "declared"
    kind: str = "native"
    async_config: dict[str, Any] | None = None

    @property
    def revision(self) -> str:
        return _fingerprint({
            "operation_id": self.operation_id,
            "integration_id": self.integration_id,
            "connection_id": self.connection_id,
            "contract": self.contract,
            "operation": self.operation,
            "capability": self.capability,
            "risk_class": self.risk_class,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "input_semantics": self.input_semantics,
            "output_semantics": self.output_semantics,
            "completion_contract": self.completion_contract,
            "artifact_contract": self.artifact_contract,
            "contract_status": self.contract_status,
            "kind": self.kind,
            "async": self.async_config,
        })[:32]

    def public(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "revision": self.revision,
            "integration_id": self.integration_id,
            "connection_id": self.connection_id,
            "contract": self.contract,
            "operation": self.operation,
            "capability": self.capability,
            "risk_class": self.risk_class,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "requires_approval": not self.read_only or self.destructive,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "input_semantics": self.input_semantics,
            "output_semantics": self.output_semantics,
            "completion_contract": self.completion_contract,
            "artifact_contract": self.artifact_contract,
            "contract_status": self.contract_status,
            "resource_requirements": _resource_fields(self.input_schema),
            "source": self.source,
            "kind": self.kind,
            "async": self.async_config,
        }


class IntegrationOperationExecutionStore:
    """Small durable store shared by every generic integration transport."""

    def __init__(self, store: Any, *, tenant_id: str) -> None:
        self.store = store
        self.tenant_id = str(tenant_id)

    def _ensure_schema(self, db: Any) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_integration_prepared_invocations (
                tenant_id TEXT NOT NULL,
                id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                revision TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                arguments_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'prepared',
                execution_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_platform_integration_prepared_lookup
                ON platform_integration_prepared_invocations
                   (tenant_id, project_id, operation_id, arguments_fingerprint);
            CREATE TABLE IF NOT EXISTS platform_integration_executions (
                tenant_id TEXT NOT NULL,
                id TEXT NOT NULL,
                prepared_invocation_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                result_json TEXT,
                error TEXT,
                cancellation_requested INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                PRIMARY KEY (tenant_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_platform_integration_execution_prepared
                ON platform_integration_executions(tenant_id, prepared_invocation_id);
            """
        )

    @staticmethod
    def _public_prepared(row: Any) -> dict[str, Any]:
        return {
            "prepared_invocation_id": str(row["id"]),
            "project_id": str(row["project_id"]),
            "operation_id": str(row["operation_id"]),
            "revision": str(row["revision"]),
            "arguments": json.loads(str(row["arguments_json"] or "{}")),
            "arguments_fingerprint": str(row["arguments_fingerprint"]),
            "status": str(row["status"]),
            "execution_id": str(row["execution_id"] or "") or None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _public_execution(row: Any) -> dict[str, Any]:
        raw_result = row["result_json"]
        try:
            result = json.loads(str(raw_result)) if raw_result else None
        except (TypeError, ValueError):
            result = None
        return {
            "execution_id": str(row["id"]),
            "prepared_invocation_id": str(row["prepared_invocation_id"]),
            "operation_id": str(row["operation_id"]),
            "status": str(row["status"]),
            "result": result,
            "error": str(row["error"] or "") or None,
            "cancellation_requested": bool(row["cancellation_requested"]),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    def create_prepared(
        self, *, project_id: str, operation_id: str, revision: str,
        arguments: dict[str, Any], arguments_fingerprint: str,
    ) -> dict[str, Any]:
        with self.store.connection() as db, db:
            self._ensure_schema(db)
            existing = db.execute(
                """SELECT * FROM platform_integration_prepared_invocations
                   WHERE tenant_id=? AND project_id=? AND operation_id=?
                     AND revision=? AND arguments_fingerprint=?
                   ORDER BY created_at DESC LIMIT 1""",
                (self.tenant_id, project_id, operation_id, revision, arguments_fingerprint),
            ).fetchone()
            if existing is not None and str(existing["status"]) not in {"expired", "cancelled"}:
                return self._public_prepared(existing)
            prepared_id = "pinv_" + uuid.uuid4().hex
            db.execute(
                """INSERT INTO platform_integration_prepared_invocations
                   (tenant_id,id,project_id,operation_id,revision,arguments_json,arguments_fingerprint)
                   VALUES (?,?,?,?,?,?,?)""",
                (self.tenant_id, prepared_id, project_id, operation_id, revision,
                 _canonical(arguments), arguments_fingerprint),
            )
            return self._public_prepared(db.execute(
                "SELECT * FROM platform_integration_prepared_invocations WHERE tenant_id=? AND id=?",
                (self.tenant_id, prepared_id),
            ).fetchone())

    def get_prepared(self, prepared_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            self._ensure_schema(db)
            row = db.execute(
                "SELECT * FROM platform_integration_prepared_invocations WHERE tenant_id=? AND id=?",
                (self.tenant_id, str(prepared_id)),
            ).fetchone()
        if row is None:
            raise PlatformOperationError("Prepared integration invocation was not found.")
        return self._public_prepared(row)

    def start_execution(self, prepared_id: str, operation_id: str) -> dict[str, Any]:
        with self.store.connection() as db, db:
            self._ensure_schema(db)
            existing = db.execute(
                """SELECT * FROM platform_integration_executions
                   WHERE tenant_id=? AND prepared_invocation_id=?
                   ORDER BY started_at DESC LIMIT 1""",
                (self.tenant_id, str(prepared_id)),
            ).fetchone()
            if existing is not None and str(existing["status"]) in {"running", "succeeded", "failed", "cancelled"}:
                return self._public_execution(existing)
            execution_id = "exec_" + uuid.uuid4().hex
            db.execute(
                """INSERT INTO platform_integration_executions
                   (tenant_id,id,prepared_invocation_id,operation_id,status)
                   VALUES (?,?,?,?, 'running')""",
                (self.tenant_id, execution_id, str(prepared_id), operation_id),
            )
            db.execute(
                """UPDATE platform_integration_prepared_invocations
                   SET status='executing', execution_id=?, updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND id=?""",
                (execution_id, self.tenant_id, str(prepared_id)),
            )
            return self._public_execution(db.execute(
                "SELECT * FROM platform_integration_executions WHERE tenant_id=? AND id=?",
                (self.tenant_id, execution_id),
            ).fetchone())

    def finish_execution(self, execution_id: str, *, status: str, result: Any = None, error: str = "") -> dict[str, Any]:
        if status not in {"running", "succeeded", "failed", "cancelled"}:
            raise PlatformValidationError("Invalid integration execution status.")
        with self.store.connection() as db, db:
            self._ensure_schema(db)
            row = db.execute(
                "SELECT * FROM platform_integration_executions WHERE tenant_id=? AND id=?",
                (self.tenant_id, str(execution_id)),
            ).fetchone()
            if row is None:
                raise PlatformOperationError("Integration execution was not found.")
            encoded = _canonical(result) if result is not None else None
            db.execute(
                """UPDATE platform_integration_executions
                   SET status=?, result_json=?, error=?,
                       completed_at=CASE WHEN ?='running' THEN NULL ELSE CURRENT_TIMESTAMP END
                   WHERE tenant_id=? AND id=?""",
                (status, encoded, str(error or "")[:2_000], status, self.tenant_id, str(execution_id)),
            )
            db.execute(
                """UPDATE platform_integration_prepared_invocations
                   SET status=?, updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND id=?""",
                (status, self.tenant_id, str(row["prepared_invocation_id"])),
            )
            return self._public_execution(db.execute(
                "SELECT * FROM platform_integration_executions WHERE tenant_id=? AND id=?",
                (self.tenant_id, str(execution_id)),
            ).fetchone())

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        with self.store.connection() as db:
            self._ensure_schema(db)
            row = db.execute(
                "SELECT * FROM platform_integration_executions WHERE tenant_id=? AND id=?",
                (self.tenant_id, str(execution_id)),
            ).fetchone()
        if row is None:
            raise PlatformOperationError("Integration execution was not found.")
        return self._public_execution(row)

    def cancel_execution(self, execution_id: str) -> dict[str, Any]:
        with self.store.connection() as db, db:
            self._ensure_schema(db)
            row = db.execute(
                "SELECT * FROM platform_integration_executions WHERE tenant_id=? AND id=?",
                (self.tenant_id, str(execution_id)),
            ).fetchone()
            if row is None:
                raise PlatformOperationError("Integration execution was not found.")
            if str(row["status"]) == "running":
                db.execute(
                    "UPDATE platform_integration_executions SET cancellation_requested=1 WHERE tenant_id=? AND id=?",
                    (self.tenant_id, str(execution_id)),
                )
            return self._public_execution(db.execute(
                "SELECT * FROM platform_integration_executions WHERE tenant_id=? AND id=?",
                (self.tenant_id, str(execution_id)),
            ).fetchone())


class IntegrationOperationCatalog:
    """Catalog and dispatcher for configured and native read operations.

    Installed integrations and the native Lynewright runtime are intentionally
    projected through the same discovery model.  Native mutations retain their
    direct runtime entry points until the generic invocation surface can carry
    the originating tool's approval context without weakening its policy gate.
    """

    def __init__(self, management: Any, *, native_runtime: Any | None = None):
        # The Operator compatibility bridge exposes the management service as
        # ``.management``; external MCP/API runtimes pass the service itself.
        # Keeping the catalog above that distinction is what lets all agent
        # surfaces share the same operation projection.
        self.management = getattr(management, "management", management)
        self.executions = IntegrationOperationExecutionStore(
            self.management.store, tenant_id=self.management.tenant_id,
        )
        self.contributions = getattr(self.management, "contributions", None)
        self.native_runtime = native_runtime

    def _entries(self, actor: Any, project_id: str) -> dict[str, dict[str, Any]]:
        project = str(project_id or "").strip()
        projectless_line = not project and str(getattr(actor, "client_type", "")) == "automation"
        if not project and not projectless_line:
            raise PlatformValidationError("Integration operations require a project_id.")
        capabilities = allowed_capabilities(actor)
        result: dict[str, dict[str, Any]] = {}
        service = self.management
        from app.services.production_integrations import (
            _catalogue_package_root, package_manifest_source_available,
        )
        from app.services.integration_provenance import get_provenance

        def package_available(integration: dict[str, Any]) -> bool:
            if integration.get("source") != "package":
                return True
            try:
                provenance = get_provenance(
                    service.store, service.tenant_id, str(integration.get("id") or ""),
                )
                if _catalogue_package_root(integration, service.integrations, provenance):
                    return True
            except Exception:
                pass
            return package_manifest_source_available(
                str(integration.get("id") or ""),
                str(integration.get("source_distribution") or ""),
                str(integration.get("source_entrypoint") or ""),
                lynewright_version=str(getattr(service.integrations, "lynewright_version", "0.1.0")),
            )

        for integration in service.integrations.list():
            if not integration.get("enabled") or not package_available(integration):
                continue
            kind = service._manual_kind(integration)
            if kind == "mcp" and project:
                for connection in service.connections.list(integration["id"]):
                    if not connection.get("enabled") or connection.get("project_id") not in {None, "", project}:
                        continue
                    for stored in service._mcp_contract_mappings(connection):
                        try:
                            mapping = service._validated_mcp_contract_mapping(integration, connection, stored)
                        except Exception:
                            continue
                        if not mapping.get("enabled") or mapping["required_capability"] not in capabilities:
                            continue
                        operation_id = self._operation_id(integration, connection, mapping)
                        result[operation_id] = {
                            "transport": "mcp", "integration": integration, "connection": connection,
                            "mapping": mapping, "definition": self._schema_definition(mapping),
                        }
            if kind == "openapi" and project:
                if "network:use" not in capabilities:
                    continue
                for connection in service.connections.list(integration["id"]):
                    if not connection.get("enabled") or connection.get("project_id") not in {None, "", project}:
                        continue
                    try:
                        operations = service.openapi_operations(integration["id"], str(connection["id"]))
                    except Exception:
                        continue
                    declaration = ((integration.get("configuration") or {}).get("manual") or {}).get("declaration") or {}
                    async_declaration = declaration.get("async") if isinstance(declaration, dict) else None
                    for operation in operations:
                        if not operation.get("approved"):
                            continue
                        operation_id = self._openapi_operation_id(integration, connection, operation)
                        entry = {
                            "transport": "openapi", "integration": integration, "connection": connection,
                            "operation": operation, "definition": self._schema_definition_openapi(operation),
                        }
                        if isinstance(async_declaration, dict) and async_declaration.get("operation_id") == operation.get("operation_id"):
                            entry["async_config"] = dict(async_declaration)
                        result[operation_id] = entry
                    for stored in service.openapi_contract_mappings(integration["id"], str(connection["id"])):
                        try:
                            mapping = service._validated_openapi_contract_mapping(
                                integration, str(connection["id"]), stored,
                            )
                        except Exception:
                            continue
                        if not mapping.get("enabled") or mapping["required_capability"] not in capabilities:
                            continue
                        remote_operation = next((
                            item for item in operations
                            if str(item.get("operation_id")) == str(mapping["operation_id"])
                        ), None)
                        if remote_operation is None or not remote_operation.get("approved"):
                            continue
                        operation_id = self._openapi_mapping_operation_id(integration, connection, mapping)
                        result[operation_id] = {
                            "transport": "openapi_mapping", "integration": integration,
                            "connection": connection, "mapping": mapping,
                            "operation": remote_operation,
                            "definition": self._schema_definition(mapping),
                        }
        if self.contributions is not None:
            enabled_integration_ids = {
                str(item["id"])
                for item in service.integrations.list()
                if item.get("enabled") and package_available(item)
            }
            # Manifest-compiled providers are service contributions too. Keep
            # them on this same generic surface so a new contract can declare
            # operations without a provider-specific tool or branch here.
            for contribution in self.contributions.list(kind="service"):
                if contribution.integration_id not in enabled_integration_ids:
                    continue
                metadata = contribution.metadata if isinstance(contribution.metadata, dict) else {}
                contract = str(metadata.get("contract") or "")
                if not contract.startswith("lynewright."):
                    continue
                declared_projects = set()
                if metadata.get("project_id") not in (None, ""):
                    declared_projects.add(str(metadata["project_id"]).strip())
                if isinstance(metadata.get("project_ids"), (list, tuple, set, frozenset)):
                    declared_projects.update(str(value).strip() for value in metadata["project_ids"] if value not in (None, ""))
                if declared_projects and project not in declared_projects:
                    continue
                caller_capability = str(metadata.get("caller_capability") or "")
                if caller_capability and caller_capability not in capabilities:
                    continue
                try:
                    integration = service.integrations.get(contribution.integration_id)
                    connection = (
                        service.connections.get(contribution.integration_id, contribution.connection_id)
                        if contribution.connection_id else
                        {"id": "", "project_id": None, "enabled": True}
                    )
                except Exception:
                    continue
                if not integration.get("enabled") or not connection.get("enabled"):
                    continue
                if connection.get("project_id") not in {None, "", project}:
                    continue
                for operation in self._service_operations(metadata):
                    operation_id = self._service_operation_id(contribution, operation)
                    async_operations = metadata.get("async_operations")
                    async_declaration = (
                        async_operations.get(str(operation.get("name") or ""))
                        if isinstance(async_operations, dict) else None
                    )
                    async_config: dict[str, Any] | None = None
                    if isinstance(async_declaration, dict):
                        poll_tool = str(async_declaration.get("poll_tool") or "").strip()
                        if poll_tool:
                            async_config = {
                                "status_contract": contract,
                                "status_operation": poll_tool,
                                "job_id_path": str(async_declaration.get("task_id_field") or "id"),
                                "status_argument": str(async_declaration.get("poll_argument") or ""),
                                "state_field": str(async_declaration.get("state_field") or "status"),
                                "active_states": list(async_declaration.get("active_states") or []),
                                "terminal_states": list(async_declaration.get("terminal_states") or []),
                            }
                    result[operation_id] = {
                        "transport": "service",
                        "integration": integration,
                        "connection": connection,
                        "contribution": contribution,
                        "contract": contract,
                        "operation": operation,
                        "definition": self._schema_definition_service(metadata, operation),
                        **({"async_config": async_config} if async_config else {}),
                    }
            for contribution in self.contributions.list(kind="mcp"):
                if contribution.integration_id not in enabled_integration_ids:
                    continue
                metadata = contribution.metadata if isinstance(contribution.metadata, dict) else {}
                capability = str(metadata.get("caller_capability") or "")
                if capability not in capabilities:
                    continue
                if str(metadata.get("project_scope") or "") == "required" and not project:
                    continue
                try:
                    integration = service.integrations.get(contribution.integration_id)
                    connection = service.connections.get(
                        contribution.integration_id, contribution.connection_id,
                    ) if contribution.connection_id else {"id": "", "project_id": None, "enabled": True}
                except Exception:
                    continue
                if contribution.connection_id and (not connection.get("enabled") or connection.get("project_id") not in {None, "", project}):
                    continue
                operation_id = self._contribution_operation_id(contribution)
                result[operation_id] = {
                    "transport": "contribution", "integration": integration, "connection": connection,
                    "contribution": contribution, "definition": self._schema_definition_contribution(metadata),
                }
        # Native read tools are capabilities too.  Showing them in the same
        # catalogue lets an agent discover a product, research result, or
        # site context before it chooses an installed integration to act on
        # it.  We deliberately keep native writes on their existing direct
        # routes for now: forwarding their approval through a second generic
        # invocation layer must never bypass the native tool policy gate.
        if self.native_runtime is not None:
            for definition in self.native_runtime.definitions():
                if not definition.read_only or definition.name.startswith("lynewright.integrations."):
                    continue
                if definition.capability not in capabilities:
                    continue
                operation_id = self._native_operation_id(definition)
                result[operation_id] = {
                    "transport": "native_runtime",
                    "integration": {"id": "lynewright.native.runtime", "enabled": True},
                    "connection": {"id": "native", "project_id": None, "enabled": True},
                    "definition": self._schema_definition_native(definition),
                    "native_definition": definition,
                    "description": definition.description,
                }
        return result

    @staticmethod
    def _operation_id(integration: dict[str, Any], connection: dict[str, Any], mapping: dict[str, Any]) -> str:
        identity = {
            "integration_id": integration["id"], "connection_id": connection["id"],
            "mapping_id": mapping["mapping_id"], "tool_name": mapping["tool_name"],
            "target_contract": mapping["target_contract"], "target_operation": mapping["target_operation"],
        }
        return "lynewright.integrations.operation." + _fingerprint(identity)[:24]

    @staticmethod
    def _openapi_operation_id(integration: dict[str, Any], connection: dict[str, Any], operation: dict[str, Any]) -> str:
        return "lynewright.integrations.operation." + _fingerprint({
            "integration_id": integration["id"], "connection_id": connection["id"],
            "operation_id": operation["operation_id"], "method": operation["method"],
            "path": operation["path"],
        })[:24]

    @staticmethod
    def _openapi_mapping_operation_id(integration: dict[str, Any], connection: dict[str, Any], mapping: dict[str, Any]) -> str:
        return "lynewright.integrations.operation." + _fingerprint({
            "integration_id": integration["id"], "connection_id": connection["id"],
            "mapping_id": mapping["mapping_id"], "operation_id": mapping["operation_id"],
            "target_contract": mapping["target_contract"], "target_operation": mapping["target_operation"],
        })[:24]

    @staticmethod
    def _contribution_operation_id(contribution: Any) -> str:
        return "lynewright.integrations.operation." + _fingerprint({
            "contribution_id": contribution.id,
            "metadata": contribution.metadata,
        })[:24]

    @staticmethod
    def _native_operation_id(definition: CoreToolDefinition) -> str:
        return "lynewright.integrations.operation." + _fingerprint({
            "native_tool": definition.name,
            "capability": definition.capability,
            "input_schema": definition.input_schema or {},
            "risk_class": definition.risk_class,
        })[:24]

    @staticmethod
    def _service_operations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize a provider's declared operation list without provider knowledge."""
        declared = metadata.get("operations")
        if declared is None:
            declared = metadata.get("capabilities")
        if not isinstance(declared, (list, tuple)):
            return []
        result: list[dict[str, Any]] = []
        for item in declared:
            if isinstance(item, str) and item.strip():
                result.append({"name": item.strip()})
            elif isinstance(item, dict):
                name = str(item.get("name") or item.get("operation") or item.get("capability") or "").strip()
                if name:
                    result.append({**item, "name": name})
        return result

    @staticmethod
    def _service_operation_id(contribution: Any, operation: dict[str, Any]) -> str:
        return "lynewright.integrations.operation." + _fingerprint({
            "contribution_id": contribution.id,
            "operation": operation,
        })[:24]

    @staticmethod
    def _schema_definition_service(metadata: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
        operation_name = str(operation.get("name") or "")
        schemas = metadata.get("operation_schemas")
        schema = schemas.get(operation_name) if isinstance(schemas, dict) else None
        operation_schema = schema if isinstance(schema, dict) else {}
        if not isinstance(schema, dict):
            schema = operation.get("input_schema") if isinstance(operation.get("input_schema"), dict) else metadata.get("input_schema")
        schema = dict(schema) if isinstance(schema, dict) else {"type": "object"}
        properties = dict(schema.get("properties") or {})
        required = list(schema.get("required") or [])
        properties.setdefault("project_id", {"type": "string"})
        if str(metadata.get("project_scope") or "") == "required" and "project_id" not in required:
            required.insert(0, "project_id")
        read_only = bool(operation.get("read_only", operation_schema.get("read_only", metadata.get("read_only", False))))
        if not read_only:
            properties.setdefault("idempotency_key", {"type": "string", "minLength": 8, "maxLength": 128})
            if "idempotency_key" not in required:
                required.append("idempotency_key")
        schema["properties"] = properties
        schema["required"] = required
        requested_risk = str(operation.get("risk_class") or operation_schema.get("risk_class") or metadata.get("risk_class") or "PUBLICATION")
        risk_class = requested_risk if requested_risk in {
            "READ_ONLY", "SAFE_OPERATION", "BOUNDED_CONFIG", "PUBLICATION",
            "DESTRUCTIVE", "MONEY_MOVING", "SECURITY_SENSITIVE",
        } else "PUBLICATION"
        return {
            "input_schema": schema,
            **_contract_metadata(operation, metadata),
            "risk_class": "READ_ONLY" if read_only else risk_class,
            "read_only": read_only,
        }

    @staticmethod
    def _schema_definition_contribution(metadata: dict[str, Any]) -> dict[str, Any]:
        schema = dict(metadata.get("input_schema") or {"type": "object"})
        properties = dict(schema.get("properties") or {})
        required = list(schema.get("required") or [])
        project_scope = str(metadata.get("project_scope") or "")
        if project_scope in {"required", "optional"}:
            properties.setdefault("project_id", {"type": "string"})
            if project_scope == "required" and "project_id" not in required:
                required.insert(0, "project_id")
        schema["properties"] = properties
        schema["required"] = required
        requested_risk = str(metadata.get("risk_class") or "PUBLICATION")
        risk_class = requested_risk if requested_risk in {
            "READ_ONLY", "SAFE_OPERATION", "BOUNDED_CONFIG", "PUBLICATION",
            "DESTRUCTIVE", "MONEY_MOVING", "SECURITY_SENSITIVE",
        } else "PUBLICATION"
        return {
            "input_schema": schema,
            **_contract_metadata(metadata),
            "risk_class": "READ_ONLY" if bool(metadata.get("read_only")) else risk_class,
            "read_only": bool(metadata.get("read_only")),
        }

    @staticmethod
    def _schema_definition(mapping: dict[str, Any]) -> dict[str, Any]:
        direct_properties = {
            path.split(".", 1)[0]: {}
            for path in mapping["argument_mapping"].values()
            if isinstance(path, str) and path and not path.startswith("$")
            and path.split(".", 1)[0] not in {"project_id", "idempotency_key", "arguments"}
        }
        write = bool(mapping["write_enabled"])
        return {
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    **direct_properties,
                    "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
                },
                "required": ["project_id", *direct_properties] + (["idempotency_key"] if write else []),
                "additionalProperties": False,
            },
            **_contract_metadata(mapping),
            "risk_class": "DESTRUCTIVE" if mapping["destructive"] else "PUBLICATION" if write else "READ_ONLY",
            "read_only": not write,
        }

    @staticmethod
    def _schema_definition_native(definition: CoreToolDefinition) -> dict[str, Any]:
        """Preserve the native schema verbatim instead of adding host fields."""
        schema = dict(definition.input_schema or {"type": "object"})
        return {
            "input_schema": schema,
            **_contract_metadata(definition.metadata()),
            "risk_class": "READ_ONLY",
            "read_only": True,
        }

    @staticmethod
    def _schema_definition_openapi(operation: dict[str, Any]) -> dict[str, Any]:
        write = bool(operation.get("destructive"))
        grouped: dict[str, dict[str, Any]] = {"path": {}, "query": {}, "headers": {}}
        required_grouped: dict[str, list[str]] = {"path": [], "query": [], "headers": []}
        for parameter in operation.get("parameters") or []:
            if not isinstance(parameter, dict) or parameter.get("in") not in grouped:
                continue
            location = str(parameter["in"])
            name = str(parameter.get("name") or "")
            if not name:
                continue
            grouped[location][name] = dict(parameter.get("schema") or {})
            if parameter.get("required") is True:
                required_grouped[location].append(name)
        sections = {
            location: {
                "type": "object", "properties": values,
                "required": required_grouped[location], "additionalProperties": False,
            }
            for location, values in grouped.items()
        }
        required = ["project_id"] + (["body"] if operation.get("request_body_required") else [])
        if write:
            required.append("idempotency_key")
        input_schema = {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    **sections, "body": operation.get("request_body_schema") or {},
                    "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
                },
                "required": required,
                "additionalProperties": False,
            }
        input_schema = _annotate_media_schema(input_schema, operation.get("input_hint"))
        return {
            "input_schema": input_schema,
            **_contract_metadata(operation),
            "risk_class": "PUBLICATION" if write else "READ_ONLY",
            "read_only": not write,
        }

    @staticmethod
    def _descriptor(operation_id: str, entry: dict[str, Any]) -> IntegrationOperationDescriptor:
        integration = entry["integration"]
        connection = entry["connection"]
        definition = entry["definition"]
        if entry["transport"] == "native_runtime":
            native_definition = entry["native_definition"]
            contract = "lynewright.native"
            operation = str(native_definition.name)
            capability = str(native_definition.capability)
            destructive = False
            source = "native_runtime"
        elif entry["transport"] == "mcp":
            mapping = entry["mapping"]
            contract = str(mapping["target_contract"])
            operation = str(mapping["target_operation"])
            capability = str(mapping["required_capability"])
            destructive = bool(mapping.get("destructive"))
            source = "mcp_contract_mapping"
        elif entry["transport"] == "openapi_mapping":
            mapping = entry["mapping"]
            contract = str(mapping["target_contract"])
            operation = str(mapping["target_operation"])
            capability = str(mapping["required_capability"])
            destructive = bool(mapping.get("destructive"))
            source = "openapi_contract_mapping"
        elif entry["transport"] == "contribution":
            contribution = entry["contribution"]
            metadata = contribution.metadata if isinstance(contribution.metadata, dict) else {}
            contract = str(metadata.get("contract") or "lynewright.integration")
            operation = str(metadata.get("name") or contribution.local_id)
            capability = str(metadata.get("caller_capability") or "")
            destructive = not bool(metadata.get("read_only")) and str(metadata.get("risk_class") or "PUBLICATION") in {"DESTRUCTIVE", "MONEY_MOVING", "SECURITY_SENSITIVE"}
            source = "mcp_contribution"
        elif entry["transport"] == "service":
            contribution = entry["contribution"]
            metadata = contribution.metadata if isinstance(contribution.metadata, dict) else {}
            operation_info = entry["operation"]
            contract = str(entry["contract"])
            operation = str(operation_info.get("name") or "")
            capability = str(metadata.get("caller_capability") or "mcp:client:use")
            destructive = not bool(definition["read_only"]) and str(definition["risk_class"]) in {"DESTRUCTIVE", "MONEY_MOVING", "SECURITY_SENSITIVE"}
            source = "service_contract"
        else:
            operation_info = entry["operation"]
            contract = "lynewright.openapi"
            operation = str(operation_info["operation_id"])
            capability = "network:use"
            destructive = bool(operation_info.get("destructive"))
            source = "openapi_discovery"
        contract_fields = _contract_metadata(
            definition,
            entry.get("mapping"),
            entry.get("operation"),
            (entry.get("contribution").metadata if entry.get("contribution") is not None else None),
            (entry.get("native_definition").metadata() if entry.get("native_definition") is not None else None),
        )
        return IntegrationOperationDescriptor(
            operation_id=operation_id,
            integration_id=str(integration["id"]),
            connection_id=str(connection["id"]),
            contract=contract,
            operation=operation,
            capability=capability,
            risk_class=str(definition["risk_class"]),
            read_only=bool(definition["read_only"]),
            destructive=destructive,
            input_schema=dict(definition["input_schema"]),
            source=source,
            output_schema=contract_fields.get("output_schema"),
            input_semantics=contract_fields.get("input_semantics"),
            output_semantics=contract_fields.get("output_semantics"),
            completion_contract=contract_fields.get("completion_contract"),
            artifact_contract=contract_fields.get("artifact_contract"),
            contract_status=str(contract_fields.get("contract_status") or "declared"),
            kind=("openapi" if entry["transport"] in {"openapi", "openapi_mapping"} else
                  "mcp" if entry["transport"] in {"mcp", "contribution"} else "native"),
            async_config=dict(entry["async_config"]) if isinstance(entry.get("async_config"), dict) else None,
        )

    @contextmanager
    def _trace_mapped_call(self, context: CapabilityInvocationContext, descriptor: IntegrationOperationDescriptor, entry: dict[str, Any]):
        if entry.get("transport") not in {"mcp", "openapi", "openapi_mapping"}:
            yield
            return
        traces = OperationTraceService(self.management.store, tenant_id=self.management.tenant_id)
        operation_name = f"{descriptor.contract}.{descriptor.operation}"
        traces.start(context, operation=operation_name, metadata={"source": descriptor.source})
        traces.record(
            context, stage="invoke", category="integration_operation", status="running",
            integration_id=descriptor.integration_id, connection_id=descriptor.connection_id,
            metadata={"operation_id": descriptor.operation_id, "kind": descriptor.kind},
        )
        try:
            yield
        except Exception as exc:
            traces.record(
                context, stage="invoke", category="integration_operation", status="failed",
                code=type(exc).__name__, message="Mapped integration operation failed.",
                integration_id=descriptor.integration_id, connection_id=descriptor.connection_id,
                metadata={"operation_id": descriptor.operation_id, "kind": descriptor.kind},
            )
            traces.finish(context.trace_id, tenant_id=context.tenant_id, status="failed", code=type(exc).__name__)
            raise
        traces.record(
            context, stage="invoke", category="integration_operation", status="completed",
            integration_id=descriptor.integration_id, connection_id=descriptor.connection_id,
            metadata={"operation_id": descriptor.operation_id, "kind": descriptor.kind},
        )
        traces.finish(context.trace_id, tenant_id=context.tenant_id, status="completed")

    def _materialize_declared_artifacts(
        self, result: Any, *, entry: dict[str, Any], actor: Any,
        project_id: str, context: CapabilityInvocationContext,
    ) -> Any:
        """Fetch only explicitly declared artifact URLs from this connection."""
        if not isinstance(result, dict):
            return result
        operation = entry.get("operation") if isinstance(entry.get("operation"), dict) else {}
        hint = operation.get("output_hint") if isinstance(operation, dict) else {}
        media_hints = hint.get("media") if isinstance(hint, dict) else None
        paths = [
            str(path).split(".") for path, kind in media_hints.items()
            if kind == "lynewright-artifact"
        ] if isinstance(media_hints, dict) else []
        if not paths:
            return result

        def selected(node: Any, parts: list[str], parent: dict[str, Any] | None = None) -> list[tuple[Any, dict[str, Any] | None]]:
            if not parts:
                return [(node, parent)]
            head, *tail = parts
            if head == "*":
                if not isinstance(node, list):
                    return []
                return [found for child in node for found in selected(child, tail, child if isinstance(child, dict) else parent)]
            if not isinstance(node, dict) or head not in node:
                return []
            child = node[head]
            return selected(child, tail, node)

        targets: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for path in paths:
            for value, parent in selected(result, path):
                if not isinstance(value, str) or not isinstance(parent, dict):
                    raise PlatformValidationError("Declared artifact output must contain a URL string.")
                if value not in seen:
                    seen.add(value)
                    targets.append((value, parent))
        if len(targets) > 12:
            raise PlatformValidationError("An operation returned too many declared artifacts.")
        if not targets:
            return result

        from urllib.parse import unquote, urlsplit
        from app.services.integration_artifacts import materialize_artifact_bytes

        integration = entry.get("integration") or {}
        connection = entry.get("connection") or {}
        origin = urlsplit(str(connection.get("endpoint_url") or ""))
        if not origin.scheme or not origin.hostname:
            raise PlatformValidationError("Artifact materialization requires a configured connection endpoint.")
        connection_id = str(connection.get("id") or "")
        sdk = self.management._sdk_factory().create(
            caller=actor,
            integration_id=str(integration.get("id") or ""),
            connection_id=connection_id or None,
            project_id=project_id,
            invocation_context=context,
        )
        media_refs: list[dict[str, Any]] = []
        for url, item in targets:
            target = urlsplit(url)
            same_origin = (
                target.scheme.lower() == origin.scheme.lower()
                and (target.hostname or "").lower() == (origin.hostname or "").lower()
                and (target.port or (443 if target.scheme == "https" else 80))
                    == (origin.port or (443 if origin.scheme == "https" else 80))
                and not target.username and not target.password and not target.fragment
            )
            decoded_parts = [unquote(part) for part in target.path.split("/")]
            if not same_origin or any(part in {".", ".."} for part in decoded_parts):
                raise PlatformValidationError("Artifact URL must remain on the integration connection origin.")
            endpoint_ref = (
                str(connection.get("internal_endpoint_ref") or "")
                if str(connection.get("origin_kind") or "") == "internal_endpoint" else ""
            )
            response = sdk.http.request(
                url, method="GET", max_bytes=10_000_000, timeout_seconds=30,
                internal_endpoint_ref=endpoint_ref,
            )
            if int(response.get("status_code") or 0) != 200:
                raise PlatformOperationError("Declared artifact download failed.")
            content_type = str(response.get("content_type") or item.get("content_type") or "application/octet-stream").split(";", 1)[0]
            name = str(item.get("name") or "artifact")[:255]
            stored = materialize_artifact_bytes(
                tenant_id=self.management.tenant_id,
                project_id=project_id or None,
                origin_url=url,
                body=response.get("body") or b"",
                name=name,
                content_type=content_type,
                max_bytes=10_000_000,
                integration_id=str(integration.get("id") or ""),
            )
            media_id = str(stored.get("id") or "")
            item["media_id"] = media_id
            item["url"] = f"managed://{media_id}"
            item["content_type"] = content_type
            media_refs.append({
                "media_id": media_id, "url": f"managed://{media_id}",
                "name": str(stored.get("name") or name), "content_type": content_type,
            })
        if media_refs:
            result["media_refs"] = media_refs
        return result

    def search(
        self, actor: Any, *, project_id: str, query: str = "",
        limit: int = 12, compact: bool = False, include_schema: bool = False,
    ) -> list[dict[str, Any]]:
        """Search authorized operations and optionally return small RAG cards.

        The default remains the historical full descriptor for compatibility.
        Operator callers use ``compact=True`` so discovery does not put every
        provider schema into the model context.  ``describe`` remains the
        authoritative schema expansion path.
        """
        query_tokens = set(_search_tokens(query))
        ranked: list[tuple[int, dict[str, Any]]] = []
        for operation_id, entry in self._entries(actor, project_id).items():
            descriptor = self._descriptor(operation_id, entry)
            metadata = entry.get("contribution").metadata if entry.get("contribution") is not None else {}
            if not isinstance(metadata, dict):
                metadata = {}
            operation_info = entry.get("operation")
            operation_info = operation_info if isinstance(operation_info, dict) else {}
            search_text = " ".join(
                str(value) for value in (
                    operation_id,
                    descriptor.integration_id,
                    descriptor.contract,
                    descriptor.operation,
                    descriptor.capability,
                    metadata.get("name"),
                    metadata.get("description"),
                    metadata.get("keywords"),
                    operation_info.get("name"),
                    operation_info.get("description"),
                    operation_info.get("keywords"),
                    entry.get("description"),
                ) if value is not None
            )
            searchable_tokens = set(_search_tokens(search_text))
            # Natural language intents contain contextual words that no
            # provider is expected to declare ("create a video for my first
            # product").  Rank by overlap instead of requiring every word so
            # discovery remains useful for all present and future contracts.
            score = sum(token in searchable_tokens for token in query_tokens)
            if query_tokens and not score:
                continue
            public = descriptor.public()
            public["search_text"] = search_text[:600]
            public["description"] = str(metadata.get("description") or operation_info.get("description") or entry.get("description") or "")[:300]
            public["keywords"] = metadata.get("keywords") or operation_info.get("keywords") or []
            if compact:
                schema = public.pop("input_schema", {})
                output_schema = public.pop("output_schema", None)
                if isinstance(output_schema, dict):
                    public["output_type"] = str(output_schema.get("type") or "object")
                    public["output_required"] = list(output_schema.get("required") or [])[:20]
                    if include_schema:
                        public["output_schema"] = output_schema
                for contract_key in ("input_semantics", "output_semantics", "completion_contract", "artifact_contract"):
                    if not include_schema:
                        public.pop(contract_key, None)
                properties = schema.get("properties") if isinstance(schema, dict) else {}
                public["required"] = list(schema.get("required") or [])[:30] if isinstance(schema, dict) else []
                public["parameters"] = {
                    str(name): str(value.get("type") or "object")
                    for name, value in list(properties.items())[:30]
                    if isinstance(name, str) and isinstance(value, dict)
                } if isinstance(properties, dict) else {}
                if include_schema:
                    public["input_schema"] = schema
            ranked.append((score, public))
        return [
            item for _score, item in sorted(
                ranked, key=lambda pair: (-pair[0], pair[1]["operation_id"], pair[1]["revision"])
            )
        ][:max(1, min(int(limit), 50))]

    def resolve_operation(
        self, actor: Any, *, project_id: str, operation_id: str = "",
        contract: str = "", operation: str = "", integration_id: str = "",
        connection_id: str = "",
    ) -> dict[str, Any]:
        """Resolve one currently authorized operation ID or legacy contract pair.

        The same filtered entry set enforces integration/connection state,
        tenant and project scope, capabilities, and MCP/OpenAPI approval for
        operator tools and production-line stages.
        """
        entries = self._entries(actor, project_id)
        requested_id = str(operation_id or "").strip()
        if requested_id:
            entry = entries.get(requested_id)
            if entry is None or (integration_id and str(entry["integration"].get("id")) != integration_id) \
                    or (connection_id and str(entry["connection"].get("id")) != connection_id):
                raise PlatformOperationError(
                    "Integration operation is unavailable: its integration or connection may be disabled, "
                    "out of project scope, or its remote operation is not approved."
                )
            descriptor = self._descriptor(requested_id, entry)
            if contract and str(contract) != descriptor.contract:
                raise PlatformValidationError("Operation id does not match the stage contract.")
            if operation and str(operation) != descriptor.operation:
                raise PlatformValidationError("Operation id does not match the stage operation.")
            return {"operation_id": requested_id, "descriptor": descriptor.public(), "entry": entry}
        wanted_contract, wanted_operation = str(contract or "").strip(), str(operation or "").strip()
        matches = []
        for key, entry in entries.items():
            descriptor = self._descriptor(key, entry)
            if descriptor.contract == wanted_contract and descriptor.operation == wanted_operation:
                if integration_id and descriptor.integration_id != integration_id:
                    continue
                if connection_id and descriptor.connection_id != connection_id:
                    continue
                matches.append((key, entry, descriptor))
        if not matches:
            raise PlatformOperationError(
                f"Operation {wanted_contract}.{wanted_operation} is unavailable: its integration or connection may be disabled, "
                "out of project scope, or its remote operation is not approved."
            )
        if len(matches) > 1 and all(entry.get("transport") == "service" for _, entry, _ in matches):
            # Old line definitions store only contract + operation. Preserve
            # the registry's provider-selection semantics when that pair has
            # multiple native providers, while pinning the selected provider
            # to its entry in this same filtered operation catalogue.
            from app.services.integration_capability_evidence import IntegrationCapabilityEvidenceService
            from app.services.integration_provider_selection import IntegrationProviderSelectionService
            from app.services.integration_service_contracts import IntegrationServiceContractRegistry

            native_registry = IntegrationServiceContractRegistry(
                self.contributions,
                evidence=IntegrationCapabilityEvidenceService(self.management.integrations),
                selections=IntegrationProviderSelectionService(
                    self.management.integrations, tenant_id=self.management.tenant_id,
                ),
                sdk_factory=self.management._sdk_factory(),
            )
            selected_provider = native_registry.resolve(wanted_contract, project_id=project).integration_id
            selected_matches = [item for item in matches if item[2].integration_id == selected_provider]
            if len(selected_matches) == 1:
                matches = selected_matches
        if len(matches) > 1:
            raise PlatformValidationError(
                f"Operation {wanted_contract}.{wanted_operation} resolves to multiple catalogue entries; specify operation_id."
            )
        resolved_id, entry, descriptor = matches[0]
        return {"operation_id": resolved_id, "descriptor": descriptor.public(), "entry": entry}

    def operations_for_lines(self, actor: Any, *, project_id: str) -> list[dict[str, Any]]:
        """Return authorized catalogue operations with line-builder metadata."""
        result = []
        for operation_id, entry in self._entries(actor, project_id).items():
            descriptor = self._descriptor(operation_id, entry)
            public = descriptor.public()
            public["schema"] = descriptor.input_schema
            if entry.get("transport") in {"openapi", "openapi_mapping"}:
                operation = entry.get("operation") or {}
                public["description"] = str(operation.get("description") or operation.get("summary") or "")
            elif entry.get("transport") == "mcp":
                public["description"] = f"Approved MCP mapping: {entry['mapping'].get('tool_name', '')}"
            elif entry.get("transport") == "service":
                metadata = entry["contribution"].metadata if isinstance(entry["contribution"].metadata, dict) else {}
                operation_info = entry.get("operation") or {}
                schemas = metadata.get("operation_schemas") if isinstance(metadata.get("operation_schemas"), dict) else {}
                declared = schemas.get(str(operation_info.get("name") or "")) if isinstance(schemas, dict) else {}
                public["description"] = str((declared or {}).get("description") or metadata.get("description") or "")
                public["run_actions"] = (declared or {}).get("run_actions", []) if isinstance(declared, dict) else []
            else:
                public["description"] = str(entry.get("description") or "")
            result.append(public)
        return sorted(result, key=lambda item: (item["kind"], item["contract"], item["operation"], item["operation_id"]))

    def describe(self, actor: Any, *, project_id: str, operation_id: str) -> dict[str, Any]:
        return self.resolve_operation(actor, project_id=project_id, operation_id=operation_id)["descriptor"]

    def search_resources(
        self, actor: Any, *, project_id: str, query: str = "", resource_type: str = "",
        integration_id: str = "", operation_id: str = "", limit: int = 20,
        auto_discover: bool = False,
    ) -> dict[str, Any]:
        """Search the shared external-resource inventory for any integration.

        This deliberately operates on the host inventory instead of knowing a
        Buffer/account, video/generation, or social/page vocabulary.  A
        connection may opt into one bounded refresh when the caller requests
        ``auto_discover``; failed discovery is returned as evidence rather than
        becoming an agent-loop exception.
        """
        project = str(project_id or "").strip()
        if not project:
            raise PlatformValidationError("Integration resources require a project_id.")
        requested_integration = str(integration_id or "").strip()
        requested_operation = str(operation_id or "").strip()
        requested_type = str(resource_type or "").strip().casefold()
        search = str(query or "").strip()[:200]
        max_items = max(1, min(int(limit), 100))

        entries = self._entries(actor, project)
        scopes: dict[tuple[str, str], dict[str, Any]] = {}
        if requested_operation:
            entry = entries.get(requested_operation)
            if entry is None:
                raise PlatformOperationError("Integration operation is unavailable for this project or operator.")
            integration = entry["integration"]
            connection = entry["connection"]
            scopes[(str(integration["id"]), str(connection["id"]))] = {
                "integration": integration, "connection": connection,
            }
        else:
            for integration in self.management.integrations.list():
                if not integration.get("enabled"):
                    continue
                if requested_integration and str(integration.get("id")) != requested_integration:
                    continue
                for connection in self.management.connections.list(str(integration["id"])):
                    if not connection.get("enabled"):
                        continue
                    if connection.get("project_id") not in {None, "", project}:
                        continue
                    scopes[(str(integration["id"]), str(connection["id"]))] = {
                        "integration": integration, "connection": connection,
                    }

        bound = {
            (str(item.get("integration_id")), str(item.get("connection_id")), str(item.get("external_resource_id")))
            for item in self.management.bindings.list(project_id=project)
            if str(item.get("state") or "") not in {"DETACHED", "RETIRING"}
        }
        items: list[dict[str, Any]] = []
        discovery: list[dict[str, Any]] = []
        seen_scopes: set[tuple[str, str]] = set()
        for (current_integration, current_connection), scope in scopes.items():
            connection = scope["connection"]
            try:
                result = self.management.inventory.list(
                    integration_id=current_integration,
                    connection_id=current_connection,
                    resource_type=requested_type or None,
                    search=search or None,
                    limit=max_items,
                )
            except Exception as exc:
                discovery.append({"integration_id": current_integration, "connection_id": current_connection,
                                  "status": "inventory_error", "message": str(exc)[:240]})
                continue
            current = list(result.get("items") or [])
            if auto_discover and not current and (current_integration, current_connection) not in seen_scopes:
                seen_scopes.add((current_integration, current_connection))
                try:
                    refreshed = self.management.discover(current_integration, current_connection)
                    discovery.append({"integration_id": current_integration, "connection_id": current_connection,
                                      "status": "refreshed", "resource_count": len(refreshed.get("resources") or [])})
                    current = list(self.management.inventory.list(
                        integration_id=current_integration,
                        connection_id=current_connection,
                        resource_type=requested_type or None,
                        search=search or None,
                        limit=max_items,
                    ).get("items") or [])
                except Exception as exc:
                    discovery.append({"integration_id": current_integration, "connection_id": current_connection,
                                      "status": "discovery_failed", "message": str(exc)[:240]})
            for item in current:
                if str(item.get("state") or "") != "ACTIVE":
                    continue
                # Shared connections expose only resources explicitly bound to
                # this project. Project-owned connections are already scoped.
                if connection.get("project_id") in {None, ""} and (
                    current_integration, current_connection, str(item.get("external_id") or "")
                ) not in bound:
                    continue
                items.append({
                    "id": item.get("id"), "external_id": item.get("external_id"),
                    "resource_type": item.get("resource_type"), "name": item.get("name"),
                    "url": item.get("url"), "remote_status": item.get("remote_status"),
                    "state": item.get("state"), "metadata": item.get("metadata") or {},
                    "available_actions": item.get("available_actions") or [],
                    "integration_id": current_integration, "connection_id": current_connection,
                    "binding_status": "BOUND" if (current_integration, current_connection, str(item.get("external_id") or "")) in bound else "UNBOUND",
                })
                if len(items) >= max_items:
                    break
            if len(items) >= max_items:
                break
        return {
            "project_id": project, "query": search, "resource_type": requested_type or None,
            "items": items[:max_items], "total": len(items[:max_items]),
            "discovery": discovery,
            "source": "lynewright.integrations.search_resources",
        }

    def resolve_requirements(
        self, actor: Any, *, project_id: str, operation_id: str,
        arguments: dict[str, Any] | None = None, auto_discover: bool = True,
    ) -> dict[str, Any]:
        """Resolve schema-required resource identifiers before approval.

        A single active match is safe to propose as an arguments patch. Zero or
        multiple matches are returned as explicit blockers, so a model never
        has to invent an account/page/workspace identifier or retry guesses.
        """
        descriptor = self.describe(actor, project_id=project_id, operation_id=operation_id)
        payload = dict(arguments or {}) if isinstance(arguments, dict) else {}
        schema = descriptor["input_schema"] if isinstance(descriptor.get("input_schema"), dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        missing = _missing_required_paths(schema, payload)
        patches: dict[str, Any] = {}
        fields: list[dict[str, Any]] = []
        for field in _resource_fields(schema):
            name, hint = str(field["name"]), str(field["resource_type"])
            path = list(field["path"])
            provided, _provided_value = _path_value(payload, path)
            if provided:
                fields.append({**field, "status": "provided"})
                continue
            result = self.search_resources(
                actor, project_id=project_id, operation_id=operation_id,
                query=name.removesuffix("_id"), resource_type=hint,
                limit=20, auto_discover=auto_discover,
            )
            candidates = list(result.get("items") or [])
            # If a provider declares a richer type (for example social_account)
            # but the mapping uses account_id, retry across its own inventory
            # using the field stem; this remains provider-neutral.
            if not candidates:
                result = self.search_resources(
                    actor, project_id=project_id, operation_id=operation_id,
                    query=name.removesuffix("_id"), limit=20, auto_discover=False,
                )
                candidates = list(result.get("items") or [])
            entry: dict[str, Any] = {**field, "status": "missing", "candidates": candidates[:10],
                                     "discovery": result.get("discovery") or []}
            if len(candidates) == 1:
                _set_path(patches, path, candidates[0].get("external_id"))
                entry["status"] = "resolved"
                entry["selected"] = candidates[0]
            elif len(candidates) > 1:
                entry["status"] = "ambiguous"
            fields.append(entry)
        unresolved = [item["field"] for item in fields if item.get("status") in {"missing", "ambiguous"}]
        prepared_payload = _merge_missing(payload, patches)
        missing = _missing_required_paths(schema, prepared_payload)
        return {
            "project_id": str(project_id), "operation_id": str(operation_id),
            "descriptor": {
                "integration_id": descriptor.get("integration_id"),
                "connection_id": descriptor.get("connection_id"),
                "operation": descriptor.get("operation"),
                "required": required,
                "resource_requirements": descriptor.get("resource_requirements") or [],
            },
            "arguments": payload, "arguments_patch": patches,
            "missing_required": missing, "unresolved_resources": unresolved,
            "fields": fields, "ready_for_prepare": not missing and not unresolved,
            "source": "lynewright.integrations.resolve_requirements",
        }

    def prepare(
        self, actor: Any, *, project_id: str, operation_id: str,
        arguments: dict[str, Any] | None = None, invocation_key: str = "",
    ) -> dict[str, Any]:
        descriptor = self.describe(actor, project_id=project_id, operation_id=operation_id)
        payload = dict(arguments or {})
        if not isinstance(arguments, dict):
            raise PlatformValidationError("Integration operation arguments must be an object.")
        properties = descriptor["input_schema"].get("properties")
        if isinstance(properties, dict) and "project_id" in properties:
            payload.setdefault("project_id", project_id)
        # Resolve unambiguous resource IDs from the shared inventory before
        # declaring an operation incomplete. This is additive and applies to
        # every MCP/OpenAPI/service contract without provider branches.
        resolved = self.resolve_requirements(
            actor, project_id=project_id, operation_id=operation_id,
            arguments=payload, auto_discover=True,
        )
        payload = _merge_missing(payload, resolved.get("arguments_patch") or {})
        error = _schema_validation_error(descriptor["input_schema"], payload)
        if error:
            missing = resolved.get("missing_required") or []
            unresolved = resolved.get("unresolved_resources") or []
            detail = {"missing_required": missing, "unresolved_resources": unresolved}
            if missing:
                error = "Required integration operation fields are missing: " + ", ".join(str(item) for item in missing)
            raise PlatformValidationError(f"{error} Resolve requirements before approval: {_canonical(detail)}")
        if not descriptor["read_only"] and not str(payload.get("idempotency_key") or "").strip():
            raise PlatformValidationError("Write integration operations require an explicit idempotency_key.")
        prepared = self.executions.create_prepared(
            project_id=project_id,
            operation_id=descriptor["operation_id"],
            revision=descriptor["revision"],
            arguments=payload,
            arguments_fingerprint=(
                _fingerprint({"arguments": payload, "invocation_key": invocation_key})
                if invocation_key else _fingerprint(payload)
            ),
        )
        return {
            **prepared,
            "operation_id": descriptor["operation_id"],
            "revision": descriptor["revision"],
            "project_id": project_id,
            "arguments": payload,
            "arguments_fingerprint": _fingerprint(payload),
            "requires_approval": descriptor["requires_approval"],
        }

    def invoke(
        self, actor: Any, *, project_id: str, operation_id: str,
        arguments: dict[str, Any] | None = None,
        prepared_invocation_id: str | None = None,
        invocation_key: str = "",
    ) -> Any:
        resolved_operation = self.resolve_operation(actor, project_id=project_id, operation_id=operation_id)
        entry = resolved_operation["entry"]
        if prepared_invocation_id:
            prepared = self.executions.get_prepared(prepared_invocation_id)
            if prepared["project_id"] != project_id or prepared["operation_id"] != operation_id:
                raise PlatformOperationError("Prepared invocation scope does not match this operation.")
            descriptor = self.describe(actor, project_id=project_id, operation_id=operation_id)
            if prepared["revision"] != descriptor["revision"]:
                raise PlatformOperationError("Prepared invocation revision is stale; prepare it again.")
            if prepared["status"] in {"cancelled", "expired", "failed", "succeeded"}:
                existing_id = prepared.get("execution_id")
                if existing_id:
                    return self.executions.get_execution(existing_id)
            arguments = prepared["arguments"]
        else:
            prepared = self.prepare(
                actor, project_id=project_id, operation_id=operation_id,
                arguments=arguments, invocation_key=invocation_key,
            )
        # Re-resolve after preparation so a disabled integration, revoked
        # mapping, changed approval, or project reassignment fails closed.
        resolved_operation = self.resolve_operation(actor, project_id=project_id, operation_id=operation_id)
        entry = resolved_operation["entry"]
        integration = entry["integration"]
        connection = entry["connection"]
        invocation_arguments = _materialize_media_uploads(
            resolved_operation["descriptor"].get("input_schema"),
            dict(prepared.get("arguments") or {}), store=self.management.store,
            tenant_id=self.management.tenant_id, project_id=project_id,
            transport=str(entry.get("transport") or ""),
        )
        execution = self.executions.start_execution(
            prepared["prepared_invocation_id"], prepared["operation_id"],
        )
        if execution["status"] == "succeeded":
            return execution
        if entry["transport"] == "native_runtime":
            try:
                # This branch is read-only by construction.  Going through
                # the runtime keeps its capability checks, rate limiting, and
                # audit record authoritative rather than calling a handler
                # directly from the catalogue.
                result = self.native_runtime.invoke(
                    entry["native_definition"].name,
                    actor,
                    invocation_arguments,
                )
            except Exception as exc:
                self.executions.finish_execution(execution["execution_id"], status="failed", error=type(exc).__name__)
                raise
            return self._finish(
                execution["execution_id"], result,
                output_schema=entry["definition"].get("output_schema"),
            )
        actor_type = "automation" if str(getattr(actor, "client_type", "")) == "automation" else "operator" if actor.client_type == "internal-operator" else "integration-agent"
        context = CapabilityInvocationContext.from_actor(
            actor,
            tenant_id=self.management.tenant_id,
            actor_type=actor_type,
            entry_point="production-line.operation.invoke" if actor_type == "automation" else "integration.operation.invoke",
            project_id=project_id,
            integration_id=str(integration["id"]),
            # A manifest-compiled service contribution may be tenant/project
            # scoped without owning a connection profile.  The catalogue uses
            # an empty display id for that case, but the capability context
            # must carry None so the SDK factory does not look up a phantom
            # connection record.
            connection_id=(str(connection["id"]) if connection.get("id") else None),
            idempotency_key=prepared["arguments"].get("idempotency_key"),
        )
        if entry["transport"] == "mcp":
            mapping = entry["mapping"]
            try:
                with self._trace_mapped_call(context, self._descriptor(operation_id, entry), entry):
                    result = self.management.execute_mcp_contract_mapping(
                        str(integration["id"]), str(connection["id"]), str(mapping["mapping_id"]),
                        invocation_arguments,
                        idempotency_key=prepared["arguments"].get("idempotency_key"),
                        confirm_destructive=bool(mapping["destructive"]),
                        caller=actor,
                        invocation_context=context,
                    )
            except Exception as exc:
                self.executions.finish_execution(execution["execution_id"], status="failed", error=type(exc).__name__)
                raise
            return self._finish(
                execution["execution_id"], result,
                output_schema=entry["definition"].get("output_schema"),
            )

        if entry["transport"] == "openapi_mapping":
            mapping = entry["mapping"]
            try:
                with self._trace_mapped_call(context, self._descriptor(operation_id, entry), entry):
                    result = self.management.execute_openapi_contract_mapping(
                        str(integration["id"]), str(connection["id"]), str(mapping["mapping_id"]),
                        invocation_arguments,
                        idempotency_key=prepared["arguments"].get("idempotency_key"),
                        caller=actor, invocation_context=context,
                    )
            except Exception as exc:
                self.executions.finish_execution(execution["execution_id"], status="failed", error=type(exc).__name__)
                raise
            result = self._materialize_declared_artifacts(
                result, entry=entry, actor=actor, project_id=project_id, context=context,
            )
            return self._finish(
                execution["execution_id"], result,
                output_schema=entry["definition"].get("output_schema"),
            )

        if entry["transport"] == "contribution":
            from app.services.integration_mcp_contributions import IntegrationMCPContributionBridge

            contribution = entry["contribution"]
            try:
                contribution_arguments = dict(invocation_arguments)
                if str(contribution.metadata.get("project_scope") or "") == "none":
                    contribution_arguments.pop("project_id", None)
                result = IntegrationMCPContributionBridge(
                    self.contributions, self.management._sdk_factory(),
                ).invoke(
                    contribution.id,
                    actor,
                    contribution_arguments,
                    invocation_context=context,
                )
            except Exception as exc:
                self.executions.finish_execution(execution["execution_id"], status="failed", error=type(exc).__name__)
                raise
            return self._finish(
                execution["execution_id"], result,
                output_schema=entry["definition"].get("output_schema"),
            )

        if entry["transport"] == "service":
            from app.services.integration_capability_evidence import IntegrationCapabilityEvidenceService
            from app.services.integration_provider_selection import IntegrationProviderSelectionService
            from app.services.integration_service_contracts import IntegrationServiceContractRegistry

            raw_arguments = dict(invocation_arguments)
            raw_arguments.pop("project_id", None)
            raw_arguments.pop("idempotency_key", None)
            raw_arguments.pop("operation_id", None)
            contribution = entry["contribution"]
            registry = IntegrationServiceContractRegistry(
                self.contributions,
                evidence=IntegrationCapabilityEvidenceService(self.management.integrations),
                selections=IntegrationProviderSelectionService(
                    self.management.integrations, tenant_id=self.management.tenant_id,
                ),
                sdk_factory=self.management._sdk_factory(),
            )
            try:
                result = registry.invoke(
                    str(entry["contract"]),
                    str(entry["operation"].get("name") or ""),
                    raw_arguments,
                    project_id=project_id,
                    require_verified=False,
                    actor=actor,
                    invocation_context=context,
                    provider_integration_id=str(integration["id"]),
                )
            except Exception as exc:
                self.executions.finish_execution(execution["execution_id"], status="failed", error=type(exc).__name__)
                raise
            return self._finish(
                execution["execution_id"], result,
                output_schema=entry["definition"].get("output_schema"),
            )

        operation = entry["operation"]
        raw_arguments = dict(invocation_arguments)
        raw_arguments.pop("project_id", None)
        idempotency_key = raw_arguments.pop("idempotency_key", None)
        raw_arguments.pop("operation_id", None)
        try:
            with self._trace_mapped_call(context, self._descriptor(operation_id, entry), entry):
                result = self.management.execute_openapi(
                    str(integration["id"]), str(connection["id"]), str(operation["operation_id"]),
                    raw_arguments,
                    confirm_destructive=bool(operation.get("destructive")),
                    idempotency_key=idempotency_key,
                    caller=actor,
                    invocation_context=context,
                )
        except Exception as exc:
            self.executions.finish_execution(execution["execution_id"], status="failed", error=type(exc).__name__)
            raise
        result = self._materialize_declared_artifacts(
            result, entry=entry, actor=actor, project_id=project_id, context=context,
        )
        return self._finish(
            execution["execution_id"], result,
            output_schema=entry["definition"].get("output_schema"),
        )

    def _finish(
        self, execution_id: str, result: Any, *, output_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(output_schema, dict) and output_schema:
            try:
                Draft202012Validator.check_schema(output_schema)
                validator = Draft202012Validator(output_schema, format_checker=FormatChecker())
                errors = sorted(validator.iter_errors(result), key=lambda error: (list(error.absolute_path), error.message))
                # Contract mappings historically wrap the provider payload in
                # an ``output`` envelope with provenance. Accept that generic
                # host envelope when its payload satisfies the declared
                # operation result schema.
                if errors and isinstance(result, dict) and isinstance(result.get("output"), (dict, list)):
                    nested_errors = sorted(
                        validator.iter_errors(result["output"]),
                        key=lambda error: (list(error.absolute_path), error.message),
                    )
                    if not nested_errors:
                        errors = []
            except SchemaError as exc:
                errors = []
                contract_error = f"Declared output contract is invalid: {str(exc.message)[:240]}"
            else:
                contract_error = ""
            if contract_error or errors:
                if errors:
                    error = errors[0]
                    path = ".".join(str(part) for part in error.absolute_path)
                    contract_error = f"result{'.' + path if path else ''} is invalid: {error.message}"
                return self.executions.finish_execution(
                    execution_id, status="failed", result=result, error=contract_error,
                )
        # A transport can return HTTP/MCP success after accepting work while
        # the provider's own result declares that the operation failed.  Keep
        # the raw result for diagnostics, but make the durable generic
        # execution terminally failed so every agent surface observes the same
        # truth and an idempotent replay cannot start the operation again.
        from app.services.integration_async import is_terminal_async

        failure = terminal_operation_failure(result)
        if isinstance(result, dict) and isinstance(result.get("async_operation"), dict) and not is_terminal_async(result):
            return self.executions.finish_execution(
                execution_id, status="running", result=result, error="",
            )
        return self.executions.finish_execution(
            execution_id,
            status="failed" if failure is not None else "succeeded",
            result=result,
            error=failure.message if failure is not None else "",
        )

    def get_execution(self, actor: Any, *, project_id: str, execution_id: str) -> dict[str, Any]:
        value = self.executions.get_execution(execution_id)
        prepared = self.executions.get_prepared(value["prepared_invocation_id"])
        if prepared["project_id"] != project_id:
            raise PlatformOperationError("Integration execution belongs to another project.")
        self.describe(actor, project_id=project_id, operation_id=value["operation_id"])
        return value

    def cancel_execution(self, actor: Any, *, project_id: str, execution_id: str) -> dict[str, Any]:
        value = self.get_execution(actor, project_id=project_id, execution_id=execution_id)
        return self.executions.cancel_execution(value["execution_id"])


def build_integration_operation_catalog(
    store: Any, tenant_id: str, contributions: Any, *, native_runtime: Any | None = None,
) -> IntegrationOperationCatalog:
    # Imports stay local because the integration-management service imports
    # the contribution host during application bootstrap.
    from app.services.integration_connections import ConnectionProfileService
    from app.services.integration_management import IntegrationManagementService
    from app.services.integration_registry import IntegrationRegistry

    registry = IntegrationRegistry(store, tenant_id=tenant_id)
    return IntegrationOperationCatalog(IntegrationManagementService(
        registry,
        ConnectionProfileService(store, tenant_id=tenant_id, registry=registry),
        contributions,
    ), native_runtime=native_runtime)


def build_tenant_operation_catalog(tenant_id: str) -> IntegrationOperationCatalog:
    """Build the tenant's catalogue from the live host composition."""
    from app.services.integration_host import connections as host_connections, contributions as host_contributions, integrations as host_integrations, store as host_store
    from app.services.integration_connections import ConnectionProfileService
    from app.services.integration_contributions import ContributionRegistry
    from app.services.integration_management import IntegrationManagementService
    from app.services.integration_registry import IntegrationRegistry
    from app.services.tenant_schema import DEFAULT_TENANT_ID

    tenant = str(tenant_id or DEFAULT_TENANT_ID)
    if tenant == host_integrations.tenant_id:
        management = IntegrationManagementService(host_integrations, host_connections, host_contributions)
    else:
        registry = IntegrationRegistry(host_store, tenant_id=tenant)
        connections = ConnectionProfileService(host_store, tenant_id=tenant, registry=registry)
        contributions = ContributionRegistry(registry, connections=connections)
        management = IntegrationManagementService(registry, connections, contributions)
    return IntegrationOperationCatalog(management)


def register_generic_integration_tools(runtime: Any, *, tenant_id: str, contributions: Any) -> list[str]:
    """Register the same generic operation surface for any tool runtime.

    The Operator, MCP gateway, and future internal transports call this
    function instead of defining provider/category tools of their own.
    """
    catalog = build_integration_operation_catalog(
        runtime.store, tenant_id, contributions, native_runtime=runtime,
    )

    def project(arguments: dict[str, Any]) -> str:
        value = str(arguments.get("project_id") or "").strip()
        if not value:
            raise PlatformValidationError("Integration operations require a project_id.")
        return value

    definitions = [
        CoreToolDefinition(
            name="lynewright.integrations.search_operations", capability="system:read",
            handler=lambda actor, arguments: {"operations": catalog.search(
                actor, project_id=project(arguments), query=str(arguments.get("query") or ""),
            )},
            description="Search eligible integration operations by declared metadata and schema.",
            read_only=True, risk_class="READ_ONLY",
            input_schema={"type": "object", "properties": {"project_id": {"type": "string"}, "query": {"type": "string"}}, "required": ["project_id"], "additionalProperties": False},
        ),
        CoreToolDefinition(
            name="lynewright.integrations.describe_operation", capability="system:read",
            handler=lambda actor, arguments: catalog.describe(
                actor, project_id=project(arguments), operation_id=str(arguments.get("operation_id") or ""),
            ),
            description="Describe one eligible integration operation and its current revision.",
            read_only=True, risk_class="READ_ONLY",
            input_schema={"type": "object", "properties": {"project_id": {"type": "string"}, "operation_id": {"type": "string"}}, "required": ["project_id", "operation_id"], "additionalProperties": False},
        ),
        CoreToolDefinition(
            name="lynewright.integrations.search_resources", capability="system:read",
            handler=lambda actor, arguments: catalog.search_resources(
                actor, project_id=project(arguments), query=str(arguments.get("query") or ""),
                resource_type=str(arguments.get("resource_type") or ""),
                integration_id=str(arguments.get("integration_id") or ""),
                operation_id=str(arguments.get("operation_id") or ""),
                limit=int(arguments.get("limit") or 20),
                auto_discover=bool(arguments.get("auto_discover")),
            ),
            description=(
                "Search the tenant/project-scoped external-resource inventory for any configured integration "
                "(accounts, pages, workspaces, channels, or future resource types). Results contain safe IDs "
                "and display metadata only; use this before supplying required resource fields."
            ),
            read_only=True, risk_class="READ_ONLY",
            input_schema={"type": "object", "properties": {
                "project_id": {"type": "string"}, "query": {"type": "string", "maxLength": 200},
                "resource_type": {"type": "string", "maxLength": 120},
                "integration_id": {"type": "string", "maxLength": 256},
                "operation_id": {"type": "string", "maxLength": 256},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "auto_discover": {"type": "boolean"},
            }, "required": ["project_id"], "additionalProperties": False},
        ),
        CoreToolDefinition(
            name="lynewright.integrations.resolve_requirements", capability="system:read",
            handler=lambda actor, arguments: catalog.resolve_requirements(
                actor, project_id=project(arguments),
                operation_id=str(arguments.get("operation_id") or ""),
                arguments=arguments.get("arguments") if isinstance(arguments.get("arguments"), dict) else {},
                auto_discover=bool(arguments.get("auto_discover", True)),
            ),
            description=(
                "Preflight a generic integration operation: resolve schema-required external resource IDs from "
                "the shared inventory, return an unambiguous arguments patch, and report explicit missing or "
                "ambiguous requirements before approval. Never invent identifiers."
            ),
            read_only=True, risk_class="READ_ONLY",
            input_schema={"type": "object", "properties": {
                "project_id": {"type": "string"}, "operation_id": {"type": "string"},
                "arguments": {"type": "object"}, "auto_discover": {"type": "boolean"},
            }, "required": ["project_id", "operation_id"], "additionalProperties": False},
        ),
        CoreToolDefinition(
            name="lynewright.integrations.prepare_invocation", capability="mcp:client:use",
            handler=lambda actor, arguments: catalog.prepare(
                actor, project_id=project(arguments), operation_id=str(arguments.get("operation_id") or ""), arguments=arguments.get("arguments") if isinstance(arguments.get("arguments"), dict) else None,
            ),
            description="Validate and canonicalize an integration operation before approval or execution.",
            read_only=True, risk_class="SAFE_OPERATION",
            input_schema={"type": "object", "properties": {"project_id": {"type": "string"}, "operation_id": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["project_id", "operation_id", "arguments"], "additionalProperties": False},
        ),
        CoreToolDefinition(
            name="lynewright.integrations.invoke", capability="mcp:client:use",
            handler=lambda actor, arguments: catalog.invoke(
                actor, project_id=project(arguments), operation_id=str(arguments.get("operation_id") or ""),
                arguments={**(arguments.get("arguments") if isinstance(arguments.get("arguments"), dict) else {}), "project_id": project(arguments), "idempotency_key": str(arguments.get("idempotency_key") or "")},
                prepared_invocation_id=str(arguments.get("prepared_invocation_id") or "") or None,
            ),
            description="Invoke any eligible mapped integration operation through its configured transport.",
            risk_class="PUBLICATION",
            input_schema={"type": "object", "properties": {"project_id": {"type": "string"}, "operation_id": {"type": "string"}, "arguments": {"type": "object"}, "prepared_invocation_id": {"type": "string"}, "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128}}, "required": ["project_id", "operation_id"], "additionalProperties": False},
        ),
        CoreToolDefinition(
            name="lynewright.integrations.get_execution", capability="system:read",
            handler=lambda actor, arguments: catalog.get_execution(
                actor, project_id=project(arguments), execution_id=str(arguments.get("execution_id") or ""),
            ),
            description="Read the durable state and sanitized result of a generic integration execution.",
            read_only=True, risk_class="READ_ONLY",
            input_schema={"type": "object", "properties": {"project_id": {"type": "string"}, "execution_id": {"type": "string"}}, "required": ["project_id", "execution_id"], "additionalProperties": False},
        ),
        CoreToolDefinition(
            name="lynewright.integrations.cancel_execution", capability="mcp:client:use",
            handler=lambda actor, arguments: catalog.cancel_execution(
                actor, project_id=project(arguments), execution_id=str(arguments.get("execution_id") or ""),
            ),
            description="Request cancellation of a running generic integration execution when supported.",
            destructive=True, risk_class="DESTRUCTIVE",
            input_schema={"type": "object", "properties": {"project_id": {"type": "string"}, "execution_id": {"type": "string"}}, "required": ["project_id", "execution_id"], "additionalProperties": False},
        ),
    ]
    existing = {item.name for item in runtime.definitions()}
    registered: list[str] = []
    for definition in definitions:
        if definition.name in existing:
            continue
        runtime.register_tool(definition)
        registered.append(definition.name)
    return registered


__all__ = [
    "IntegrationOperationCatalog",
    "IntegrationOperationDescriptor",
    "build_integration_operation_catalog",
    "build_tenant_operation_catalog",
    "register_generic_integration_tools",
]
