from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.services.extension_registry import ExtensionRegistry
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_lifecycle import IntegrationLifecycleRuntime, IntegrationRuntimeDefinition
from app.services.integration_management import IntegrationManagementService
from app.services.integration_removal import IntegrationRemovalService
from app.services.integration_runner import IntegrationRunnerService
from app.services.line_engine import ProductionLineEngine
from app.services.platform_services import MediaService, PlatformConflictError, PlatformNotFoundError, PlatformStore


def _setup(db: sqlite3.Connection, *, integration_id: str, configuration: dict | None = None, package: bool = False):
    store = PlatformStore(lambda: db)
    registry = ExtensionRegistry(store)
    manifest = {
        "schema_version": "1.2", "id": integration_id, "name": "Removal Fixture", "version": "1.0.0",
        "type": "native", "lynewright_version": ">=0.1.0,<1.0.0", "sdk_version": ">=1.0.0,<2.0.0",
    }
    registry.register(manifest, configuration=configuration or {})
    if package:
        registry.set_source_metadata(integration_id, source="package", distribution="lynewright-test-wheel", version="1.0.0")
    connections = ConnectionProfileService(store, registry=registry, endpoint_validator=lambda _url: {})
    contributions = ContributionRegistry(registry, connections=connections)
    management = IntegrationManagementService(registry, connections, contributions, actor_id="admin-1")
    return store, registry, management


@pytest.mark.parametrize("kind", ["manual", "file", "draft_enabled", "catalogue_file", "outside_package", "package", "container"])
def test_nonbundled_integration_kinds_remove_through_one_path(kind: str, tmp_path: Path, monkeypatch) -> None:
    integration_id = f"lynewright.integration.remove-{kind.replace('_', '-')}"
    configuration: dict = {}
    package = kind in {"outside_package", "package"}
    if package:
        package_root = tmp_path / "packages"
        package_directory = package_root / integration_id / "1.0.0"
        package_directory.mkdir(parents=True)
        (package_directory / "fixture.whl").write_bytes(b"wheel")
        staging = package_root / f".catalogue-install-{integration_id}-stale"
        staging.mkdir(parents=True)
        (staging / "partial").write_bytes(b"partial")
        monkeypatch.setenv("LYNEWRIGHT_PACKAGES_DIR", str(package_root))
        configuration = {"catalogue_package": {"wheel_directory": str(package_directory), "wheel_filename": "fixture.whl", "sha256": "a" * 64}}
    elif kind == "container":
        configuration = {"catalogue_container": {"image": "registry.test/example@sha256:" + "a" * 64}}

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store, registry, management = _setup(db, integration_id=integration_id, configuration=configuration, package=package)
    lifecycle = IntegrationLifecycleRuntime(registry, management.contributions)
    lifecycle.register_definition(IntegrationRuntimeDefinition(integration_id))
    management.lifecycle = lifecycle
    preview = IntegrationRemovalService(management).preview(integration_id)
    assert preview["bundled"] is False
    assert preview["blocked_reasons"] == []
    result = IntegrationRemovalService(management).remove(integration_id, confirm_name=integration_id)
    assert result["removed"] is True
    assert lifecycle.definitions_for(integration_id) == ()
    with pytest.raises(Exception, match="Extension not found"):
        registry.get(integration_id)
    if package:
        assert not Path(configuration["catalogue_package"]["wheel_directory"]).exists()
        assert not staging.exists()
    db.close()


def test_remove_blocked_by_line_then_disable_lines_removes_tokens_media_and_live_record(tmp_path: Path, monkeypatch) -> None:
    integration_id = "lynewright.integration.remove-used"
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store, registry, management = _setup(db, integration_id=integration_id)
    lines = ProductionLineEngine(store=store).db
    definition = {"stages": [{"id": "render", "kind": "operation", "integration_id": integration_id, "contract": f"{integration_id}.render", "operation": "render"}]}
    with db:
        db.execute(
            "INSERT INTO production_lines(tenant_id,id,project_id,name,version,definition_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("ten_default", "line-uses-integration", "project-1", "Uses fixture", 1, json.dumps(definition), 1, 1.0, 1.0),
        )
    runner = IntegrationRunnerService(store)
    issued = runner.create_or_rotate(integration_id)
    media_root = tmp_path / "media"
    monkeypatch.setenv("LYNEWRIGHT_MANAGED_MEDIA_DIR", str(media_root))
    media_root.mkdir()
    (media_root / "fixture.stored").write_bytes(b"image")
    media = MediaService(store).create_managed(
        name="fixture.png", content_type="image/png", metadata={"stored_as": "fixture.stored", "integration_id": integration_id, "bytes": 5},
    )
    service = IntegrationRemovalService(management)
    preview = service.preview(integration_id)
    assert [line["id"] for line in preview["lines"]] == ["line-uses-integration"]
    assert preview["blocked"] is True
    assert any("line-uses-integration" in reason and "disable those lines" in reason for reason in preview["blocked_reasons"])
    assert [item["id"] for item in preview["media"]] == [media["id"]]
    with pytest.raises(PlatformConflictError, match="line-uses-integration"):
        service.remove(integration_id, confirm_name=integration_id)

    removed = service.remove(integration_id, confirm_name=integration_id, disable_using_lines=True)
    assert removed["disabled_lines"] == ["line-uses-integration"]
    assert lines.get_line("line-uses-integration")["enabled"] is False
    assert runner.authenticate(issued["token"]) is None
    assert not (media_root / "fixture.stored").exists()
    with db:
        token_row = db.execute("SELECT revoked_at FROM integration_runner_tokens WHERE tenant_id='ten_default' AND integration_id=?", (integration_id,)).fetchone()
        assert token_row and token_row["revoked_at"]
    db.close()


def test_removal_preview_blocking_matches_delete_decision_for_line_usage_and_active_runs() -> None:
    integration_id = "lynewright.integration.remove-preview-agreement"
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store, registry, management = _setup(db, integration_id=integration_id)
    lines = ProductionLineEngine(store=store).db
    definition = {"stages": [{"id": "render", "kind": "operation", "integration_id": integration_id,
                              "contract": f"{integration_id}.render", "operation": "render"}]}
    with db:
        db.execute(
            "INSERT INTO production_lines(tenant_id,id,project_id,name,version,definition_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("ten_default", "line-preview-agreement", "project-1", "Preview agreement", 1, json.dumps(definition), 1, 1.0, 1.0),
        )
    service = IntegrationRemovalService(management)
    preview = service.preview(integration_id)
    assert preview["blocked"] is True
    assert preview["disable_lines_blocked_reasons"] == []
    with pytest.raises(PlatformConflictError, match="line-preview-agreement"):
        service.remove(integration_id, confirm_name=integration_id)
    assert service.remove(integration_id, confirm_name=integration_id, disable_using_lines=True)["removed"] is True

    # A live invocation remains a hard blocker even when line disabling is selected.
    other_id = "lynewright.integration.remove-preview-active"
    registry.register({
        "schema_version": "1.2", "id": other_id, "name": "Active fixture", "version": "1.0.0",
        "type": "native", "lynewright_version": ">=0.1.0,<1.0.0", "sdk_version": ">=1.0.0,<2.0.0",
    })
    active_definition = {"stages": [{"id": "render", "kind": "operation", "integration_id": other_id,
                                      "contract": f"{other_id}.render", "operation": "render"}]}
    with db:
        db.execute(
            "INSERT INTO production_lines(tenant_id,id,project_id,name,version,definition_json,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("ten_default", "line-active-preview", "project-1", "Active preview", 1, json.dumps(active_definition), 1, 1.0, 1.0),
        )
        db.execute(
            "INSERT INTO line_runs(tenant_id,id,line_id,project_id,status,current_stage,vars_json,actor_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("ten_default", "active-preview-run", "line-active-preview", "project-1", "running", "render", "{}", "{}", 1.0, 2.0),
        )
    active_preview = service.preview(other_id)
    assert active_preview["blocked"] is True
    assert any("active runs" in reason for reason in active_preview["disable_lines_blocked_reasons"])
    with pytest.raises(PlatformConflictError, match="active runs"):
        service.remove(other_id, confirm_name=other_id, disable_using_lines=True)
    db.close()


def test_bundled_integration_cannot_be_removed(tmp_path: Path) -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store, registry, management = _setup(db, integration_id="lynewright.integration.bundled-remove")
    registry.set_source_metadata("lynewright.integration.bundled-remove", source="bundled")
    service = IntegrationRemovalService(management)
    assert service.preview("lynewright.integration.bundled-remove")["bundled"] is True
    with pytest.raises(PlatformConflictError, match="Bundled integrations"):
        service.remove("lynewright.integration.bundled-remove", confirm_name="lynewright.integration.bundled-remove")
    db.close()


def test_removal_preview_only_offers_exclusive_integration_credentials_for_deletion() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    registry = ExtensionRegistry(store)
    for integration_id, name in (
        ("lynewright.integration.credential-owner", "Credential owner"),
        ("lynewright.integration.credential-sharer", "Credential sharer"),
    ):
        registry.register({
            "schema_version": "1.2", "id": integration_id, "name": name, "version": "1.0.0",
            "type": "native", "lynewright_version": ">=0.1.0,<1.0.0", "sdk_version": ">=1.0.0,<2.0.0",
        }, configuration={"credential_ref": "shared_ref"})
    with db:
        db.execute("INSERT INTO platform_credentials(name,kind,encrypted_secret) VALUES('shared_ref','token','ciphertext')")
        db.execute("INSERT INTO platform_credentials(name,kind,encrypted_secret) VALUES('exclusive_ref','token','ciphertext')")
        db.execute(
            "UPDATE platform_extensions SET configuration_json=? WHERE id=?",
            (json.dumps({"credential_ref": "shared_ref", "private_credential_ref": "exclusive_ref"}), "lynewright.integration.credential-owner"),
        )
    connections = ConnectionProfileService(store, registry=registry, endpoint_validator=lambda _url: {})
    contributions = ContributionRegistry(registry, connections=connections)
    management = IntegrationManagementService(registry, connections, contributions, actor_id="admin-1")
    preview = IntegrationRemovalService(management).preview("lynewright.integration.credential-owner")
    assert preview["credentials"]["shared"] == ["shared_ref"]
    assert preview["credentials"]["exclusive"] == ["exclusive_ref"]
    db.close()


def test_integration_list_skips_a_registry_row_removed_during_detail_lookup(monkeypatch) -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    _store, registry, management = _setup(db, integration_id="lynewright.integration.list-race")
    management._shipped_packages_reconciled = True
    monkeypatch.setattr(registry, "list", lambda: [{"id": "lynewright.integration.disappeared"}])
    monkeypatch.setattr(
        management, "detail",
        lambda _integration_id: (_ for _ in ()).throw(PlatformNotFoundError("Extension not found.")),
    )
    assert management.list_integrations() == []
    db.close()


def test_terminal_run_archive_hides_but_retains_history_and_active_runs_are_blocked() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    engine = ProductionLineEngine(store=store, tenant_id="ten_default")
    with db:
        db.execute(
            "INSERT INTO line_runs(tenant_id,id,line_id,project_id,status,current_stage,vars_json,actor_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("ten_default", "done-run", "line-1", "project-1", "done", "", "{}", "{}", 1.0, 2.0),
        )
        db.execute(
            "INSERT INTO line_runs(tenant_id,id,line_id,project_id,status,current_stage,vars_json,actor_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("ten_default", "active-run", "line-1", "project-1", "running", "render", "{}", "{}", 1.0, 2.0),
        )

    engine.archive_run("done-run", actor_id="admin-1")
    assert engine.get_run("done-run")["archived_at"]
    assert "done-run" not in {run["id"] for run in engine.list_runs()}
    with pytest.raises(PlatformConflictError, match="Only completed"):
        engine.archive_run("active-run", actor_id="admin-1")
    assert engine.get_run("active-run")["archived_at"] == ""
    db.close()
