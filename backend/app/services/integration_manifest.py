"""Public generic Integration Manifest v1 API.

The initial host implementation used "extension" as an internal term. The
generic infrastructure plan supersedes that vocabulary: integrations can be
trusted native adapters, external HTTP/MCP services, or webhook-driven
adapters. These aliases preserve the validated manifest implementation while
giving all new code the plan's provider-neutral public API.
"""
from app.services.extension_manifest import (
    EXTENSION_SDK_VERSION as INTEGRATION_SDK_VERSION,
    MANIFEST_SCHEMA_VERSION as INTEGRATION_MANIFEST_SCHEMA_VERSION,
    ExtensionDependencyManifest as IntegrationDependencyManifest,
    ExtensionManifest as IntegrationManifest,
    ExtensionManifestValidationError as IntegrationManifestValidationError,
    ExtensionNetworkPolicy as IntegrationNetworkPolicy,
    ExtensionServiceManifest as IntegrationServiceManifest,
    manifest_capabilities as integration_capabilities,
    manifest_dependencies as integration_dependencies,
    validate_extension_manifest as validate_integration_manifest,
    version_satisfies,
)

__all__ = [
    "INTEGRATION_MANIFEST_SCHEMA_VERSION",
    "INTEGRATION_SDK_VERSION",
    "IntegrationDependencyManifest",
    "IntegrationManifest",
    "IntegrationManifestValidationError",
    "IntegrationNetworkPolicy",
    "IntegrationServiceManifest",
    "integration_capabilities",
    "integration_dependencies",
    "validate_integration_manifest",
    "version_satisfies",
]
