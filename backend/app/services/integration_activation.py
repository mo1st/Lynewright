"""The one definition of "activate integrations for this process".

Contributions live in process memory. Any process that serves integration
operations must therefore run this sequence itself -- the API process doing it
does not populate the MCP process. This module exists so the entrypoints
cannot answer that question differently: each imports this, none owns a
private copy of the order or the error handling.

The sequence is deliberately install -> inject -> attach -> activate. A
runtime definition cannot be registered for an integration the registry has
never heard of, and a managed source must be re-attached from its durable
configuration before anything tries to enable it.

``recover_enabled`` is the one thing processes must not agree on, and it is
the reason this module takes a flag rather than assuming. ``recover_enabled()``
walks *every* enabled integration and writes a durable ``status="error"`` for
any whose definition is missing from the calling process. That is correct in
the API, which is the composition root and builds every definition. Run in a
secondary process that legitimately builds only some of them, it would mark
the others failed in the shared database and clobber the API's healthy state.
A secondary process therefore activates only what it registered itself.
"""
from __future__ import annotations

from typing import Any

from app.services.application_logging import log_event
from app.services.extension_manifest import version_satisfies


def activate_integrations(
    *, source: str, recover_enabled: bool = False,
) -> dict[str, Any]:
    """Populate this process's contribution registry from durable state.

    Idempotent: a settled process re-running this registers nothing new. Each
    step is isolated so one unavailable integration cannot stop the others
    from recovering, which is why the report is returned rather than raised --
    a partially recovered process is still worth serving, and the caller
    decides how loudly to say so.

    ``source`` names the calling entrypoint so logs distinguish which process
    recovered what. ``recover_enabled`` must be true only in the composition
    root; see the module docstring for why.
    """
    from app.routers import integration_gateway
    from app.services.production_integrations import ensure_discovered_integrations_installed

    report: dict[str, Any] = {}

    # Discovered by location rather than by name, idempotent, always left
    # disabled. The installation record must exist before anything can
    # register a runtime definition against it, and whichever process starts
    # first creates it while the others find it already present.
    installed = ensure_discovered_integrations_installed(
        integration_gateway.runtime.integrations
    )
    report["installed"] = installed
    if any(state == "installed" for state in installed.values()):
        log_event(
            level="info", category="integration", source=source,
            message="Installed configured integration packages.",
            details={"integrations": installed},
        )

    # The composition root owns transport construction; the gateway never
    # imports a provider or reads the environment itself. An unconfigured
    # provider contributes no factory, so this is empty on a deployment that
    # does not render.
    runtime_bootstrap = bootstrap_installed_runtime_definitions(
        integration_gateway.runtime.integrations,
        runtime=integration_gateway.runtime,
    )
    report["runtime_definitions"] = runtime_bootstrap
    if runtime_bootstrap["failed"]:
        log_event(
            level="warning", category="integration", source=source,
            message="Some injected integration runtime definitions could not be registered.",
            details={"failed": runtime_bootstrap["failed"]},
        )

    if recover_enabled:
        upgrades = _reconcile_compatible_deployed_definitions(
            integration_gateway.runtime,
            runtime_bootstrap.get("registered", []),
            source=source,
        )
        report["definition_upgrades"] = upgrades
        recovery = integration_gateway.recover_enabled_integrations()
        report["recovery"] = recovery
        if recovery["unavailable"] or recovery["failed"]:
            log_event(
                level="warning", category="integration", source=source,
                message="Some enabled integrations could not be restored during startup.",
                details={
                    "unavailable": recovery["unavailable"],
                    "failed": recovery["failed"],
                },
            )
        return report

    report["activated"] = _activate_registered_only(
        integration_gateway.runtime,
        sorted(runtime_bootstrap.get("registered", [])),
        source=source,
    )
    return report


def bootstrap_installed_runtime_definitions(
    registry: Any,
    *,
    runtime: Any | None = None,
    integration_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Compile installed manifest data and register definitions in this runtime.

    Startup and live installs share this path. Package compilation reads the
    stored ``lynewright-manifest.json`` and returns container definitions without
    importing package modules unless the explicit in-process opt-in is set.
    Re-registering an unchanged definition is idempotent.
    """
    from app.services.production_integrations import compile_installed_definitions

    if runtime is None:
        from app.routers import integration_gateway
        target_runtime = integration_gateway.runtime
    else:
        target_runtime = runtime
    compiled = compile_installed_definitions(registry)
    selected = set(str(item) for item in integration_ids) if integration_ids is not None else None
    outcome: dict[str, Any] = {
        "registered": [], "already_registered": [], "failed": [],
        "diagnostics": dict(compiled.get("diagnostics") or {}),
    }
    seen: set[str] = set()
    for definition in compiled.get("definitions") or []:
        integration_id = str(definition.integration_id)
        if selected is not None and integration_id not in selected:
            continue
        seen.add(integration_id)
        try:
            identity = target_runtime.identities.identity_for(
                integration_id,
                definition_version=definition.definition_version,
                manifest_fingerprint=definition.manifest_fingerprint,
                contract_versions=definition.contract_versions,
            )
            if any(
                target_runtime.definition_identity(existing) == identity
                for existing in target_runtime.definitions_for(integration_id)
            ):
                outcome["already_registered"].append(integration_id)
                continue
            target_runtime.register_definition(definition)
            outcome["registered"].append(integration_id)
        except Exception as exc:  # one unavailable integration cannot block others
            outcome["failed"].append(f"{integration_id}:{type(exc).__name__}")
    if selected is not None:
        for integration_id in sorted(selected - seen):
            reason = str(outcome["diagnostics"].get(integration_id) or "runtime_definition_not_compiled")
            outcome["failed"].append(f"{integration_id}:{reason}")
    return outcome


def _reconcile_compatible_deployed_definitions(
    runtime: Any, integration_ids: list[str], *, source: str,
) -> dict[str, list[dict[str, Any]]]:
    """Auto-reconcile newer definitions only when capability parity is known."""
    from app.services.integration_definition_upgrades import IntegrationDefinitionUpgradeService

    service = IntegrationDefinitionUpgradeService(runtime)
    outcome: dict[str, list[dict[str, Any]]] = {"auto_upgraded": [], "available": []}
    for integration_id in sorted(set(str(item) for item in integration_ids)):
        try:
            current = runtime.identities.active(integration_id)
            if current is None:
                continue
            integration = runtime.integrations.get(integration_id)
            manifest = integration.get("manifest") or {}
            contracts = {
                str(item["contract"]): str(item["version"])
                for item in manifest.get("provides", [])
                if isinstance(item, dict) and item.get("contract") and item.get("version")
            }
            candidate_identity = None
            candidate = None
            for registered in runtime.definitions_for(integration_id):
                identity = runtime.definition_identity(registered)
                if identity != current and identity.definition_version == str(manifest.get("version") or integration.get("version") or "") and identity.contract_versions == contracts:
                    candidate_identity, candidate = identity, registered
                    break
            if candidate_identity is None:
                candidate_identity = runtime.identities.identity_for(
                    integration_id,
                    definition_version=str(manifest.get("version") or integration.get("version") or ""),
                    contract_versions=contracts,
                )
            if current == candidate_identity:
                continue
            if candidate is None:
                candidate = runtime.definition_for_identity(candidate_identity)
            if candidate is None:
                continue

            current_version = current.definition_version
            candidate_version = candidate_identity.definition_version
            same_version_fingerprint_change = (
                candidate_version == current_version
                and candidate_identity.manifest_fingerprint != current.manifest_fingerprint
            )
            is_newer_version = version_satisfies(candidate_version, f">{current_version}")
            if not is_newer_version and not same_version_fingerprint_change:
                continue

            incompatible_contracts = {
                contract: {
                    "from": version,
                    "to": candidate_identity.contract_versions.get(contract),
                }
                for contract, version in current.contract_versions.items()
                if (
                    candidate_identity.contract_versions.get(contract) is None
                    or not version_satisfies(
                        str(candidate_identity.contract_versions.get(contract) or ""),
                        f"^{version}",
                    )
                )
            }
            if incompatible_contracts:
                capability_change = service._deployed_capability_change(integration_id, current)
                item = {
                    "integration_id": integration_id,
                    "from_version": current_version,
                    "to_version": candidate_version,
                    "capability_change": capability_change,
                    "same_version_fingerprint_changed": same_version_fingerprint_change,
                    "incompatible_contracts": incompatible_contracts,
                }
                outcome["available"].append(item)
                log_event(
                    level="error", category="integration", source=source,
                    message="A deployed integration definition has an incompatible contract version and needs review.",
                    details=item,
                )
                continue

            preview = service.deployed_reconcile_preview(integration_id, candidate)

            capability_change = preview.get("capability_change") or {}
            item = {
                "integration_id": integration_id,
                "from_version": current_version,
                "to_version": candidate_version,
                "capability_change": capability_change,
                "same_version_fingerprint_changed": same_version_fingerprint_change,
            }
            added = capability_change.get("added") or {}
            removed = capability_change.get("removed") or {}
            if capability_change.get("known") is True and not removed:
                result = service.reconcile_to_deployed(integration_id, candidate)
                outcome["auto_upgraded"].append(item)
                log_event(
                    level="info", category="integration", source=source,
                    message="definition.auto_upgraded",
                    details={
                        "event": "definition.auto_upgraded",
                        **item,
                        "added": added,
                        "mode": result.get("mode"),
                    },
                )
            else:
                outcome["available"].append(item)
                log_event(
                    level="error", category="integration", source=source,
                    message=(
                        "A same-version integration definition fingerprint changed and "
                        "needs capability review before activation."
                        if same_version_fingerprint_change
                        else "A newer integration definition needs review before activation."
                    ),
                    details=item,
                )
        except Exception as exc:  # noqa: BLE001 - one integration must not stop startup
            log_event(
                level="warning", category="integration", source=source,
                message="A deployed integration definition could not be reconciled automatically.",
                details={"integration_id": integration_id}, exc=exc,
            )
    return outcome


def _activate_registered_only(
    runtime: Any, integration_ids: list[str], *, source: str,
) -> dict[str, list[str]]:
    """Activate only the definitions this process actually registered.

    Deliberately silent about integrations owned by another process: their
    absence here is expected, not a fault, and recording it as one is the
    exact damage this path exists to avoid.
    """
    outcome: dict[str, list[str]] = {"activated": [], "skipped": [], "failed": []}
    for integration_id in integration_ids:
        try:
            if runtime.is_active(integration_id):
                continue
            record = runtime.integrations.get(integration_id)
            if not record.get("enabled"):
                # Installed but switched off. The definition stays registered
                # so a later enable can activate it without a restart.
                outcome["skipped"].append(integration_id)
                continue
            runtime.activate(integration_id)
            outcome["activated"].append(integration_id)
        except Exception as exc:  # noqa: BLE001 - one bad provider is not fatal
            outcome["failed"].append(f"{integration_id}:{type(exc).__name__}")
    if outcome["failed"]:
        log_event(
            level="warning", category="integration", source=source,
            message="Some integration definitions did not activate in this process.",
            details={"failed": outcome["failed"][:20], "activated": outcome["activated"][:20]},
        )
    return outcome


__all__ = ["activate_integrations", "bootstrap_installed_runtime_definitions"]
