from __future__ import annotations

import pytest

from app.services.extension_manifest import (
    ExtensionManifestValidationError,
    manifest_capabilities,
    validate_extension_manifest,
    version_satisfies,
)


def valid_service_manifest() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "id": "lynewright.social.mock",
        "name": "Mock Social Provider",
        "version": "0.1.0",
        "type": "service",
        "lynewright_version": "^0.1.0",
        "sdk_version": "^1.0.0",
        "requested_capabilities": ["projects:read", "jobs:create", "network:use"],
        "service": {"kind": "http", "health_check": "/health"},
        "dependencies": [{"id": "lynewright.media.core", "version": ">=1.0.0 <2.0.0"}],
        "contributions": ["services", "jobs", "health"],
        "network": {"allow": ["Postiz.EXAMPLE.com."]},
        "settings_schema": "schemas/settings.json",
        "backend": "dist/backend.mjs",
        "ui": "dist/ui.mjs",
    }


def test_valid_service_manifest_is_normalized_and_does_not_grant_capabilities() -> None:
    manifest = validate_extension_manifest(valid_service_manifest(), lynewright_version="0.1.0")

    assert manifest.id == "lynewright.social.mock"
    assert manifest.network.allow == ["postiz.example.com"]
    assert manifest_capabilities(manifest) == {"projects:read", "jobs:create", "network:use"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "Lynewright Social"),
        ("id", "lynewright..social"),
        ("version", "release-1"),
        ("schema_version", "2.0"),
        ("type", "remote"),
    ],
)
def test_invalid_identity_and_schema_fields_are_rejected(field: str, value: str) -> None:
    manifest = valid_service_manifest()
    manifest[field] = value

    with pytest.raises(ExtensionManifestValidationError):
        validate_extension_manifest(manifest, lynewright_version="0.1.0")


def test_incompatible_lynewright_and_sdk_versions_are_rejected() -> None:
    manifest = valid_service_manifest()
    manifest["lynewright_version"] = ">=2.0.0 <3.0.0"
    with pytest.raises(ExtensionManifestValidationError, match="incompatible with Lynewright"):
        validate_extension_manifest(manifest, lynewright_version="0.1.0")

    manifest = valid_service_manifest()
    manifest["sdk_version"] = ">=2.0.0 <3.0.0"
    with pytest.raises(ExtensionManifestValidationError, match="incompatible with Extension SDK"):
        validate_extension_manifest(manifest, lynewright_version="0.1.0")


def test_service_metadata_network_policy_and_entrypoints_are_validated() -> None:
    manifest = valid_service_manifest()
    manifest.pop("service")
    with pytest.raises(ExtensionManifestValidationError, match="require service metadata"):
        validate_extension_manifest(manifest, lynewright_version="0.1.0")

    manifest = valid_service_manifest()
    manifest["network"] = {"allow": ["https://postiz.example.com"]}
    with pytest.raises(ExtensionManifestValidationError, match="Network allowlist"):
        validate_extension_manifest(manifest, lynewright_version="0.1.0")

    manifest = valid_service_manifest()
    manifest["backend"] = "../backend.mjs"
    with pytest.raises(ExtensionManifestValidationError, match="Invalid backend entrypoint"):
        validate_extension_manifest(manifest, lynewright_version="0.1.0")


def test_unknown_capabilities_and_invalid_dependencies_are_rejected() -> None:
    manifest = valid_service_manifest()
    manifest["requested_capabilities"] = ["projects:read", "core-db:read"]
    with pytest.raises(ExtensionManifestValidationError, match="unknown or reserved"):
        validate_extension_manifest(manifest, lynewright_version="0.1.0")

    manifest = valid_service_manifest()
    manifest["dependencies"] = [{"id": "lynewright.social.mock", "version": "^0.1.0"}]
    with pytest.raises(ExtensionManifestValidationError, match="cannot include the extension itself"):
        validate_extension_manifest(manifest, lynewright_version="0.1.0")


def test_semver_ranges_support_exact_comparators_and_caret_ranges() -> None:
    assert version_satisfies("1.2.3", "1.2.3")
    assert version_satisfies("1.4.0", "^1.2.3")
    assert not version_satisfies("2.0.0", "^1.2.3")
    assert version_satisfies("0.2.4", "^0.2.3")
    assert not version_satisfies("0.3.0", "^0.2.3")
    assert version_satisfies("1.2.3", ">=1.0.0 <2.0.0")
