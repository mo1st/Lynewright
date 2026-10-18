from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.routers import integration_management as router
from app.services.catalogue import ensure_schema as ensure_catalogue_schema
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_drafts import IntegrationDraftService
from app.services.integration_file import IntegrationFileService
from app.services.integration_install_policy import InstallConfirmationRequired, InstallPolicyForbidden
from app.services.integration_management import IntegrationManagementService
from app.services.integration_operations import IntegrationOperationCatalog
from app.services.integration_provenance import get_provenance
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_runner import IntegrationRunnerService
from app.services.platform_services import HTTPService, PlatformOperationError, PlatformStore
from app.services.platform_capabilities import CapabilityActor

APP_ROOT = Path(__file__).parents[1]
FILE_FIXTURE = APP_ROOT / "tests" / "fixtures" / "integration_files" / "openapi.yaml"
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Install policy fixture", "version": "1.0.0"},
    "paths": {"/health": {"get": {
        "operationId": "health", "responses": {"200": {"description": "ok"}},
    }}},
}


@pytest.fixture(autouse=True)
def _keep_install_logs_in_test_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.application_logging.log_event", lambda **_event: None)
    public = base64.b64encode(b"Ed" + bytes.fromhex("0011223344556677") + b"\x02" * 32).decode("ascii")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", public)
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "0")


def _manager(*, actor: str = "admin-policy-test") -> tuple[IntegrationManagementService, sqlite3.Connection]:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("""CREATE TABLE tenant_projects(
      project_id TEXT,tenant_id TEXT,name TEXT,status TEXT DEFAULT 'active',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(tenant_id,project_id));
      INSERT INTO tenant_projects(project_id,tenant_id,name) VALUES('prj_policy','ten_default','Policy');""")
    store = PlatformStore(lambda: db)
    registry = IntegrationRegistry(store)
    connections = ConnectionProfileService(store, registry=registry, endpoint_validator=lambda _url: {})
    return IntegrationManagementService(
        registry, connections, ContributionRegistry(registry, connections=connections), actor_id=actor,
    ), db


def _file_value() -> tuple[dict[str, Any], bytes]:
    value = IntegrationFileService.parse(FILE_FIXTURE.read_bytes())
    value["id"] = "lynewright.integration.install-policy-fixture"
    value["name"] = "Install policy fixture"
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return value, raw


def _manual_payload() -> tuple[dict[str, Any], bytes]:
    payload = {"name": "Manual policy fixture", "runtime_kind": "openapi",
               "endpoint_url": "https://api.example.test", "document": SPEC,
               "credential_refs": ["vendor_api"]}
    return payload, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("path", ["file", "manual"])
@pytest.mark.parametrize("policy", ["confirm", "catalogue-only", "open"])
def test_file_and_manual_outside_import_policies(path: str, policy: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", policy)
    if policy == "open":
        monkeypatch.setattr("app.services.application_logging.log_event", lambda **_event: None)
    manager, db = _manager()
    if path == "manual":
        manager.connections.credentials.set({"name": "vendor_api", "kind": "token", "secret": "fixture-only-value"})
    importer = IntegrationFileService(manager)
    if path == "file":
        value, raw = _file_value()
        install = lambda confirm=None: importer.import_value(value, artifact_bytes=raw, confirm=confirm)
        integration_id = value["id"]
    else:
        payload, raw = _manual_payload()
        install = lambda confirm=None: importer.import_manual(payload, artifact_bytes=raw, confirm=confirm)
        integration_id = ""
    if policy == "confirm":
        with pytest.raises(InstallConfirmationRequired) as required:
            install()
        summary = required.value.summary
        assert required.value.response()["requires_confirmation"] is True
        assert summary["kind"] == "declarative file"
        assert summary["runs_code"] is False
        assert summary["catalogue_status"] == "not listed"
        if path == "manual":
            assert summary["credential_refs"] == ["vendor_api"]
        integration_id = str(summary["integration_id"])
        created = install({"integration_id": integration_id, "summary_hash": required.value.summary_hash})
        provenance = created["integration"]["provenance"]
        assert provenance["confirmed_by"] == "admin-policy-test"
        assert provenance["confirmed_at"]
    elif policy == "catalogue-only":
        with pytest.raises(InstallPolicyForbidden):
            install()
        assert not manager.integrations.list()
    else:
        created = install()
        integration_id = str((created["integration"].get("integration") or {}).get("id") or created["integration"]["integration"]["id"])
        provenance = get_provenance(manager.store, manager.tenant_id, integration_id)
        assert provenance["kind"] == "outside"
        assert provenance["confirmed_by"] == "admin-policy-test"
        assert provenance["confirmed_at"]
    db.close()


def test_wrong_typed_id_and_stale_summary_hash_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "confirm")
    manager, db = _manager()
    importer = IntegrationFileService(manager)
    value, raw = _file_value()
    with pytest.raises(InstallConfirmationRequired) as initial:
        importer.import_value(value, artifact_bytes=raw)
    with pytest.raises(InstallConfirmationRequired, match="stale") as wrong_id:
        importer.import_value(value, artifact_bytes=raw, confirm={
            "integration_id": "lynewright.integration.wrong", "summary_hash": initial.value.summary_hash,
        })
    assert wrong_id.value.summary_hash == initial.value.summary_hash

    changed = dict(value)
    changed["name"] = "Changed after review"
    changed_raw = json.dumps(changed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    with pytest.raises(InstallConfirmationRequired, match="stale") as stale:
        importer.import_value(changed, artifact_bytes=changed_raw, confirm={
            "integration_id": value["id"], "summary_hash": initial.value.summary_hash,
        })
    assert stale.value.summary_hash != initial.value.summary_hash
    assert not manager.integrations.list()
    db.close()


def test_confirmation_audit_log_contains_only_summary_hash_and_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "confirm")
    manager, db = _manager()
    logged: list[dict[str, Any]] = []
    monkeypatch.setattr("app.services.application_logging.log_event", lambda **event: logged.append(event))
    value, raw = _file_value()
    importer = IntegrationFileService(manager)
    with pytest.raises(InstallConfirmationRequired) as required:
        importer.import_value(value, artifact_bytes=raw)
    importer.import_value(value, artifact_bytes=raw, confirm={
        "integration_id": value["id"], "summary_hash": required.value.summary_hash,
    })
    assert len(logged) == 1
    assert logged[0]["details"] == {
        "integration_id": value["id"],
        "confirmed_by": manager.actor_id,
        "policy": "confirm",
        "summary_hash": required.value.summary_hash,
    }
    assert "secret" not in json.dumps(logged[0]["details"]).lower()
    db.close()


def _seed_catalogue(db: sqlite3.Connection, entry: dict[str, Any]) -> None:
    ensure_catalogue_schema(db)
    db.execute(
        "UPDATE integration_catalogue_cache SET index_bytes=?,serial=11,verified_key_id='0011223344556677' WHERE id=1",
        (json.dumps({"entries": [entry]}, separators=(",", ":")).encode(),),
    )


@pytest.mark.parametrize("policy", ["confirm", "catalogue-only"])
def test_exact_catalogue_file_hash_needs_no_confirmation(policy: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", policy)
    manager, db = _manager()
    value, raw = _file_value()
    _seed_catalogue(db, {
        "id": value["id"], "version": value["version"], "kind": "file",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "review": {"reviewer": "Review fixture", "date": "2026-10-01"},
    })
    result = IntegrationFileService(manager).import_value(value, artifact_bytes=raw)
    assert result["integration"]["provenance"]["kind"] == "catalogue"
    db.close()


@pytest.mark.parametrize("policy", ["confirm", "catalogue-only", "open"])
def test_hash_mismatch_follows_outside_policy(policy: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", policy)
    if policy == "open":
        monkeypatch.setattr("app.services.application_logging.log_event", lambda **_event: None)
    manager, db = _manager()
    value, raw = _file_value()
    _seed_catalogue(db, {
        "id": value["id"], "version": value["version"], "kind": "file",
        "sha256": "f" * 64,
        "review": {"reviewer": "Review fixture", "date": "2026-10-01"},
    })
    importer = IntegrationFileService(manager)
    if policy == "confirm":
        with pytest.raises(InstallConfirmationRequired) as required:
            importer.import_value(value, artifact_bytes=raw)
        assert required.value.summary["catalogue_status"] == "id listed but hash differs"
        result = importer.import_value(value, artifact_bytes=raw, confirm={
            "integration_id": value["id"], "summary_hash": required.value.summary_hash,
        })
        assert result["integration"]["provenance"]["kind"] == "outside"
    elif policy == "catalogue-only":
        with pytest.raises(InstallPolicyForbidden):
            importer.import_value(value, artifact_bytes=raw)
    else:
        result = importer.import_value(value, artifact_bytes=raw)
        assert result["integration"]["provenance"]["kind"] == "outside"
    db.close()


class _DraftHTTP(HTTPService):
    def validate_public_url(self, _url: str) -> dict[str, Any]:
        return {}

    def request(self, _url: str, **_kwargs: Any) -> dict[str, Any]:
        return {"status_code": 200, "headers": {}, "body": json.dumps(SPEC).encode()}


@pytest.mark.parametrize("policy", ["confirm", "catalogue-only", "open"])
def test_draft_enable_uses_the_same_policy_and_confirmation(policy: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", policy)
    if policy == "open":
        monkeypatch.setattr("app.services.application_logging.log_event", lambda **_event: None)
    manager, db = _manager()
    service = IntegrationDraftService(manager, project_id="prj_policy", actor_id="admin-policy-test", http=_DraftHTTP())
    service._probe_openapi = lambda *_args, **_kwargs: {"status_code": 200, "value": {"ok": True}}
    draft = service.create(source_kind="openapi_url", url="https://docs.example.test/openapi.json")
    candidate = dict(draft["draft_json"])
    candidate["operations"] = [{"operation_id": "health", "enabled": True}]
    draft = service.update(draft["id"], candidate)
    draft = service.probe(draft["id"], operation_id="health", arguments={})
    if policy == "confirm":
        with pytest.raises(InstallConfirmationRequired) as required:
            service.enable(draft["id"])
        assert required.value.summary["kind"] == "declarative file"
        enabled = service.enable(draft["id"], confirm={
            "integration_id": required.value.summary["integration_id"],
            "summary_hash": required.value.summary_hash,
        })
        assert enabled["status"] == "enabled"
    elif policy == "catalogue-only":
        with pytest.raises(InstallPolicyForbidden):
            service.enable(draft["id"])
        assert not manager.integrations.list()
    else:
        assert service.enable(draft["id"])["status"] == "enabled"
    db.close()


def _package_manifest() -> dict[str, Any]:
    return {
        "schema_version": "1.2", "id": "lynewright.integration.policy-package",
        "name": "Policy package", "version": "1.0.0", "type": "service",
        "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0",
        "requested_capabilities": ["network:use"],
        "service": {"kind": "http", "health_check": "/health"}, "backend": "manual-runtime",
    }


def _install_package(manager: IntegrationManagementService, *, sha256: str = "") -> str:
    from app.services.production_integrations import ensure_installed

    manifest = _package_manifest()
    outcome = ensure_installed(
        manager.integrations, manifest, source="package", distribution="lynewright-policy-fixture",
        distribution_version="1.0.0", entrypoint_module="policy_fixture.manifest",
        artifact_sha256=sha256,
    )
    if outcome != "policy_blocked":
        manager.integrations.set_source_metadata(
            manifest["id"], source="package", distribution="lynewright-policy-fixture",
            version="1.0.0", entrypoint_module="policy_fixture.manifest",
        )
    return outcome


def test_inprocess_package_summary_uses_strong_execution_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.integration_install_policy import build_install_summary

    monkeypatch.setenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", "1")
    summary = build_install_summary(
        _package_manifest(), kind="native package",
        source="package:lynewright-policy-fixture==1.0.0",
    )
    assert summary["runs_code"] is True
    assert summary["execution"] == "runs INSIDE Lynewright with access to all data"


def test_package_waits_for_confirmation_and_is_fail_closed_until_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "confirm")
    manager, db = _manager()
    from app.services.production_integrations import DiscoveredManifest

    found = DiscoveredManifest(
        manifest=_package_manifest(), package_root="policy_fixture",
        product_role="external_integration", kind="integration", source="package",
        distribution="lynewright-policy-fixture", distribution_version="1.0.0",
        entrypoint_module="policy_fixture.manifest",
        installed_sha256="c" * 64,
    )
    monkeypatch.setattr("app.services.production_integrations.discover_shipped_manifests", lambda *_args: [found])
    assert _install_package(manager) == "installed"
    provenance = get_provenance(manager.store, manager.tenant_id, found.manifest["id"])
    assert provenance["status"] == "awaiting_confirmation"
    with pytest.raises(PlatformOperationError, match="awaiting admin confirmation"):
        manager.set_integration_enabled(found.manifest["id"], enabled=True)
    with pytest.raises(PlatformOperationError, match="awaiting admin confirmation"):
        IntegrationRunnerService(manager.store).create_or_rotate(found.manifest["id"])
    runtime = IntegrationOperationCatalog(manager)
    actor = CapabilityActor(
        client_id="policy-test", name="Policy test", client_type="human",
        permission_mode="full", granted_capabilities=frozenset(),
    )
    with pytest.raises(PlatformOperationError, match="unavailable"):
        runtime.resolve_operation(actor, project_id="prj_policy", contract="policy.fixture", operation="run")
    from app.services.production_integrations import compile_installed_definitions
    compiled = compile_installed_definitions(manager.integrations)
    assert compiled["diagnostics"][found.manifest["id"]] == "awaiting_confirmation:admin_review_required"
    with pytest.raises(InstallConfirmationRequired) as required:
        manager.confirm_package_install(found.manifest["id"], confirm=None)
    assert required.value.summary["kind"] == "native package"
    assert required.value.summary["execution"] == "runs in its own runner container"
    assert required.value.summary["sha256"] == "c" * 64
    changed = replace(found, installed_sha256="d" * 64)
    monkeypatch.setattr("app.services.production_integrations.discover_shipped_manifests", lambda *_args: [changed])
    with pytest.raises(InstallConfirmationRequired) as stale:
        manager.confirm_package_install(found.manifest["id"], confirm={
            "integration_id": found.manifest["id"], "summary_hash": required.value.summary_hash,
        })
    assert stale.value.summary_hash != required.value.summary_hash
    confirmed = manager.confirm_package_install(found.manifest["id"], confirm={
        "integration_id": found.manifest["id"], "summary_hash": stale.value.summary_hash,
    })
    assert confirmed["provenance"]["confirmed_by"] == manager.actor_id
    assert confirmed["provenance"]["confirmed_at"]
    assert manager.set_integration_enabled(found.manifest["id"], enabled=True)["integration"]["enabled"] is True
    assert IntegrationRunnerService(manager.store).create_or_rotate(found.manifest["id"])["token"].startswith("crn_")
    db.close()


def test_catalogue_only_blocks_unlisted_package_but_allows_exact_package_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "catalogue-only")
    manager, db = _manager()
    assert _install_package(manager) == "policy_blocked"
    assert not manager.integrations.list()

    digest = "b" * 64
    _seed_catalogue(db, {
        "id": _package_manifest()["id"], "version": "1.0.0", "kind": "package", "sha256": digest,
        "review": {"reviewer": "Review fixture", "date": "2026-10-01"},
    })
    assert _install_package(manager, sha256=digest) == "installed"
    provenance = get_provenance(manager.store, manager.tenant_id, _package_manifest()["id"])
    assert provenance["kind"] == "catalogue"
    db.close()


def test_open_policy_package_records_actor_and_confirmation_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "open")
    monkeypatch.setattr("app.services.application_logging.log_event", lambda **_event: None)
    manager, db = _manager()
    assert _install_package(manager) == "installed"
    provenance = get_provenance(manager.store, manager.tenant_id, _package_manifest()["id"])
    assert provenance["kind"] == "outside"
    assert provenance["confirmed_by"] == "admin:LYNEWRIGHT_INTEGRATION_PACKAGES"
    assert provenance["confirmed_at"]
    db.close()


def test_catalogue_only_keeps_existing_outside_install_but_blocks_reenable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "open")
    manager, db = _manager()
    assert _install_package(manager) == "installed"
    integration_id = _package_manifest()["id"]
    manager.set_integration_enabled(integration_id, enabled=True)

    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "catalogue-only")
    assert manager.integrations.get(integration_id)["enabled"] is True
    manager.set_integration_enabled(integration_id, enabled=False)
    with pytest.raises(InstallPolicyForbidden):
        manager.set_integration_enabled(integration_id, enabled=True)
    status = next(item for item in manager.package_statuses() if item.get("integration_id") == integration_id)
    assert status["status"] == "not_allowed_by_policy"
    db.close()


def test_invalid_install_policy_refuses_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    from app.main import lifespan

    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "silent")
    async def start() -> None:
        async with lifespan(FastAPI()):
            pass
    with pytest.raises(RuntimeError, match="LYNEWRIGHT_INSTALL_POLICY"):
        asyncio.run(start())


def test_manual_http_confirmation_uses_recomputed_summary_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "confirm")
    manager, db = _manager()
    manager.connections.credentials.set({"name": "vendor_api", "kind": "token", "secret": "fixture-only-value"})
    monkeypatch.setattr(router, "_service", lambda _context: manager)
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router._ADMIN] = lambda: {"tenant_id": "ten_default", "user_id": "admin-policy-test", "role": "admin"}
    payload, _raw = _manual_payload()
    with TestClient(app) as client:
        first = client.post("/api/integration-management/manual", json=payload)
        assert first.status_code == 409
        detail = first.json()["detail"]
        assert detail["requires_confirmation"] is True
        confirm = {"integration_id": detail["install_summary"]["integration_id"], "summary_hash": detail["summary_hash"]}
        stale = {**confirm, "summary_hash": "0" * 64}
        second = client.post("/api/integration-management/manual", json=payload,
                             headers={"X-Lynewright-Install-Confirm": json.dumps(stale)})
        assert second.status_code == 409
        assert second.json()["detail"]["requires_confirmation"] is True
        third = client.post("/api/integration-management/manual", json=payload,
                            headers={"X-Lynewright-Install-Confirm": json.dumps(confirm)})
        assert third.status_code == 201
    db.close()
