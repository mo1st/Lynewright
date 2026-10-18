from __future__ import annotations

import ast
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from app.services.catalogue import ensure_schema as ensure_catalogue_schema
from app.services.integration_file import IntegrationFileService
from app.services.integration_management import IntegrationManagementService
from app.services.integration_provenance import (
    backfill_pre_k52_provenance,
    get_provenance,
    reconcile_catalogue_drift,
    record_provenance,
)
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.platform_services import PlatformStore

APP_ROOT = Path(__file__).parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "integration_files" / "openapi.yaml"
OPERATOR_KEY_ID = "abcd1234abcd5678"


@pytest.fixture(autouse=True)
def _legacy_provenance_tests_use_open_install_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep provenance tests focused; explicit policy gates are covered separately."""
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "open")
    operator_public = base64.b64encode(b"Ed" + bytes.fromhex(OPERATOR_KEY_ID) + b"\x01" * 32).decode("ascii")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", operator_public)
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "0")
    monkeypatch.setattr("app.services.application_logging.log_event", lambda **_event: None)


def _manager(tenant_id: str = "ten_default") -> tuple[IntegrationManagementService, sqlite3.Connection, PlatformStore]:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    db.execute("""CREATE TABLE tenant_projects(
        project_id TEXT, tenant_id TEXT, name TEXT, status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(tenant_id,project_id))""")
    registry = IntegrationRegistry(store, tenant_id=tenant_id)
    connections = ConnectionProfileService(
        store, tenant_id=tenant_id, registry=registry, endpoint_validator=lambda _url: {},
    )
    manager = IntegrationManagementService(
        registry, connections, ContributionRegistry(registry, connections=connections),
        actor_id="admin-test",
    )
    return manager, db, store


def _manifest(integration_id: str, version: str = "1.0.0") -> dict[str, Any]:
    return {
        "schema_version": "1.2", "id": integration_id, "name": "Provenance fixture",
        "version": version, "type": "service", "lynewright_version": "^0.1.0",
        "sdk_version": "^1.0.0", "requested_capabilities": [],
        "service": {"kind": "http", "health_check": "/health"},
        "backend": "manual-runtime",
    }


def _canonical_file(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _catalogue_index(integration_id: str, version: str, digest: str, *, yanked: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": integration_id, "version": version, "kind": "file", "sha256": digest,
        "review": {"reviewer": "Lynewright Trust", "date": "2026-10-01"},
    }
    if yanked:
        entry["yanked"] = {"reason": "review withdrawn"}
    return {"entries": [entry]}


def _seed_catalogue_cache(db: sqlite3.Connection, index: dict[str, Any], *, serial: int = 7) -> None:
    ensure_catalogue_schema(db)
    db.execute(
        "UPDATE integration_catalogue_cache SET index_bytes=?,serial=?,verified_key_id=? WHERE id=1",
        (json.dumps(index, separators=(",", ":")).encode(), serial, OPERATOR_KEY_ID),
    )


def test_bundled_install_records_provenance():
    from app.services.production_integrations import ensure_installed

    manager, db, _store = _manager()
    integration_id = "lynewright.test.provenance-bundle"
    assert ensure_installed(manager.integrations, _manifest(integration_id)) == "installed"
    provenance = get_provenance(manager.store, manager.tenant_id, integration_id)
    assert provenance["kind"] == "bundled"
    assert provenance["status"] == "current"
    db.close()


def test_catalogue_file_import_requires_exact_id_version_and_sha256_match():
    manager, db, _store = _manager()
    raw = FIXTURE.read_bytes()
    value = IntegrationFileService.parse(raw)
    digest = hashlib.sha256(raw).hexdigest()
    _seed_catalogue_cache(db, _catalogue_index(value["id"], value["version"], digest))
    result = IntegrationFileService(manager).import_value(value, artifact_bytes=raw)
    provenance = result["integration"]["provenance"]
    assert provenance["kind"] == "catalogue"
    assert provenance["sha256"] == digest
    assert provenance["catalogue_entry_id"] == value["id"]
    assert provenance["entry_version"] == value["version"]
    assert provenance["catalogue_serial"] == 7
    assert provenance["signing_key_id"] == OPERATOR_KEY_ID
    assert provenance["review"] == {"reviewer": "Lynewright Trust", "date": "2026-10-01"}
    assert provenance["confirmed_at"] is None
    db.close()


def test_same_file_id_with_a_different_catalogue_hash_is_outside():
    manager, db, _store = _manager()
    raw = FIXTURE.read_bytes()
    value = IntegrationFileService.parse(raw)
    digest = hashlib.sha256(raw).hexdigest()
    _seed_catalogue_cache(db, _catalogue_index(value["id"], value["version"], "f" * 64))
    result = IntegrationFileService(manager).import_value(value, artifact_bytes=raw)
    provenance = result["integration"]["provenance"]
    assert provenance["kind"] == "outside"
    assert provenance["source"] == f"file sha256:{digest}"
    assert provenance["confirmed_by"] == "admin-test"
    assert provenance["catalogue_entry_id"] is None
    db.close()


def test_stale_catalogue_cache_is_not_a_match_without_trusted_keys(monkeypatch: pytest.MonkeyPatch):
    from app.services.integration_install_policy import InstallConfirmationRequired

    manager, db, _store = _manager()
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "confirm")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", "")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "0")
    raw = FIXTURE.read_bytes()
    value = IntegrationFileService.parse(raw)
    digest = hashlib.sha256(raw).hexdigest()
    _seed_catalogue_cache(db, _catalogue_index(value["id"], value["version"], digest))

    with pytest.raises(InstallConfirmationRequired):
        IntegrationFileService(manager).import_value(value, artifact_bytes=raw)
    assert manager.integrations.list() == []
    db.close()


@pytest.mark.parametrize("stored_key_id", ["dev:deadbeef", "deadbeef"])
def test_dev_signed_install_drifts_when_development_key_is_no_longer_trusted(stored_key_id: str):
    manager, db, store = _manager()
    integration_id = "lynewright.test.dev-key-drift"
    manager.integrations.register(_manifest(integration_id))
    record_provenance(
        store, "ten_default", integration_id, kind="catalogue",
        catalogue_entry_id=integration_id, entry_version="1.0.0", sha256="a" * 64,
        catalogue_serial=6, signing_key_id=stored_key_id, reviewer="Development fixture",
        review_date="2026-10-16",
    )

    assert reconcile_catalogue_drift(
        store, _catalogue_index(integration_id, "1.0.0", "a" * 64), serial=7,
        trusted_signing_key_ids={"operator12345678"},
    ) == 1
    provenance = get_provenance(store, "ten_default", integration_id)
    assert provenance["status"] == "catalogue_drift"
    assert provenance["drift_reason"] == "signing key no longer trusted"
    assert manager.integrations.get(integration_id)["version"] == "1.0.0"
    db.close()


def test_startup_flags_dev_key_installs_as_drift_after_opt_in_is_removed(monkeypatch, tmp_path):
    from app.services.catalogue import (
        CatalogueService, _BUILTIN_DEV_PUBLIC_KEY, _public_key,
    )

    db_path = tmp_path / "catalogue-trust.sqlite3"
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(db_path))
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", "")
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "confirm")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "1")
    store = PlatformStore()
    integration_id = "lynewright.test.startup-dev-drift"
    IntegrationRegistry(store, tenant_id="ten_default").register(_manifest(integration_id))
    dev_key_id = _public_key(_BUILTIN_DEV_PUBLIC_KEY)[0].hex()
    dev_label = f"dev:{dev_key_id}"
    index = _catalogue_index(integration_id, "1.0.0", "a" * 64)
    with store.connection() as db:
        ensure_catalogue_schema(db)
        db.execute(
            "UPDATE integration_catalogue_cache SET index_bytes=?,serial=6,verified_key_id=? WHERE id=1",
            (json.dumps(index, separators=(",", ":")).encode(), dev_label),
        )
    record_provenance(
        store, "ten_default", integration_id, kind="catalogue",
        catalogue_entry_id=integration_id, entry_version="1.0.0", sha256="a" * 64,
        catalogue_serial=6, signing_key_id=dev_label, reviewer="Development fixture",
        review_date="2026-10-16",
    )
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "0")

    status = CatalogueService().startup_refresh()
    provenance = get_provenance(store, "ten_default", integration_id)
    assert status["status"] == "untrusted"
    assert provenance["status"] == "catalogue_drift"
    assert provenance["drift_reason"] == "signing key no longer trusted"
    assert IntegrationRegistry(store, tenant_id="ten_default").get(integration_id)["version"] == "1.0.0"


def test_manual_declarative_import_path_records_actor_and_file_hash():
    manager, db, _store = _manager()
    raw = FIXTURE.read_bytes()
    value = IntegrationFileService.parse(raw)
    result = IntegrationFileService(manager).import_value(value, artifact_bytes=raw)
    provenance = result["integration"]["provenance"]
    assert provenance["kind"] == "outside"
    assert provenance["confirmed_by"] == "admin-test"
    assert provenance["sha256"] == hashlib.sha256(raw).hexdigest()
    db.close()


def test_artifact_hash_uses_exact_yaml_and_json_bytes():
    yaml_bytes = FIXTURE.read_bytes()
    parsed = IntegrationFileService.parse(yaml_bytes)
    whitespace_yaml = yaml_bytes.replace(b"name: Local OpenAPI Fixture", b"name:  Local OpenAPI Fixture")
    pretty_json = json.dumps(parsed, ensure_ascii=False, indent=2).encode("utf-8")
    assert IntegrationFileService.parse(whitespace_yaml) == parsed
    assert IntegrationFileService.parse(pretty_json) == parsed
    digests = {hashlib.sha256(item).hexdigest() for item in (yaml_bytes, whitespace_yaml, pretty_json)}
    assert len(digests) == 3

    results = []
    for raw in (yaml_bytes, whitespace_yaml, pretty_json):
        manager, db, _store = _manager()
        value = IntegrationFileService.parse(raw)
        _seed_catalogue_cache(
            db, _catalogue_index(value["id"], value["version"], hashlib.sha256(yaml_bytes).hexdigest()),
        )
        result = IntegrationFileService(manager).import_value(value, artifact_bytes=raw)
        provenance = result["integration"]["provenance"]
        results.append((provenance["kind"], provenance["sha256"]))
        db.close()
    assert results == [
        ("catalogue", hashlib.sha256(yaml_bytes).hexdigest()),
        ("outside", hashlib.sha256(whitespace_yaml).hexdigest()),
        ("outside", hashlib.sha256(pretty_json).hexdigest()),
    ]


def test_package_install_records_distribution_as_outside(monkeypatch: pytest.MonkeyPatch):
    from app.services.production_integrations import ensure_installed

    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "confirm")
    manager, db, _store = _manager()
    integration_id = "lynewright.integration.external-package-fixture"
    assert ensure_installed(
        manager.integrations, _manifest(integration_id), source="package",
        distribution="lynewright-test-provider", distribution_version="2.4.1",
        entrypoint_module="fixture.manifest",
    ) == "installed"
    provenance = get_provenance(manager.store, manager.tenant_id, integration_id)
    assert provenance["kind"] == "outside"
    assert provenance["source"] == "package:lynewright-test-provider==2.4.1"
    assert provenance["status"] == "awaiting_confirmation"
    assert provenance["confirmed_by"] is None
    db.close()


def test_package_confirmation_file_set_digest_tracks_installed_code(tmp_path):
    from app.services.production_integrations import _distribution_file_set_sha256

    package = tmp_path / "fixture_pkg"
    package.mkdir()
    provider = package / "provider.py"
    manifest = package / "lynewright-manifest.json"
    provider.write_text("class Provider: pass\n", encoding="utf-8")
    manifest.write_text('{"id":"lynewright.integration.digest-fixture"}\n', encoding="utf-8")

    class Distribution:
        files = ["fixture_pkg/provider.py", "fixture_pkg/lynewright-manifest.json"]

        def locate_file(self, relative):
            return tmp_path / Path(relative)

    first = _distribution_file_set_sha256(Distribution())
    assert first and first == _distribution_file_set_sha256(Distribution())
    provider.write_text("class Provider: changed\n", encoding="utf-8")
    assert _distribution_file_set_sha256(Distribution()) != first


def test_package_definition_upgrade_refreshes_provenance_to_the_installed_version():
    from app.services.production_integrations import ensure_installed

    manager, db, _store = _manager()
    integration_id = "lynewright.integration.external-package-upgrade"
    assert ensure_installed(
        manager.integrations, _manifest(integration_id, "1.0.0"), source="package",
        distribution="lynewright-test-provider", distribution_version="1.0.0",
        entrypoint_module="fixture.manifest",
    ) == "installed"
    manager.integrations.set_source_metadata(
        integration_id, source="package", distribution="lynewright-test-provider",
        version="1.0.0", entrypoint_module="fixture.manifest",
    )
    assert ensure_installed(
        manager.integrations, _manifest(integration_id, "2.0.0"), source="package",
        distribution="lynewright-test-provider", distribution_version="2.0.0",
        entrypoint_module="fixture.manifest",
    ) == "refreshed"
    provenance = get_provenance(manager.store, manager.tenant_id, integration_id)
    assert provenance["source"] == "package:lynewright-test-provider==2.0.0"
    assert manager.integrations.get(integration_id)["version"] == "2.0.0"
    db.close()


def test_shipped_installer_refuses_to_mutate_without_a_provenance_store():
    from app.services.production_integrations import ensure_installed

    class RegistryWithoutStore:
        store = None
        register_calls = 0

        def get(self, _integration_id: str):
            raise AssertionError("Registry must not be queried before the provenance guard.")

        def register(self, _manifest):
            self.register_calls += 1

    registry = RegistryWithoutStore()
    assert ensure_installed(registry, _manifest("lynewright.test.no-store")) == "unavailable"
    assert registry.register_calls == 0


def test_pre_k52_startup_backfill_is_conservative_and_idempotent():
    manager, db, store = _manager()
    registry = manager.integrations
    registry.register(_manifest("lynewright.shipped.preexisting"))
    registry.set_source_metadata("lynewright.shipped.preexisting", source="bundled")
    registry.register(_manifest("lynewright.external.preexisting"))
    registry.set_source_metadata(
        "lynewright.external.preexisting", source="package", distribution="old-package",
        version="1.2.3", entrypoint_module="old_pkg.manifest",
    )
    assert backfill_pre_k52_provenance(store) == 2
    assert backfill_pre_k52_provenance(store) == 0
    bundled = get_provenance(store, "ten_default", "lynewright.shipped.preexisting")
    outside = get_provenance(store, "ten_default", "lynewright.external.preexisting")
    assert bundled["kind"] == "bundled"
    assert outside["kind"] == "outside"
    assert outside["source"] == "pre-K5.2 install, unknown"
    assert outside["confirmed_by"] == "unknown"
    assert outside["confirmed_at"] is None
    db.close()


@pytest.mark.parametrize(
    ("case", "index_factory", "reason"),
    [
        ("yanked", lambda i: _catalogue_index(i, "1.0.0", "a" * 64, yanked=True), "yanked"),
        ("missing", lambda _i: {"entries": []}, "missing"),
        ("hash", lambda i: _catalogue_index(i, "1.0.0", "b" * 64), "sha256 changed"),
    ],
)
def test_catalogue_drift_flags_without_updating_or_removing_install(case, index_factory, reason):
    manager, db, store = _manager()
    integration_id = f"lynewright.test.drift-{case}"
    manager.integrations.register(_manifest(integration_id, version="1.0.0"))
    record_provenance(
        store, "ten_default", integration_id, kind="catalogue",
        catalogue_entry_id=integration_id, entry_version="1.0.0", sha256="a" * 64,
        catalogue_serial=6, signing_key_id=OPERATOR_KEY_ID, reviewer="Lynewright Trust",
        review_date="2026-10-01",
    )
    assert reconcile_catalogue_drift(store, index_factory(integration_id), serial=7) == 1
    provenance = get_provenance(store, "ten_default", integration_id)
    assert provenance["status"] == "catalogue_drift"
    assert reason in provenance["drift_reason"]
    assert provenance["sha256"] == "a" * 64
    assert provenance["catalogue_serial"] == 6
    assert manager.integrations.get(integration_id)["version"] == "1.0.0"
    assert any(row["id"] == integration_id for row in manager.integrations.list())
    db.close()


def test_successful_catalogue_cache_refresh_runs_drift_reconciliation(monkeypatch, tmp_path):
    from app.services.catalogue import CatalogueService

    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(tmp_path / "lynewright.sqlite3"))
    store = PlatformStore()
    integration_id = "lynewright.test.drift-refresh-hook"
    registry = IntegrationRegistry(store, tenant_id="ten_default")
    registry.register(_manifest(integration_id, version="1.0.0"))
    record_provenance(
        store, "ten_default", integration_id, kind="catalogue",
        catalogue_entry_id=integration_id, entry_version="1.0.0", sha256="a" * 64,
        catalogue_serial=6, signing_key_id=OPERATOR_KEY_ID, reviewer="Lynewright Trust",
        review_date="2026-10-01",
    )
    refreshed = {
        "serial": 7, "generated_at": "2026-10-15T00:00:00Z",
        "entries": [_catalogue_index(integration_id, "1.0.0", "b" * 64)["entries"][0]],
    }
    body = json.dumps(refreshed, separators=(",", ":")).encode("utf-8")
    CatalogueService()._record_success(body, refreshed, OPERATOR_KEY_ID)
    provenance = get_provenance(store, "ten_default", integration_id)
    assert provenance["status"] == "catalogue_drift"
    assert "sha256 changed" in provenance["drift_reason"]
    assert provenance["catalogue_serial"] == 6
    assert registry.get(integration_id)["version"] == "1.0.0"


def _unprovenanced_install_mutations(sources: dict[str, str]) -> list[str]:
    failures: list[str] = []
    for filename, source in sources.items():
        tree = ast.parse(source, filename=filename)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            mutations = []
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                if call.func.attr not in {"register", "refresh_manifest"}:
                    continue
                receiver = call.func.value
                integration_registry = (
                    isinstance(receiver, ast.Name) and receiver.id == "registry"
                ) or (
                    isinstance(receiver, ast.Attribute) and receiver.attr == "integrations"
                    and isinstance(receiver.value, ast.Name) and receiver.value.id == "self"
                )
                if integration_registry and (
                    call.func.attr == "register" or node.name == "ensure_installed"
                ):
                    mutations.append(call)
            if mutations and not any(
                isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "record_provenance" for call in ast.walk(node)
            ):
                failures.append(f"{filename}:{node.name}")
    return failures


def _install_function_names_with_registry_mutations(source: str) -> list[str]:
    tree = ast.parse(source)
    result: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in {"register", "refresh_manifest"}:
                continue
            receiver = call.func.value
            is_registry = (
                isinstance(receiver, ast.Name) and receiver.id == "registry"
            ) or (
                isinstance(receiver, ast.Attribute) and receiver.attr == "integrations"
                and isinstance(receiver.value, ast.Name) and receiver.value.id == "self"
            )
            if is_registry and (call.func.attr == "register" or node.name == "ensure_installed"):
                result.append(node.name)
                break
    return result


def test_install_entry_point_ast_guard_requires_single_provenance_writer():
    sources = {
        "services/production_integrations.py": (APP_ROOT / "services" / "production_integrations.py").read_text(encoding="utf-8"),
        "services/integration_management.py": (APP_ROOT / "services" / "integration_management.py").read_text(encoding="utf-8"),
    }
    tree_calls = {
        f"{filename}:{function_name}"
        for filename, source in sources.items()
        for function_name in _install_function_names_with_registry_mutations(source)
    }
    assert tree_calls == {
        "services/production_integrations.py:ensure_installed",
        "services/integration_management.py:create_manual",
    }
    assert _unprovenanced_install_mutations(sources) == []
    probe = {"probe.py": "def accidental_install(registry):\n    registry.register(manifest)\n"}
    assert _unprovenanced_install_mutations(probe) == ["probe.py:accidental_install"]

    for path in APP_ROOT.rglob("*.py"):
        if path.name in {"integration_provenance.py", "test_integration_provenance.py"}:
            continue
        contents = path.read_text(encoding="utf-8-sig")
        assert "INSERT INTO integration_provenance" not in contents
        assert "UPDATE integration_provenance" not in contents
        assert "DELETE FROM integration_provenance" not in contents


def _confirmation_gate_failures(sources: dict[str, str], requirements: dict[str, dict[str, set[str]]]) -> list[str]:
    """Require every public outside-install hop to reach the shared gate."""
    failures: list[str] = []
    for filename, functions in requirements.items():
        tree = ast.parse(sources[filename], filename=filename)
        nodes = {
            node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name, required_calls in functions.items():
            node = nodes.get(function_name)
            if node is None:
                failures.append(f"{filename}:{function_name} (missing entry point)")
                continue
            calls = {
                call.func.id if isinstance(call.func, ast.Name) else call.func.attr
                for call in ast.walk(node) if isinstance(call, ast.Call)
                and isinstance(call.func, (ast.Name, ast.Attribute))
            }
            if not required_calls.issubset(calls):
                failures.append(f"{filename}:{function_name} (missing {','.join(sorted(required_calls - calls))})")
    return failures


def test_outside_install_paths_are_fenced_by_the_shared_confirmation_gate():
    app_root = APP_ROOT
    root = app_root.parents[1]
    sources = {
        "integration_file.py": (app_root / "services" / "integration_file.py").read_text(encoding="utf-8"),
        "integration_drafts.py": (app_root / "services" / "integration_drafts.py").read_text(encoding="utf-8"),
        "integration_management.py": (app_root / "services" / "integration_management.py").read_text(encoding="utf-8"),
        "integration_management_router.py": (app_root / "routers" / "integration_management.py").read_text(encoding="utf-8"),
        "production_integrations.py": (app_root / "services" / "production_integrations.py").read_text(encoding="utf-8"),
    }
    requirements = {
        "integration_file.py": {
            "import_value": {"decide_install"},
            "import_manual": {"import_value"},
        },
        "integration_drafts.py": {"enable": {"import_value"}},
        "integration_management.py": {"confirm_package_install": {"decide_install"}},
        "integration_management_router.py": {
            "create_manual": {"import_manual"},
            "import_integration_file": {"import_value"},
            "enable_integration_draft": {"enable"},
            "confirm_package_install": {"confirm_package_install"},
        },
    }
    assert _confirmation_gate_failures(sources, requirements) == []
    package_installer = sources["production_integrations.py"]
    assert '"catalogue-only"' in package_installer
    assert 'status="awaiting_confirmation"' in package_installer

    # A registry mutation that writes outside provenance without passing the
    # shared gate is rejected by the same guard used for real entry points.
    probe = {"probe.py": "def accidental_install(registry, manifest):\n    registry.register(manifest)\n    record_provenance(store, tenant, manifest['id'], kind='outside')\n"}
    assert _confirmation_gate_failures(probe, {"probe.py": {"accidental_install": {"decide_install"}}}) == [
        "probe.py:accidental_install (missing decide_install)"
    ]
