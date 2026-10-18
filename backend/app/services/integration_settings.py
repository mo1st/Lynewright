"""Bounded, provider-neutral settings schemas for the integration surface."""
from __future__ import annotations

import importlib
import os
import re
from typing import Any

from app.services.extension_manifest import ExtensionManifest
from app.services.platform_services import PlatformValidationError


_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_TYPES = frozenset({"text", "number", "boolean", "enum", "credential_ref", "list"})
_RUNTIME_TARGETS = frozenset({"config"})
_MAX_FIELDS = 50


class IntegrationSettingsService:
    """Load inline schemas or trusted bundled settings data.

    Package manifests carry settings schemas inline. A legacy module reference
    from a selected package can only be resolved when the operator has enabled
    trusted in-process package execution; normal discovery and settings reads
    never import package code.
    """

    @classmethod
    def model(cls, integration: dict[str, Any]) -> dict[str, Any] | None:
        schema = cls.schema(integration)
        if schema is None:
            return None
        stored = (integration.get("configuration") or {}).get("settings")
        values = stored if isinstance(stored, dict) else {}
        visible: dict[str, Any] = {}
        for field in schema["fields"]:
            key = field["key"]
            value = values.get(key, field.get("default"))
            if field["type"] == "credential_ref":
                # A reference name is safe to show and lets the UI select a
                # stored credential; the secret value never enters this model.
                visible[key] = str(value) if value else ""
            elif value is not None:
                visible[key] = value
        return {
            "schema_id": schema["schema_id"],
            "fields": schema["fields"],
            "values": visible,
            # The registry timestamp is deliberately metadata only.  It lets
            # every declarative settings UI say when its live configuration
            # changed without ever exposing a secret or deployment file.
            "updated_at": integration.get("updated_at"),
        }

    @classmethod
    def schema(cls, integration: dict[str, Any]) -> dict[str, Any] | None:
        """Return the validated packaged schema for runtime configuration.

        This is the same schema used by the management API.  A provider never
        imports an integration-specific settings module directly, which keeps
        runtime configuration declarative for shipped and future integrations.
        """
        manifest = integration.get("manifest")
        reference = manifest.get("settings_schema") if isinstance(manifest, dict) else None
        if reference:
            if isinstance(reference, dict):
                return cls._validate_schema(reference)
            return cls._load_schema(str(reference), integration)
        configuration = integration.get("configuration")
        manual = configuration.get("manual") if isinstance(configuration, dict) else None
        inline = manual.get("settings_schema") if isinstance(manual, dict) else None
        return cls._validate_schema(inline) if inline is not None else None

    @classmethod
    def update(cls, integration: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        schema = cls.schema(integration)
        if schema is None:
            raise PlatformValidationError("This integration does not declare operator settings.")
        if not isinstance(values, dict) or len(values) > _MAX_FIELDS:
            raise PlatformValidationError("Integration settings must be a bounded object.")
        fields = {field["key"]: field for field in schema["fields"]}
        unknown = set(values) - set(fields)
        if unknown:
            raise PlatformValidationError("Integration settings contain an undeclared field.")
        current = (integration.get("configuration") or {}).get("settings")
        candidate = dict(current) if isinstance(current, dict) else {}
        for key, value in values.items():
            field = fields[key]
            candidate[key] = cls._validate_value(field, value)
        for field in schema["fields"]:
            if cls._is_required(field, candidate) and cls._is_missing(candidate.get(field["key"], field.get("default"))):
                raise PlatformValidationError(f"Required integration setting is missing: {field['key']}.")
        return candidate

    @classmethod
    def _load_schema(cls, reference: str, integration: dict[str, Any] | None = None) -> dict[str, Any]:
        if ":" not in reference:
            raise PlatformValidationError("Integration settings schema must be a packaged entry point.")
        module_name, attribute = reference.split(":", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", attribute) or any(part in {"", ".", ".."} for part in module_name.split(".")):
            raise PlatformValidationError("Integration settings schema entry point is invalid.")
        source = str((integration or {}).get("source") or "bundled")
        if source == "package":
            if os.getenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", "").strip() != "1":
                raise PlatformValidationError("Package settings must be inline data unless in-process package execution is explicitly enabled.")
            entrypoint = str((integration or {}).get("source_entrypoint") or "")
            package_root = entrypoint.rpartition(".")[0]
            distribution = str((integration or {}).get("source_distribution") or "")
            integration_id = str((integration or {}).get("id") or "")
            from app.services.production_integrations import package_manifest_source_available
            if not package_root or not (module_name == package_root or module_name.startswith(package_root + ".")) or not package_manifest_source_available(integration_id, distribution, entrypoint):
                raise PlatformValidationError("Integration package settings are unavailable unless its distribution is selected and discovered.")
        elif not module_name.startswith("app.integrations."):
            raise PlatformValidationError("Bundled integration settings must be under app.integrations.")
        try:
            value = getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError) as exc:
            raise PlatformValidationError("Integration settings schema could not be loaded.") from exc
        if not isinstance(value, dict):
            raise PlatformValidationError("Integration settings schema must be a data object.")
        return cls._validate_schema(value)

    @staticmethod
    def _validate_schema(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PlatformValidationError("Integration settings schema must be a data object.")
        schema_id = str(value.get("schema_id") or "")
        fields = value.get("fields")
        if not _KEY.fullmatch(schema_id) or not isinstance(fields, list) or not 1 <= len(fields) <= _MAX_FIELDS:
            raise PlatformValidationError("Integration settings schema is invalid.")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in fields:
            if not isinstance(raw, dict):
                raise PlatformValidationError("Integration settings fields must be objects.")
            key = str(raw.get("key") or "")
            kind = str(raw.get("type") or "")
            label = str(raw.get("label") or key)
            if not _KEY.fullmatch(key) or key in seen or kind not in _TYPES or not 1 <= len(label) <= 120:
                raise PlatformValidationError("Integration settings field declaration is invalid.")
            description = str(raw.get("description") or "")
            if "<" in description or ">" in description:
                raise PlatformValidationError("Integration settings descriptions cannot contain HTML.")
            item = {"key": key, "label": label, "type": kind, "description": description}
            for optional in ("required", "default", "options", "min", "max", "max_items", "credential_kind", "required_when"):
                if optional in raw:
                    item[optional] = raw[optional]
            if "required_when" in item:
                conditions = item["required_when"]
                if not isinstance(conditions, dict) or not conditions or any(not _KEY.fullmatch(str(key)) or isinstance(value, (dict, list)) for key, value in conditions.items()):
                    raise PlatformValidationError("Integration required_when declaration is invalid.")
            binding = raw.get("runtime_binding")
            if binding is not None:
                if not isinstance(binding, dict) or set(binding) - {"target", "name", "collection"}:
                    raise PlatformValidationError("Integration runtime binding declaration is invalid.")
                target = str(binding.get("target") or "")
                name = str(binding.get("name") or "")
                collection = binding.get("collection", False)
                if target not in _RUNTIME_TARGETS or not _KEY.fullmatch(name) or not isinstance(collection, bool):
                    raise PlatformValidationError("Integration runtime binding declaration is invalid.")
                item["runtime_binding"] = {"target": target, "name": name, "collection": collection}
            if kind == "enum":
                options = item.get("options")
                if not isinstance(options, list) or not 1 <= len(options) <= 50 or any(not isinstance(option, str) or len(option) > 120 for option in options):
                    raise PlatformValidationError("Enum settings require bounded string options.")
            if kind == "list" and (not isinstance(item.get("max_items", 20), int) or not 1 <= item.get("max_items", 20) <= 100):
                raise PlatformValidationError("List settings require a bounded max_items value.")
            normalized.append(item)
            seen.add(key)
        return {"schema_id": schema_id, "fields": normalized}

    @staticmethod
    def _validate_value(field: dict[str, Any], value: Any) -> Any:
        kind = field["type"]
        if kind == "credential_ref":
            if value == "":
                return ""
            if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
                raise PlatformValidationError(f"{field['label']} must be a credential reference.")
            return value
        if kind == "text" and (not isinstance(value, str) or len(value) > 2_000 or "<" in value or ">" in value):
            raise PlatformValidationError(f"{field['label']} must be plain bounded text.")
        if kind == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise PlatformValidationError(f"{field['label']} must be a number.")
        if kind == "boolean" and not isinstance(value, bool):
            raise PlatformValidationError(f"{field['label']} must be true or false.")
        if kind == "enum" and value not in field.get("options", []):
            raise PlatformValidationError(f"{field['label']} has an unsupported option.")
        if kind == "list":
            if not isinstance(value, list) or len(value) > int(field.get("max_items", 20)) or any(not isinstance(item, (str, int, float, bool)) for item in value):
                raise PlatformValidationError(f"{field['label']} must be a bounded list of scalar values.")
        if kind == "number":
            if field.get("min") is not None and value < field["min"] or field.get("max") is not None and value > field["max"]:
                raise PlatformValidationError(f"{field['label']} is outside its allowed range.")
        return value

    @staticmethod
    def _is_required(field: dict[str, Any], values: dict[str, Any]) -> bool:
        if field.get("required"):
            return True
        conditions = field.get("required_when")
        return isinstance(conditions, dict) and all(values.get(key) == value for key, value in conditions.items())

    @staticmethod
    def _is_missing(value: Any) -> bool:
        return value is None or value == "" or value == []


__all__ = ["IntegrationSettingsService"]
