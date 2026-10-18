"""Compile a runtime definition from an integration's own manifest.

Lynewright had two halves that nothing generic connected. An installed integration
carried a manifest and configuration; an executable runtime definition carried a
provider factory, handlers, and registrations. Bridging them was per-provider
code in Core, so every new provider meant editing Core -- which is the shape
that ends with `if integration == "<some-provider>"` and no plugin
architecture left.

This compiles the bridge instead. A manifest declares which Core contracts it
provides and which shipped class implements each one; the compiler validates
that, resolves the implementation, and produces an `IntegrationRuntimeDefinition`
whose activation registers the service contributions. Adding a provider becomes
a package plus a manifest, with no Core edit.

**The package allowlist is a native-code trust boundary.** Importing an
allowlisted package's manifest executes trusted package code; discovery does not
load the provider implementation. At compile time, `implementation` is a dotted
`module:attribute` reference resolved only under that package's root. A manifest
cannot point the compiler at code outside its selected distribution.
"""
from __future__ import annotations

from collections.abc import Iterable

import importlib
import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any

from app.services.extension_manifest import ExtensionManifest, validate_extension_manifest
from app.services.integration_lifecycle import (
    IntegrationActivationContext,
    IntegrationRuntimeDefinition,
)
from app.services.platform_services import PlatformValidationError


# Only shipped package roots may be named by a manifest. `app.services` is
# deliberately absent: allowlisting it would let a manifest name any Core
# service, which is the arbitrary-execution surface this exists to prevent.
DEFAULT_ALLOWED_ROOTS: tuple[str, ...] = ("app.integrations", "app.features")

# What a compile attempt concluded, kept separate from activation state so an
# operator can tell "this integration cannot be compiled" from "this integration
# failed to start".
COMPILED = "compiled"
INCOMPLETE = "incomplete"
INVALID_IMPLEMENTATION = "invalid_implementation"
NOT_EXECUTABLE = "not_executable"
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompileResult:
    """Why an integration did or did not produce a runtime definition."""

    integration_id: str
    outcome: str
    definition: IntegrationRuntimeDefinition | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.definition is not None


def resolve_implementation(reference: str, *, allowed_roots: tuple[str, ...] = DEFAULT_ALLOWED_ROOTS) -> Any:
    """Import `module:attribute`, refusing anything outside the allowlist.

    The root check happens *before* the import, because importing a module to
    find out whether it was allowed has already run its top-level code.
    """
    module_path, _, attribute = str(reference or "").partition(":")
    if not module_path or not attribute:
        raise PlatformValidationError("Implementation must be a dotted module:attribute reference.")
    if not any(module_path == root or module_path.startswith(f"{root}.") for root in allowed_roots):
        raise PlatformValidationError(
            f"Implementation {module_path!r} is outside the allowed integration roots."
        )
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        raise PlatformValidationError(f"Implementation module could not be imported: {type(exc).__name__}.") from exc
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise PlatformValidationError(f"Implementation {attribute!r} is not defined in {module_path!r}.") from exc


def requires_no_constructor_arguments(implementation: Any) -> bool:
    """Whether the entrypoint can be built with no arguments.

    Checked by signature rather than by calling it, so validation has no side
    effects, and deliberately *not* used to work out what to inject. A manifest
    names a class; it never describes how to construct one. Anything needing
    configuration wraps it inside its own integration package, where
    provider-specific construction belongs.
    """
    try:
        signature = inspect.signature(implementation)
    except (TypeError, ValueError):
        # Builtins and C-implemented callables expose no signature. Treat that
        # as unverifiable rather than acceptable.
        return False
    return all(
        parameter.default is not inspect.Parameter.empty
        or parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        for parameter in signature.parameters.values()
    )



def _declare_capabilities(
    integration_id: str, contract: str, capabilities: "Iterable[str]", integrations: Any | None = None,
) -> None:
    """Record what this manifest says it provides, as declared evidence.

    Declared, not verified: the manifest is a claim about what exists, and
    only a live check can promote it. Failing to declare is never fatal to an
    activation -- the invocation path refuses on its own if evidence is
    missing, and taking down an otherwise working activation over a state-tracking
    write would trade something real for something recoverable.
    """
    from app.services.integration_capability_evidence import (
        IntegrationCapabilityEvidenceService,
    )
    try:
        if integrations is None:
            from app.services.integration_registry import IntegrationRegistry
            integrations = IntegrationRegistry()
        evidence = IntegrationCapabilityEvidenceService(integrations)
        for capability in capabilities:
            evidence.declare(
                integration_id=integration_id, contract=contract, capability=capability)
    except Exception:  # noqa: BLE001 - declaration must not fail an activation
        return


class IntegrationDefinitionCompiler:
    """Builds runtime definitions from manifests, without naming any provider."""

    def __init__(
        self, *, allowed_roots: tuple[str, ...] = DEFAULT_ALLOWED_ROOTS,
        lynewright_version: str = "0.1.0", sdk_version: str = "1.0.0",
        integrations: Any | None = None,
    ) -> None:
        self.allowed_roots = tuple(allowed_roots)
        self.lynewright_version = lynewright_version
        self.sdk_version = sdk_version
        self.integrations = integrations

    def compile(
        self, record: dict[str, Any], *, allowed_roots: tuple[str, ...] | None = None,
    ) -> CompileResult:
        """Compile one installed integration's record into a definition.

        Never raises. A manifest that cannot compile is a diagnostic, not a
        startup failure -- one malformed integration must not stop the others.
        """
        integration_id = str(record.get("id") or "")
        try:
            manifest = self._manifest(record)
        except Exception as exc:
            return CompileResult(integration_id, NOT_EXECUTABLE, reason=f"manifest_invalid:{type(exc).__name__}")

        if not manifest.provides:
            # A 1.0 manifest, or a placeholder that declares nothing. Either way
            # there is nothing to compile, and saying so is more useful than
            # reporting a runtime definition as "unavailable after restart".
            return CompileResult(integration_id, NOT_EXECUTABLE, reason="manifest_declares_no_provides")

        package = str(record.get("source") or "") == "package"
        allow_in_process = os.getenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", "").strip() == "1"
        if package and not allow_in_process:
            # The API registers only manifest metadata and a fail-closed stub.
            # The implementation is resolved by lynewright-runner, never imported
            # into the API process for a default container execution.
            return CompileResult(
                integration_id, COMPILED,
                definition=self._container_definition(manifest),
            )

        implementations: list[tuple[Any, Any]] = []
        for provided in manifest.provides:
            try:
                implementation = resolve_implementation(
                    provided.implementation,
                    allowed_roots=tuple(allowed_roots) if allowed_roots is not None else self.allowed_roots,
                )
            except PlatformValidationError as exc:
                return CompileResult(integration_id, INCOMPLETE, reason=str(exc))
            if not callable(implementation):
                return CompileResult(
                    integration_id, INCOMPLETE,
                    reason=f"implementation for {provided.contract!r} is not constructible",
                )
            if not requires_no_constructor_arguments(implementation):
                return CompileResult(
                    integration_id, INVALID_IMPLEMENTATION,
                    reason=(
                        f"implementation for {provided.contract!r} is not no-argument "
                        "constructible; wrap it in an entrypoint inside the integration package"
                    ),
                )
            implementations.append((provided, implementation))

        return CompileResult(
            integration_id,
            COMPILED,
            definition=self._definition(
                manifest, implementations,
                execution_plane_override=(
                    ("direct" if os.getenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", "").strip() == "1" else "container")
                    if str(record.get("source") or "") == "package" else None
                ),
                integration_source=str(record.get("source") or ""),
            ),
            reason="",
        )

    def _manifest(self, record: dict[str, Any]) -> ExtensionManifest:
        manifest = record.get("manifest")
        if isinstance(manifest, ExtensionManifest):
            return manifest
        if not isinstance(manifest, dict):
            raise PlatformValidationError("Integration record carries no manifest.")
        return validate_extension_manifest(
            manifest,
            lynewright_version=self.lynewright_version,
            sdk_version=self.sdk_version,
        )

    def _definition(
        self, manifest: ExtensionManifest, implementations: list[tuple[Any, Any]],
        *, execution_plane_override: str | None = None, integration_source: str = "",
    ) -> IntegrationRuntimeDefinition:
        """Assemble the definition whose activation registers each contract."""

        instances: list[Any] = []

        def activate(context: IntegrationActivationContext) -> None:
            instances.clear()
            for provided, implementation in implementations:
                # The provider is constructed per activation generation, so a
                # reload cannot leave a previous generation's instance bound.
                instance = implementation()
                instances.append(instance)
                configure_instance = getattr(instance, "configure", None)
                if callable(configure_instance):
                    configure_instance(dict(context.configuration))
                invoke = getattr(instance, "invoke", None)
                if not callable(invoke):
                    raise PlatformValidationError(
                        f"Provider for {provided.contract!r} does not expose invoke()."
                    )
                # Providers may optionally implement the host-defined
                # ``invoke_with_lifecycle`` protocol.  The callback is the
                # provider's normal invoke method; the hook can coordinate a
                # provider-owned runtime (for example, an on-demand external
                # service) without Core importing, naming, or configuring that
                # provider.  Providers that do not need lifecycle work keep the
                # exact historical direct path.
                lifecycle = getattr(instance, "invoke_with_lifecycle", None)
                if callable(lifecycle):
                    def handler(*args: Any, _invoke=invoke, _lifecycle=lifecycle, **kwargs: Any) -> Any:
                        return _lifecycle(
                            lambda: _invoke(*args, **kwargs),
                            *args,
                            **kwargs,
                        )
                else:
                    handler = invoke
                execution_timeout = getattr(provided, "execution_timeout_seconds", None)
                if execution_timeout is None and callable(lifecycle):
                    # A trusted provider may carry a code-level default when
                    # its startup handshake is intrinsic to the adapter.  The
                    # optional attribute is read only from the already
                    # allowlisted provider instance, so adding that default
                    # cannot silently change an activated manifest identity.
                    execution_timeout = getattr(instance, "execution_timeout_seconds", None)
                execution_plane = execution_plane_override or getattr(instance, "execution_plane", None)
                if execution_plane is not None:
                    execution_plane = str(execution_plane).strip().lower()
                    if execution_plane not in {"direct", "worker", "container"}:
                        raise PlatformValidationError(
                            f"Provider for {provided.contract!r} declares an invalid execution plane."
                        )
                if integration_source == "package" and execution_plane_override == "direct":
                    _LOG.warning(
                        "Native integration package %s is running in-process by explicit opt-in; package code is trusted.",
                        manifest.id,
                    )
                async_operations = dict(provided.async_operations)
                if not async_operations:
                    candidate_async = getattr(instance, "async_operations", {})
                    if isinstance(candidate_async, dict):
                        async_operations = candidate_async
                operation_schemas = getattr(instance, "operation_schemas", {})
                if not isinstance(operation_schemas, dict):
                    operation_schemas = {}
                # The SDK is deliberately not built here. It depends on tenant,
                # project, actor, policy, and trace, none of which exist at
                # activation; a handler holding one would serve every later
                # request with the authority of whoever caused the activation.
                # The mode travels in metadata so the request path can build a
                # scoped SDK and pass it in.
                context.contribute(
                    local_id=f"{provided.contract}.provider",
                    kind="service",
                    metadata={
                        "contract": provided.contract,
                        "version": provided.version,
                        "capabilities": list(provided.capabilities),
                        "invocation": provided.invocation,
                        **({"async_operations": {
                            str(name): (
                                value.model_dump(mode="json")
                                if hasattr(value, "model_dump") else dict(value)
                            )
                            for name, value in async_operations.items()
                            if isinstance(value, dict) or hasattr(value, "model_dump")
                        }} if async_operations else {}),
                        **({"execution_plane": execution_plane}
                           if execution_plane is not None else {}),
                        **({"execution_timeout_seconds": execution_timeout}
                           if execution_timeout is not None else {}),
                        **({"operation_schemas": operation_schemas} if operation_schemas else {}),
                        **({
                            "model_slots": [slot.model_dump(mode="json") for slot in provided.model_slots],
                            "integration_name": manifest.name,
                        } if provided.model_slots else {}),
                    },
                    handler=handler,
                )
                # A manifest declaring a capability is the declaration. Without
                # this the contract registry refuses every invocation with
                # "provider capability has not been declared", because evidence
                # rows only ever appeared where a provider happened to write
                # one itself -- so a purely manifest-compiled provider could be
                # registered, activated, and never usable.
                _declare_capabilities(
                    manifest.id, provided.contract, provided.capabilities, self.integrations)

        has_configurer = any(callable(getattr(implementation, "configure", None))
                             for _provided, implementation in implementations)

        def configure(context: IntegrationActivationContext, configuration: dict[str, Any]) -> None:
            for instance in tuple(instances):
                callback = getattr(instance, "configure", None)
                if callable(callback):
                    callback(dict(configuration))
            context.configuration = dict(configuration)

        return IntegrationRuntimeDefinition(
            integration_id=manifest.id,
            definition_version=manifest.version,
            contract_versions={item.contract: item.version for item, _ in implementations},
            activate=activate,
            configure=configure if has_configurer else None,
        )

    def _container_definition(self, manifest: ExtensionManifest) -> IntegrationRuntimeDefinition:
        """Register package metadata without importing package implementation code."""
        from app.services.integration_lifecycle import IntegrationRuntimeDefinition

        def activate(context: Any) -> None:
            for provided in manifest.provides:
                schemas = dict(provided.operation_schemas)
                for operation in provided.capabilities:
                    schemas.setdefault(operation, {
                        "type": "object", "properties": {}, "additionalProperties": True,
                        "description": f"{operation} operation provided by {manifest.name}.",
                    })
                async_operations = {
                    str(name): value.model_dump(mode="json")
                    for name, value in provided.async_operations.items()
                }

                def not_local(*_args: Any, **_kwargs: Any) -> Any:
                    raise PlatformValidationError("Container integration code cannot execute in the API process.")

                context.contribute(
                    local_id=f"{provided.contract}.provider",
                    kind="service",
                    metadata={
                        "contract": provided.contract,
                        "version": provided.version,
                        "capabilities": list(provided.capabilities),
                        "invocation": provided.invocation,
                        "execution_plane": "container",
                        "operation_schemas": schemas,
                        **({"async_operations": async_operations} if async_operations else {}),
                        **({"model_slots": [slot.model_dump(mode="json") for slot in provided.model_slots],
                            "integration_name": manifest.name} if provided.model_slots else {}),
                    },
                    handler=not_local,
                )
                _declare_capabilities(manifest.id, provided.contract, provided.capabilities, self.integrations)

        return IntegrationRuntimeDefinition(
            integration_id=manifest.id,
            definition_version=manifest.version,
            contract_versions={item.contract: item.version for item in manifest.provides},
            activate=activate,
        )


__all__ = [
    "COMPILED",
    "DEFAULT_ALLOWED_ROOTS",
    "INCOMPLETE",
    "INVALID_IMPLEMENTATION",
    "NOT_EXECUTABLE",
    "CompileResult",
    "IntegrationDefinitionCompiler",
    "requires_no_constructor_arguments",
    "resolve_implementation",
]
