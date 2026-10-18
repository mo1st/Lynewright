"""Import and export for data-only Lynewright integration declarations."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from app.services.integration_management import IntegrationManagementService
from app.services.integration_openapi import IntegrationOpenAPIClient
from app.services.extension_manifest import version_satisfies
from app.services.integration_settings import IntegrationSettingsService
from app.services.integration_install_policy import (
    build_install_summary,
    catalogue_status,
    decide_install,
    log_install_confirmation,
)
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformServiceError,
    PlatformValidationError,
    HTTPService,
)


FORMAT = "lynewright-integration/1"
LYNEWRIGHT_VERSION = "0.1.0"
MAX_FILE_BYTES = 1_000_000
_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "integration-file.schema.json"
_SENSITIVE_KEY = re.compile(
    r"password|token|secret|api[_-]?key|apikey|authorization|bearer|client_secret|private_key",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|authorization)\b[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)([^\s,;\"'<>}]+)"
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while parsing an integration file", node.start_mark,
                f"duplicate mapping key {key!r}", key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class IntegrationFileService:
    """Validates declarative files and delegates creation to the manual path."""

    def __init__(
        self,
        management: IntegrationManagementService,
        *,
        lynewright_version: str | None = None,
        document_fetcher: Any | None = None,
    ) -> None:
        self.management = management
        self.lynewright_version = lynewright_version or str(
            getattr(management.integrations, "lynewright_version", LYNEWRIGHT_VERSION)
        )
        self.document_fetcher = document_fetcher

    @staticmethod
    def parse(raw: str | bytes) -> Any:
        data = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(data) > MAX_FILE_BYTES:
            raise PlatformValidationError("Integration file exceeds the 1 MiB limit.")
        try:
            return yaml.load(data.decode("utf-8-sig"), Loader=_UniqueKeyLoader)
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise PlatformValidationError(f"Integration file could not be parsed: {exc}") from exc

    @staticmethod
    def schema() -> dict[str, Any]:
        try:
            return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("The committed integration-file schema is unavailable.") from exc

    def validate(self, value: Any) -> list[str]:
        errors: list[str] = []
        validator = jsonschema.Draft202012Validator(self.schema(), format_checker=jsonschema.FormatChecker())
        for error in sorted(validator.iter_errors(value), key=lambda item: (list(map(str, item.absolute_path)), item.message)):
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            errors.append(f"{path}: {error.message}")
        errors.extend(self.secret_errors(value))
        if isinstance(value, dict):
            compatibility = value.get("lynewright_version")
            declared_version = value.get("version")
            if isinstance(declared_version, str):
                try:
                    version_satisfies(declared_version, f"={declared_version}")
                except ValueError as exc:
                    errors.append(f"version: invalid semantic version: {exc}")
            if isinstance(compatibility, str):
                try:
                    if not version_satisfies(self.lynewright_version, compatibility):
                        errors.append(
                            f"lynewright_version: range {compatibility!r} does not include Lynewright {self.lynewright_version}; "
                            "credential refs cannot bypass host compatibility."
                        )
                except ValueError as exc:
                    errors.append(f"lynewright_version: invalid semantic-version range: {exc}")
            if not str(value.get("endpoint_url") or "").strip() and not str(value.get("internal_endpoint_ref") or "").strip():
                errors.append("endpoint_url: provide a public endpoint URL or a configured internal_endpoint_ref.")
            errors.extend(self._operation_errors(value))
            credential_names = [
                item.get("name") for item in value.get("credentials", [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ] if isinstance(value.get("credentials", []), list) else []
            if len(credential_names) != len(set(credential_names)):
                errors.append("credentials: reference names must be unique.")
            if value.get("settings_schema") is not None:
                try:
                    IntegrationSettingsService._validate_schema(value["settings_schema"])
                except PlatformValidationError as exc:
                    errors.append(f"settings_schema: {exc.message}")
        return errors

    @classmethod
    def redact_untrusted_text(cls, value: str) -> str:
        """Remove likely literal credentials before untrusted text reaches an AI provider."""
        redacted = str(value)
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", redacted)

    @classmethod
    def secret_errors(cls, value: Any) -> list[str]:
        errors: list[str] = []

        def walk(item: Any, path: tuple[str, ...] = (), sensitive_parent: bool = False) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    text_key = str(key)
                    walk(nested, (*path, text_key), bool(_SENSITIVE_KEY.search(text_key)))
            elif isinstance(item, list):
                for index, nested in enumerate(item):
                    walk(nested, (*path, str(index)), sensitive_parent)
            elif isinstance(item, str):
                location = ".".join(path) or "$"
                if sensitive_parent and item.strip():
                    errors.append(
                        f"{location}: literal values under secret-like keys are forbidden; declare a named credential ref under credentials."
                    )
                if any(pattern.search(item) for pattern in _SECRET_PATTERNS):
                    errors.append(
                        f"{location}: value resembles a key or token; declare a named credential ref under credentials."
                    )
            elif sensitive_parent and item is not None:
                location = ".".join(path) or "$"
                errors.append(
                    f"{location}: literal values under secret-like keys are forbidden; declare a named credential ref under credentials."
                )

        walk(value)
        return errors

    def _operation_errors(self, value: dict[str, Any]) -> list[str]:
        kind = value.get("runtime_kind")
        operations = value.get("operations", [])
        if not isinstance(operations, list):
            return []
        names: list[str] = []
        errors: list[str] = []
        for index, item in enumerate(operations):
            if not isinstance(item, dict):
                continue
            name = item.get("operation_id" if kind == "openapi" else "tool_name")
            if isinstance(name, str):
                names.append(name)
        if len(names) != len(set(names)):
            errors.append("operations: operation identifiers must be unique.")
        async_declaration = value.get("async")
        if async_declaration is not None:
            if kind != "openapi":
                errors.append("async: async polling is currently supported only for OpenAPI integrations.")
            elif isinstance(async_declaration, dict):
                declared = set(names)
                for key in ("operation_id", "status_operation_id"):
                    operation_id = str(async_declaration.get(key) or "")
                    if operation_id not in declared:
                        errors.append(f"async.{key}: must reference an operation declared in operations.")
        if kind == "openapi" and isinstance(value.get("openapi"), dict):
            inline = value["openapi"].get("inline")
            if isinstance(inline, dict):
                try:
                    available = {item.operation_id for item in IntegrationOpenAPIClient(inline, self.management._http_adapter()).operations()}
                except PlatformValidationError as exc:
                    errors.append(f"openapi.inline: {exc.message}")
                else:
                    for index, item in enumerate(operations):
                        if isinstance(item, dict) and item.get("operation_id") not in available:
                            errors.append(f"operations.{index}.operation_id: is not declared by openapi.inline.")
                    if isinstance(async_declaration, dict):
                        for key in ("operation_id", "status_operation_id"):
                            operation_id = str(async_declaration.get(key) or "")
                            if operation_id and operation_id not in available:
                                errors.append(f"async.{key}: is not declared by openapi.inline.")
        if kind == "webhook" and operations:
            errors.append("operations: webhook integrations do not declare callable operations.")
        return errors

    def _load_document(self, value: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if value.get("runtime_kind") != "openapi":
            return None, None
        declaration = value.get("openapi") or {}
        if "inline" in declaration:
            document = declaration["inline"]
            source = None
        else:
            url = declaration["url"]
            try:
                internal_ref = str(value.get("internal_endpoint_ref") or "")
                if internal_ref:
                    response = HTTPService().request(
                        url, method="GET", max_bytes=MAX_FILE_BYTES, timeout_seconds=15,
                        internal_endpoint_ref=internal_ref,
                    )
                    if int(response.get("status_code") or 0) >= 400:
                        raise PlatformOperationError("OpenAPI document fetch failed.")
                elif self.document_fetcher is not None:
                    response = self.document_fetcher(url, max_bytes=MAX_FILE_BYTES, timeout_seconds=15)
                else:
                    response = HTTPService().get(url, max_bytes=MAX_FILE_BYTES, timeout_seconds=15)
                document = self.parse(response["body"])
            except PlatformValidationError:
                raise
            except Exception as exc:
                raise PlatformOperationError("OpenAPI document could not be fetched through the safe HTTP service.") from exc
            source = {"url": url}
        if not isinstance(document, dict):
            raise PlatformValidationError("OpenAPI document must be a YAML or JSON object.")
        try:
            normalized = IntegrationOpenAPIClient(document, self.management._http_adapter()).document
        except PlatformValidationError:
            raise
        return normalized, source

    @staticmethod
    def _explicitly_enabled_operations(value: dict[str, Any], runtime_kind: str) -> list[str]:
        """Return operation names explicitly opted into during file import.

        Missing ``enabled`` is deliberately false for every declarative runtime.
        Keep this decision in one place so OpenAPI operations and MCP tools have
        identical import semantics.
        """
        key = {"openapi": "operation_id", "mcp": "tool_name"}.get(runtime_kind)
        if key is None:
            return []
        operations = value.get("operations", [])
        if not isinstance(operations, list):
            return []
        return [
            str(item[key]) for item in operations
            if isinstance(item, dict) and item.get("enabled") is True
            and isinstance(item.get(key), str) and item[key]
        ]

    def import_value(
        self, value: Any, *, project_id: str | None = None,
        provenance_source: str | None = None,
        provenance_actor: str | None = None,
        confirm: dict[str, Any] | None = None,
        artifact_bytes: bytes,
    ) -> dict[str, Any]:
        errors = self.validate(value)
        if errors:
            raise IntegrationFileValidationError(errors)
        assert isinstance(value, dict)
        # External boundaries pass the exact bytes before parsing. This hash is
        # intentionally distinct from the canonical fingerprint below, which
        # is only an idempotency key after OpenAPI resolution.
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        runtime_kind = str(value["runtime_kind"])
        explicitly_enabled = self._explicitly_enabled_operations(value, runtime_kind)
        approval_message = (
            "0 operations approved; approve them in Integrations."
            if runtime_kind in {"openapi", "mcp"} and not explicitly_enabled else None
        )
        if project_id is not None:
            with self.management.store.connection() as db:
                exists = db.execute(
                    "SELECT 1 FROM tenant_projects WHERE tenant_id=? AND project_id=? AND status='active'",
                    (self.management.tenant_id, project_id),
                ).fetchone()
            if exists is None:
                raise PlatformNotFoundError("Project not found.")
        declaration = copy.deepcopy(value)
        document, openapi_source = self._load_document(declaration)
        if document is not None:
            declared = set(self._explicitly_enabled_operations(value, "openapi"))
            available = {item.operation_id for item in IntegrationOpenAPIClient(document, self.management._http_adapter()).operations()}
            for operation_id in sorted(declared - available):
                raise IntegrationFileValidationError([f"operations: operation_id {operation_id!r} is not declared by the fetched OpenAPI document."])
            declaration["openapi"] = {"inline": document}
        normalized_bytes = json.dumps(declaration, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        fingerprint = hashlib.sha256(normalized_bytes).hexdigest()
        try:
            existing = self.management.integrations.get(str(value["id"]))
        except PlatformNotFoundError:
            existing = None
        if existing is not None:
            current_version = str(existing.get("version") or "")
            if current_version != str(value["version"]):
                raise PlatformConflictError(
                    "This data-only integration version cannot be upgraded through integration_definition_upgrades; "
                    "the existing record was left unchanged."
                )
            manual = (existing.get("configuration") or {}).get("manual") or {}
            metadata = manual.get("declaration") if isinstance(manual, dict) else None
            if isinstance(metadata, dict) and metadata.get("fingerprint") == fingerprint:
                return {
                    "integration": self.management.detail(str(value["id"])), "created": False,
                    "idempotent": True, "approval_message": approval_message,
                }
            raise PlatformConflictError("This integration id and version already exist with different file contents.")

        catalogue_state = catalogue_status(
            self.management.store,
            integration_id=str(value["id"]),
            version=str(value["version"]),
            artifact_kind="file",
            sha256=artifact_sha256,
        )
        summary = build_install_summary(
            value,
            kind="declarative file",
            source=provenance_source or f"file sha256:{artifact_sha256}",
            sha256=artifact_sha256,
            catalogue_status_value=catalogue_state,
        )
        decision = decide_install(
            summary,
            confirm=confirm,
            actor=provenance_actor or self.management.actor_id,
            catalogue_matched=catalogue_state == "matched",
        )

        credential_values = value.get("credentials", [])
        credential_refs = [item["name"] for item in credential_values]
        webhook = value.get("webhook") or {}
        file_metadata = {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in {"openapi", "settings_schema"}
        }
        if openapi_source:
            file_metadata["openapi"] = openapi_source
        file_metadata["fingerprint"] = fingerprint
        projected_configuration: dict[str, Any] = {
            "manual": {
                "runtime": runtime_kind,
                "declaration": file_metadata,
                **({"document": document} if document is not None else {}),
                **({"settings_schema": value["settings_schema"]} if value.get("settings_schema") else {}),
            },
        }
        try:
            projected_bytes = len(json.dumps(projected_configuration, sort_keys=True).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise IntegrationFileValidationError(["$: integration declaration must contain JSON-compatible data."]) from exc
        if projected_bytes > 64 * 1024:
            raise IntegrationFileValidationError([
                "$: persisted integration configuration exceeds the existing 64 KiB registry limit."
            ])
        try:
            result = self.management.create_manual(
                integration_id=str(value["id"]),
                name=str(value["name"]),
                version=str(value["version"]),
                lynewright_version=str(value["lynewright_version"]),
                runtime_kind=runtime_kind,
                endpoint_url=str(value["endpoint_url"]),
                document=document,
                credential_refs=credential_refs,
                verification=webhook.get("verification"),
                event_type=webhook.get("event_type"),
                payload_mapping=webhook.get("payload_mapping", {}),
                declaration_metadata=file_metadata,
                settings_schema=value.get("settings_schema"),
                approved_openapi_operations=explicitly_enabled if runtime_kind == "openapi" else [],
                project_id=project_id,
                internal_endpoint_ref=str(value.get("internal_endpoint_ref") or ""),
                provenance_source=provenance_source or f"file sha256:{artifact_sha256}",
                artifact_sha256=artifact_sha256,
                provenance_actor=provenance_actor or "",
                provenance_confirmed_at=decision.confirmed_at,
            )
        except PlatformServiceError:
            raise
        enabled_mcp_tools = explicitly_enabled if runtime_kind == "mcp" else []
        if enabled_mcp_tools:
            try:
                self._discover_and_approve_imported_mcp(
                    str(value["id"]), enabled_mcp_tools, credential_refs, project_id,
                )
            except Exception:
                try:
                    self.management.uninstall_manual(str(value["id"]))
                except Exception:
                    pass
                raise
        if not decision.catalogue_matched:
            log_install_confirmation(
                integration_id=str(value["id"]),
                actor=decision.confirmed_by or provenance_actor or self.management.actor_id,
                decision=decision,
            )
        return {
            "integration": result, "created": True, "idempotent": False,
            "approval_message": approval_message,
        }

    def _discover_and_approve_imported_mcp(
        self, integration_id: str, enabled_tools: list[str],
        credential_refs: list[str], project_id: str | None,
    ) -> None:
        connections = self.management.connections.list(integration_id, project_id=project_id)
        if not connections:
            raise PlatformOperationError("The imported MCP integration has no primary connection.")
        connection_id = str(connections[0]["id"])
        grants = ["network:use"]
        if credential_refs:
            grants.append("credentials:use")
        self.management.set_authorization(
            integration_id, permission_mode="custom", granted_capabilities=grants,
        )
        sdk = self.management._sdk(
            integration_id, connection_id, allow_disabled_integration_for_preview=True,
        )
        discovered = self.management.mcp.discover(sdk, connection_id=connection_id)
        self.management.connections.configure(
            integration_id, connection_id,
            metadata={"mcp": {
                "protocol_version": str(discovered.get("protocol_version") or ""),
                "tools": [
                    {
                        "name": item.get("name", ""),
                        "description": str(item.get("description") or "")[:500],
                        "input_schema": item.get("input_schema") if isinstance(item.get("input_schema"), dict) else {},
                        "read_only": item.get("read_only") if isinstance(item.get("read_only"), bool) else None,
                        "destructive": item.get("destructive") if isinstance(item.get("destructive"), bool) else None,
                        "safety_known": item.get("safety_known") is True,
                    } for item in discovered.get("tools", []) if isinstance(item, dict)
                ],
            }},
        )
        self.management.approve_mcp_tools(integration_id, connection_id, enabled_tools)

    def import_manual(
        self, payload: dict[str, Any], *, artifact_bytes: bytes | None = None,
        confirm: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "integration"
        if artifact_bytes is None:
            artifact_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
        manual: dict[str, Any] = {
            "format": FORMAT,
            "id": f"lynewright.integration.manual-{slug}-{artifact_digest[:12]}",
            "name": name,
            "version": "1.0.0",
            "lynewright_version": f"^{self.lynewright_version}",
            "runtime_kind": payload["runtime_kind"],
            "endpoint_url": payload["endpoint_url"],
            "credentials": [
                {"name": reference, "description": "Credential reference", "required": False}
                for reference in payload.get("credential_refs", [])
            ],
            "operations": [],
        }
        if payload["runtime_kind"] == "openapi":
            document = payload.get("document")
            if isinstance(document, str):
                document = self.parse(document)
            manual["openapi"] = {"inline": document}
        if payload["runtime_kind"] == "webhook":
            manual["webhook"] = {
                "event_type": payload.get("event_type"),
                "payload_mapping": payload.get("payload_mapping", {}),
                "verification": payload.get("verification"),
            }
        return self.import_value(manual, artifact_bytes=artifact_bytes, confirm=confirm)

    def export_value(self, integration_id: str) -> dict[str, Any]:
        integration = self.management.integrations.get(integration_id)
        configuration = integration.get("configuration") or {}
        manual = configuration.get("manual") if isinstance(configuration, dict) else None
        if not isinstance(manual, dict) or manual.get("runtime") not in {"openapi", "mcp", "webhook"}:
            raise PlatformOperationError("Only data-only integrations can be exported as an integration file.")
        metadata = manual.get("declaration")
        if not isinstance(metadata, dict):
            raise PlatformOperationError("This manual integration predates file declarations and cannot be exported losslessly.")
        result = {key: copy.deepcopy(value) for key, value in metadata.items() if key != "fingerprint"}
        if manual["runtime"] == "openapi":
            openapi_source = result.get("openapi")
            if not isinstance(openapi_source, dict) or "url" not in openapi_source:
                result["openapi"] = {"inline": copy.deepcopy(manual.get("document") or {})}
        settings = manual.get("settings_schema")
        if settings:
            result["settings_schema"] = copy.deepcopy(settings)
        return result


class IntegrationFileValidationError(PlatformValidationError):
    code = "invalid_integration_file"

    def __init__(self, errors: list[str]) -> None:
        super().__init__("Integration file validation failed.")
        self.errors = errors


__all__ = ["IntegrationFileService", "IntegrationFileValidationError"]
