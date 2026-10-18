"""Validated Extension Manifest v1 definitions.

The manifest is descriptive only. It does not load extension code, grant a
capability, or register a contribution; those operations are deliberately
deferred to later extension-host milestones.
"""
from __future__ import annotations

import re
import json
from collections.abc import Iterable
from typing import Any, Literal
from urllib.parse import urlsplit

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.platform_capabilities import PUBLIC_CAPABILITIES
from app.services.platform_services import PlatformValidationError


MANIFEST_SCHEMA_VERSION = "1.0"
# 1.1 adds `provides`, which lets the host compile a runtime definition from the
# manifest instead of a per-provider builder in Core. 1.0 manifests stay valid
# and simply compile to nothing, so existing installations are unaffected.
MANIFEST_SCHEMA_VERSIONS = ("1.0", "1.1", "1.2")
_DOTTED_ENTRYPOINT_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")
EXTENSION_SDK_VERSION = "1.0.0"

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_EXTENSION_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ExtensionManifestValidationError(PlatformValidationError):
    """An extension manifest is malformed or incompatible with this host."""


def _parse_semver(value: str) -> tuple[int, int, int, tuple[str | int, ...] | None]:
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        raise ValueError("Expected a semantic version such as 1.2.3.")
    prerelease = match.group(4)
    identifiers: tuple[str | int, ...] | None = None
    if prerelease:
        parsed: list[str | int] = []
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                if len(identifier) > 1 and identifier.startswith("0"):
                    raise ValueError("Semantic-version numeric prerelease identifiers cannot have leading zeroes.")
                parsed.append(int(identifier))
            else:
                parsed.append(identifier)
        identifiers = tuple(parsed)
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), identifiers


def _compare_semver(left: str, right: str) -> int:
    left_core = _parse_semver(left)
    right_core = _parse_semver(right)
    for left_part, right_part in zip(left_core[:3], right_core[:3]):
        if left_part != right_part:
            return 1 if left_part > right_part else -1
    left_pre, right_pre = left_core[3], right_core[3]
    if left_pre is None and right_pre is None:
        return 0
    if left_pre is None:
        return 1
    if right_pre is None:
        return -1
    for left_identifier, right_identifier in zip(left_pre, right_pre):
        if left_identifier == right_identifier:
            continue
        if isinstance(left_identifier, int) and isinstance(right_identifier, str):
            return -1
        if isinstance(left_identifier, str) and isinstance(right_identifier, int):
            return 1
        return 1 if left_identifier > right_identifier else -1
    if len(left_pre) == len(right_pre):
        return 0
    return 1 if len(left_pre) > len(right_pre) else -1


def semver_specifier_set(value: str) -> SpecifierSet:
    """Translate the manifest's SemVer ranges to PEP 440 specifiers.

    Exact SemVer versions and caret ranges retain their existing meaning;
    comma- or whitespace-separated comparisons use packaging's specifier
    implementation so native package compatibility follows one parser.
    """
    tokens = str(value or "").replace(",", " ").split()
    if not tokens:
        raise ValueError("A compatibility range is required.")
    specifiers: list[str] = []
    for token in tokens:
        match = re.fullmatch(r"(\^|>=|<=|>|<|==|!=|=)?(.+)", token)
        if match is None:
            raise ValueError("Invalid semantic-version range.")
        operator, expected = match.group(1) or "=", match.group(2)
        major, minor, patch, _prerelease = _parse_semver(expected)
        if operator == "^":
            upper = (
                f"{major + 1}.0.0" if major
                else f"0.{minor + 1}.0" if minor
                else f"0.0.{patch + 1}"
            )
            specifiers.extend((f">={expected}", f"<{upper}"))
        elif operator == "=":
            specifiers.append(f"=={expected}")
        else:
            specifiers.append(f"{operator}{expected}")
    return SpecifierSet(",".join(specifiers))


def _validate_version_range(value: str) -> str:
    """Validate the supported SemVer range syntax using packaging."""
    semver_specifier_set(value)
    return value


def version_satisfies(version: str, version_range: str) -> bool:
    """Return whether a semantic version satisfies the manifest range."""
    _parse_semver(version)
    return Version(version) in semver_specifier_set(version_range)


def _validate_relative_entrypoint(value: str) -> str:
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise ValueError("Entrypoints must be non-empty relative paths.")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("Entrypoints cannot contain empty, current-directory, or parent-directory segments.")
    return normalized


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExtensionServiceManifest(_ManifestModel):
    kind: Literal["http"]
    health_check: str = Field(pattern=r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,1023}$")


class ExtensionDependencyManifest(_ManifestModel):
    id: str = Field(min_length=3, max_length=160)
    version: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_values(self) -> "ExtensionDependencyManifest":
        if _EXTENSION_ID_RE.fullmatch(self.id) is None:
            raise ValueError("Dependency id must be a lowercase dotted or hyphenated extension id.")
        _validate_version_range(self.version)
        return self


class ExtensionNetworkPolicy(_ManifestModel):
    allow: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_allowlist(self) -> "ExtensionNetworkPolicy":
        normalized = [host.rstrip(".").lower() for host in self.allow]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Network allowlist entries must be unique.")
        if any(_HOSTNAME_RE.fullmatch(host) is None for host in normalized):
            raise ValueError("Network allowlist entries must be hostnames without a scheme, path, or port.")
        self.allow = normalized
        return self


class ExtensionManagementResource(_ManifestModel):
    """Descriptive metadata for the generic integration management plane.

    This model deliberately contains no entrypoints or executable references.
    Provider behavior is still resolved only through ``provides`` and the
    allowlisted runtime compiler; this section only tells the UI which generic
    controls are meaningful for a provider.
    """

    type: str = Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_.-]*$")
    label: str = Field(min_length=1, max_length=120)
    plural_label: str = Field(min_length=1, max_length=120)
    parent_type: str | None = Field(default=None, max_length=120)
    discoverable: bool = True
    bindable: bool = True
    operations: list[str] = Field(default_factory=list, max_length=20)
    display_fields: list[str] = Field(default_factory=list, max_length=20)
    discovery_contract: str | None = Field(default=None, max_length=160)
    discovery_operation: str | None = Field(default=None, max_length=120)
    operation_contract: str | None = Field(default=None, max_length=160)
    operation_names: dict[str, str] = Field(default_factory=dict, max_length=20)
    fields: list["ExtensionManagementResourceField"] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_values(self) -> "ExtensionManagementResource":
        if self.parent_type is not None and re.fullmatch(r"^[a-z][a-z0-9_.-]*$", self.parent_type) is None:
            raise ValueError("Management resource parent_type is invalid.")
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("Management resource operations must be unique.")
        if any(re.fullmatch(r"^[a-z][a-z0-9_.-]*$", operation) is None for operation in self.operations):
            raise ValueError("Management resource operations must be bounded identifiers.")
        if len(set(self.display_fields)) != len(self.display_fields):
            raise ValueError("Management resource display_fields must be unique.")
        if any(re.fullmatch(r"^[a-z][a-z0-9_.-]*$", field) is None for field in self.display_fields):
            raise ValueError("Management resource display_fields must be bounded identifiers.")
        if self.discovery_contract is not None and not self.discovery_contract.startswith("lynewright."):
            raise ValueError("Management discovery contracts must use the lynewright namespace.")
        if self.discovery_operation is not None and re.fullmatch(r"^[a-z][a-z0-9_.-]*$", self.discovery_operation) is None:
            raise ValueError("Management discovery operation is invalid.")
        if self.discoverable and (self.discovery_contract is None or self.discovery_operation is None):
            raise ValueError("Discoverable management resources require a discovery contract and operation.")
        if self.operation_contract is not None and not self.operation_contract.startswith("lynewright."):
            raise ValueError("Management operation contracts must use the lynewright namespace.")
        if any(re.fullmatch(r"^[a-z][a-z0-9_.-]*$", key) is None for key in self.operation_names):
            raise ValueError("Management operation names must use bounded action identifiers.")
        if any(re.fullmatch(r"^[a-z][a-z0-9_.-]*$", value) is None for value in self.operation_names.values()):
            raise ValueError("Management operation handlers must use bounded identifiers.")
        if any(key not in self.operations for key in self.operation_names):
            raise ValueError("Management operation mappings must reference declared operations.")
        field_keys = [field.key for field in self.fields]
        if len(set(field_keys)) != len(field_keys):
            raise ValueError("Management resource fields must be unique.")
        return self


class ExtensionManagementResourceField(_ManifestModel):
    """A bounded, non-executable field used by a generic resource form."""

    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    label: str = Field(min_length=1, max_length=120)
    type: Literal["text", "number", "boolean", "enum", "list"] = "text"
    description: str | None = Field(default=None, max_length=500)
    required: bool = False
    default: str | int | float | bool | list[str] | None = None
    options: list[str] = Field(default_factory=list, max_length=50)
    min: float | None = None
    max: float | None = None
    max_items: int | None = Field(default=None, ge=1, le=50)
    normalization: Literal["trim", "hostname"] = "trim"

    @model_validator(mode="after")
    def validate_values(self) -> "ExtensionManagementResourceField":
        if self.type == "enum" and not self.options:
            raise ValueError("Enum resource fields require options.")
        if self.type != "enum" and self.options:
            raise ValueError("Only enum resource fields may declare options.")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("Resource field minimum cannot exceed maximum.")
        if self.normalization == "hostname" and self.type != "text":
            raise ValueError("Hostname normalization is only valid for text fields.")
        return self


class ExtensionOAuthManagement(_ManifestModel):
    """Declarative OAuth endpoints and credential references for the host."""

    authorization_url: str = Field(min_length=1, max_length=2_000)
    token_url: str = Field(min_length=1, max_length=2_000)
    client_id_ref: str = Field(min_length=1, max_length=256)
    client_secret_ref: str = Field(min_length=1, max_length=256)
    scopes: list[str] = Field(default_factory=list, max_length=50)
    use_pkce: bool = True

    @model_validator(mode="after")
    def validate_values(self) -> "ExtensionOAuthManagement":
        for field_name in ("authorization_url", "token_url"):
            parsed = urlsplit(str(getattr(self, field_name)))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError(f"OAuth {field_name} must be an absolute HTTP(S) URL without embedded credentials.")
        if any(not isinstance(scope, str) or not 1 <= len(scope) <= 160 for scope in self.scopes):
            raise ValueError("OAuth scopes must be bounded strings.")
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("OAuth scopes must be unique.")
        return self


class ExtensionManagementManifest(_ManifestModel):
    """Provider-neutral sections and remote resource declarations."""

    sections: list[str] = Field(default_factory=list, max_length=20)
    resources: list[ExtensionManagementResource] = Field(default_factory=list, max_length=100)
    supports_oauth: bool = False
    oauth: ExtensionOAuthManagement | None = None
    supports_webhooks: bool = False
    supports_health: bool = True
    supports_diagnostics: bool = True
    supports_settings: bool = True

    @model_validator(mode="after")
    def validate_values(self) -> "ExtensionManagementManifest":
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("Management sections must be unique.")
        if any(re.fullmatch(r"^[a-z][a-z0-9_.-]*$", section) is None for section in self.sections):
            raise ValueError("Management sections must be bounded identifiers.")
        resource_types = [item.type for item in self.resources]
        if len(set(resource_types)) != len(resource_types):
            raise ValueError("Management resource types must be unique.")
        if self.supports_oauth != (self.oauth is not None):
            raise ValueError("Management OAuth support must declare exactly one OAuth configuration.")
        return self


class ExtensionAsyncOperationManifest(_ManifestModel):
    """Declarative polling contract for an operation that outlives a call."""

    poll_tool: str = Field(min_length=3, max_length=200)
    task_id_field: str = Field(default="id", min_length=1, max_length=80)
    poll_argument: str | None = Field(default=None, max_length=80)
    state_field: str = Field(default="state", min_length=1, max_length=80)
    active_states: list[str] = Field(default_factory=lambda: [
        "accepted", "queued", "running", "processing", "pending", "in_progress",
    ], max_length=30)
    terminal_states: list[str] = Field(default_factory=lambda: [
        "completed", "succeeded", "failed", "cancelled", "canceled", "rejected",
    ], max_length=30)

    @model_validator(mode="after")
    def validate_async_operation(self) -> "ExtensionAsyncOperationManifest":
        if len(set(self.active_states)) != len(self.active_states):
            raise ValueError("Async active states must be unique.")
        if len(set(self.terminal_states)) != len(self.terminal_states):
            raise ValueError("Async terminal states must be unique.")
        if set(self.active_states) & set(self.terminal_states):
            raise ValueError("Async active and terminal states must not overlap.")
        return self


class ExtensionModelSlotManifest(_ManifestModel):
    """A model an integration needs the operator to pick.

    The integration declares the slot; Core discovers it from the active
    integrations and AI provider settings shows one picker per slot, so a new
    integration gets its own model pickers with no Core or frontend change.
    """

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    kind: Literal["text", "image", "vision"]
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)


class ExtensionProvidesManifest(_ManifestModel):
    """One Core contract this integration implements, and the class behind it.

    `implementation` is a dotted `module:attribute` reference the host resolves
    at activation. It is deliberately not a file path: the loader imports it
    from an allowlisted package root, so a manifest names *which* shipped
    provider to use and can never introduce new code.
    """

    contract: str = Field(min_length=3, max_length=160)
    implementation: str = Field(min_length=3, max_length=512)
    version: str = Field(default="1.0.0", min_length=1, max_length=80)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    # How the host calls the provider. "direct" is invoke(operation, payload).
    # "sdk_scoped" is invoke(sdk, operation, payload), where the host builds a
    # request-scoped SDK and passes it in. The manifest chooses between two
    # host-defined shapes; it cannot describe the injection itself, so it can
    # never ask for an arbitrary dependency.
    invocation: Literal["direct", "sdk_scoped"] = "direct"
    async_operations: dict[str, ExtensionAsyncOperationManifest] = Field(default_factory=dict, max_length=50)
    operation_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=50)
    model_slots: list[ExtensionModelSlotManifest] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_provides(self) -> "ExtensionProvidesManifest":
        if _DOTTED_ENTRYPOINT_RE.fullmatch(self.implementation) is None:
            raise ValueError(
                "Implementation must be a dotted module:attribute reference, "
                "such as app.integrations.<package>.provider:<Class>."
            )
        _parse_semver(self.version)
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Provided capability ids must be unique.")
        if set(self.operation_schemas) - set(self.capabilities):
            raise ValueError("Operation schemas must reference declared capability ids.")
        try:
            if len(json.dumps(self.operation_schemas, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
                raise ValueError("Provided operation schemas exceed the 64 KiB limit.")
        except (TypeError, ValueError) as exc:
            raise ValueError("Provided operation schemas must be JSON serializable and bounded.") from exc
        slot_ids = [slot.id for slot in self.model_slots]
        if len(set(slot_ids)) != len(slot_ids):
            raise ValueError("Model slot ids must be unique within one provided contract.")
        return self


class ExtensionManifest(_ManifestModel):
    schema_version: Literal["1.0", "1.1", "1.2"] = MANIFEST_SCHEMA_VERSION
    id: str = Field(min_length=3, max_length=160)
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=80)
    type: Literal["native", "service"]
    lynewright_version: str = Field(min_length=1, max_length=120)
    sdk_version: str = Field(min_length=1, max_length=120)
    requested_capabilities: list[str] = Field(default_factory=list, max_length=100)
    credential_refs: list[str] = Field(default_factory=list, max_length=50)
    service: ExtensionServiceManifest | None = None
    dependencies: list[ExtensionDependencyManifest] = Field(default_factory=list, max_length=100)
    contributions: list[Literal["services", "jobs", "events", "mcp", "settings", "health", "ui"]] = Field(default_factory=list, max_length=20)
    provides: list[ExtensionProvidesManifest] = Field(default_factory=list, max_length=50)
    network: ExtensionNetworkPolicy = Field(default_factory=ExtensionNetworkPolicy)
    management: ExtensionManagementManifest = Field(default_factory=ExtensionManagementManifest)
    settings_schema: dict[str, Any] | str | None = Field(default=None, max_length=512)
    backend: str | None = Field(default=None, max_length=512)
    ui: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_manifest(self) -> "ExtensionManifest":
        if _EXTENSION_ID_RE.fullmatch(self.id) is None:
            raise ValueError("Extension id must be a lowercase dotted or hyphenated identifier.")
        _parse_semver(self.version)
        _validate_version_range(self.lynewright_version)
        _validate_version_range(self.sdk_version)
        unknown_capabilities = set(self.requested_capabilities) - PUBLIC_CAPABILITIES
        if unknown_capabilities:
            raise ValueError(f"Manifest requests unknown or reserved capabilities: {', '.join(sorted(unknown_capabilities))}.")
        if len(set(self.requested_capabilities)) != len(self.requested_capabilities):
            raise ValueError("Requested capabilities must be unique.")
        if len(set(self.credential_refs)) != len(self.credential_refs) or any(
            re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", item) is None for item in self.credential_refs
        ):
            raise ValueError("Manifest credential refs must be unique bounded reference names.")
        if self.type == "service" and self.service is None:
            raise ValueError("Service extensions require service metadata.")
        if self.type == "native" and self.service is not None:
            raise ValueError("Native extensions cannot declare service metadata.")
        if self.type == "service" and self.backend is None:
            raise ValueError("Service extensions require a backend adapter entrypoint.")
        for label, entrypoint in (("settings schema", self.settings_schema), ("backend", self.backend), ("UI", self.ui)):
            if entrypoint is not None:
                if label == "settings schema" and isinstance(entrypoint, dict):
                    continue
                try:
                    setattr(self, {"settings schema": "settings_schema", "backend": "backend", "UI": "ui"}[label], _validate_relative_entrypoint(entrypoint))
                except ValueError as exc:
                    raise ValueError(f"Invalid {label} entrypoint: {exc}") from exc
        dependency_ids = [dependency.id for dependency in self.dependencies]
        if self.id in dependency_ids or len(set(dependency_ids)) != len(dependency_ids):
            raise ValueError("Dependencies must be unique and cannot include the extension itself.")
        if len(set(self.contributions)) != len(self.contributions):
            raise ValueError("Contribution types must be unique.")
        return self


def validate_extension_manifest(
    value: ExtensionManifest | dict[str, object],
    *,
    lynewright_version: str,
    sdk_version: str = EXTENSION_SDK_VERSION,
) -> ExtensionManifest:
    """Validate a v1 manifest and its compatibility with the running host."""
    try:
        manifest = value if isinstance(value, ExtensionManifest) else ExtensionManifest.model_validate(value)
        _parse_semver(lynewright_version)
        _parse_semver(sdk_version)
        if not version_satisfies(lynewright_version, manifest.lynewright_version):
            raise ExtensionManifestValidationError(
                f"Extension {manifest.id} is incompatible with Lynewright {lynewright_version}."
            )
        if not version_satisfies(sdk_version, manifest.sdk_version):
            raise ExtensionManifestValidationError(
                f"Extension {manifest.id} is incompatible with Extension SDK {sdk_version}."
            )
        return manifest
    except ExtensionManifestValidationError:
        raise
    except Exception as exc:
        raise ExtensionManifestValidationError(f"Invalid extension manifest: {exc}") from exc


def manifest_capabilities(manifest: ExtensionManifest) -> frozenset[str]:
    """Return the declared public capabilities without treating them as grants."""
    return frozenset(manifest.requested_capabilities)


def manifest_dependencies(manifest: ExtensionManifest) -> Iterable[ExtensionDependencyManifest]:
    """Expose immutable-by-convention dependency metadata to the future registry."""
    return tuple(manifest.dependencies)


def manifest_declares_runtime(manifest: ExtensionManifest | dict[str, Any]) -> bool:
    """Return whether a manifest declares executable host work.

    ``ui`` is deliberately not considered executable. A UI-only feature must
    use the registry's durable enable/disable transition rather than inventing
    an empty lifecycle definition that startup would later try to activate.
    """
    if isinstance(manifest, ExtensionManifest):
        return bool(manifest.provides or manifest.contributions)
    return bool(manifest.get("provides")) or bool(manifest.get("contributions"))
