from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from app.services.integration_contributions import ContributionRegistry
from app.services.integration_definition_upgrades import IntegrationDefinitionUpgradeService
from app.services.integration_lifecycle import IntegrationLifecycleRuntime, IntegrationRuntimeDefinition
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformOperationError, PlatformStore, PlatformValidationError


def _manifest(integration_id: str, version: str = "0.1.0") -> dict[str, object]:
    return {
        "schema_version": "1.0", "id": integration_id, "name": "Definition test", "version": version,
        "type": "native", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0", "backend": "dist/backend.mjs",
    }


def _definition(integration_id: str, label: str, *, contract_version: str = "1.0.0", fail: bool = False) -> IntegrationRuntimeDefinition:
    def activate(context: Any) -> None:
        if fail:
            raise RuntimeError("candidate failed")
        context.contribute(local_id="publisher", kind="service", metadata={"contract": "lynewright.social.publisher", "version": "1.0.0"}, handler=lambda _operation, _payload: {"label": label})

    return IntegrationRuntimeDefinition(
        integration_id=integration_id,
        contract_versions={"lynewright.social.publisher": contract_version},
        activate=activate,
        dry_run=lambda _context: {"label": label, "safe": not fail},
    )


def _runtime() -> tuple[sqlite3.Connection, IntegrationRegistry, ContributionRegistry, IntegrationLifecycleRuntime, str]:
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db); integration_id = "lynewright.integration.definition-test"
    registry = IntegrationRegistry(store); registry.register(_manifest(integration_id)); registry.enable(integration_id)
    contributions = ContributionRegistry(registry); runtime = IntegrationLifecycleRuntime(registry, contributions)
    return db, registry, contributions, runtime, integration_id


def _replace_manifest(registry: IntegrationRegistry, db: sqlite3.Connection, integration_id: str, *, version: str, name: str = "Definition test") -> None:
    manifest = registry.get(integration_id)["manifest"]
    manifest["version"] = version; manifest["name"] = name
    db.execute("UPDATE platform_extensions SET version=?,manifest_json=? WHERE tenant_id=? AND id=?", (version, json.dumps(manifest), registry.tenant_id, integration_id)); db.commit()


def test_restart_uses_the_exact_locked_definition_and_rejects_newer_unselected_definition() -> None:
    db, registry, _contributions, runtime, integration_id = _runtime()
    first = _definition(integration_id, "v1"); runtime.register_definition(first); runtime.activate(integration_id)
    locked = runtime.identities.active(integration_id); assert locked is not None and locked.definition_version == "0.1.0"

    _replace_manifest(registry, db, integration_id, version="0.2.0")
    second = _definition(integration_id, "v2"); runtime.register_definition(second)
    assert runtime.active_definition(integration_id) is first

    restarted_contributions = ContributionRegistry(registry); restarted = IntegrationLifecycleRuntime(registry, restarted_contributions)
    restarted.register_definition(second)
    recovered = restarted.recover_enabled()
    assert recovered["unavailable"] == [integration_id]
    assert registry.get(integration_id)["status"] == "error"
    db.close()


def test_restart_with_same_version_restores_once_and_disable_reenable_keeps_lock() -> None:
    db, registry, _contributions, runtime, integration_id = _runtime()
    definition = _definition(integration_id, "v1"); runtime.register_definition(definition); runtime.activate(integration_id)
    before = runtime.identities.active(integration_id)
    runtime.disable(integration_id); registry.enable(integration_id); runtime.activate(integration_id)
    assert runtime.identities.active(integration_id) == before

    restarted_contributions = ContributionRegistry(registry); restarted = IntegrationLifecycleRuntime(registry, restarted_contributions)
    restarted.register_definition(_definition(integration_id, "v1"))
    assert restarted.recover_enabled()["activated"] == [integration_id]
    assert restarted.recover_enabled()["already_active"] == [integration_id]
    db.close()


def test_manifest_fingerprint_mismatch_leaves_enabled_integration_unavailable_after_restart() -> None:
    db, registry, _contributions, runtime, integration_id = _runtime()
    runtime.register_definition(_definition(integration_id, "v1")); runtime.activate(integration_id)
    _replace_manifest(registry, db, integration_id, version="0.1.0", name="Changed manifest")

    restarted = IntegrationLifecycleRuntime(registry, ContributionRegistry(registry))
    restarted.register_definition(_definition(integration_id, "v1"))
    assert restarted.recover_enabled()["unavailable"] == [integration_id]
    db.close()


def test_upgrade_preview_requires_contract_compatibility_and_apply_is_reversible() -> None:
    db, registry, contributions, runtime, integration_id = _runtime()
    original = _definition(integration_id, "v1"); runtime.register_definition(original); runtime.activate(integration_id)
    _replace_manifest(registry, db, integration_id, version="0.2.0")
    upgrades = IntegrationDefinitionUpgradeService(runtime)

    incompatible = _definition(integration_id, "v2", contract_version="2.0.0")
    with pytest.raises(PlatformValidationError, match="incompatible"):
        upgrades.preview(integration_id, incompatible)

    candidate = _definition(integration_id, "v2")
    preview = upgrades.preview(integration_id, candidate, conformance=lambda _definition: {"status": "passed"})
    assert preview.diff["definition_version"] == {"from": "0.1.0", "to": "0.2.0"}
    assert preview.dry_run == {"label": "v2", "safe": True}
    assert upgrades.apply(integration_id, candidate, conformance=lambda _definition: {"status": "passed"})["integration"]["status"] == "active"
    assert contributions.invoke(f"{integration_id}:publisher", "health", {}) == {"label": "v2"}

    _replace_manifest(registry, db, integration_id, version="0.3.0")
    broken = _definition(integration_id, "v3", fail=True)
    with pytest.raises(PlatformOperationError, match="restored"):
        upgrades.apply(integration_id, broken)
    assert runtime.identities.active(integration_id).definition_version == "0.2.0"
    assert contributions.invoke(f"{integration_id}:publisher", "health", {}) == {"label": "v2"}
    db.close()
