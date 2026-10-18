from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
import zipfile
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.routers.integration_runner import router as runner_router
from app.services.capability_context import CapabilityInvocationContext
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_host import DatabaseContainerInvocationBridge
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_runner import IntegrationRunnerService
from app.services.integration_worker_invocations import IntegrationWorkerInvocationStore
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import (
    CredentialService, MediaService, PlatformNotFoundError, PlatformOperationError, PlatformStore,
)


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
RUNNER = ROOT / "runner" / "runner.py"
TEMPLATE_NATIVE = Path(os.getenv("LYNEWRIGHT_TEMPLATE_REPO", ROOT / "templates/starters")) / "native"
TENANT = "ten_default"
PROJECT = "runner-test-project"


def _store(tmp_path: Path) -> tuple[PlatformStore, list[sqlite3.Connection]]:
    opened: list[sqlite3.Connection] = []
    path = tmp_path / "runner.sqlite3"

    def connect() -> sqlite3.Connection:
        db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        opened.append(db)
        return db

    return PlatformStore(connect), opened


def _manifest(
    integration_id: str, *, module: str = "runner_fixture", credential_refs: list[str] | None = None,
    provides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.2", "id": integration_id, "name": "Runner Fixture",
        "version": "1.0.0", "type": "native", "lynewright_version": "^0.1.0",
        "sdk_version": "1.0.0", "requested_capabilities": ["system:read"],
        "contributions": ["services"], "credential_refs": credential_refs or [],
        "provides": provides or [{
            "contract": "lynewright.fixture.runner", "implementation": f"{module}.provider:RunnerProvider",
            "version": "1.0.0", "capabilities": ["run"],
            "operation_schemas": {"run": {
                "type": "object", "properties": {"value": {"type": "string"}},
                "required": ["value"], "additionalProperties": False,
                "caller_capability": "system:read", "read_only": True,
                "risk_class": "READ_ONLY", "description": "Runner fixture operation.",
            }},
        }],
    }


def _register(store: PlatformStore, integration_id: str, *, manifest: dict[str, Any] | None = None) -> IntegrationRegistry:
    registry = IntegrationRegistry(store, tenant_id=TENANT)
    registry.register(manifest or _manifest(integration_id))
    registry.set_source_metadata(
        integration_id, source="package", distribution="lynewright-integration-runner-fixture",
        version="1.0.0", entrypoint_module="runner_fixture.manifest",
    )
    registry.enable(integration_id)
    return registry


def _context(integration_id: str, *, connection_id: str | None = None) -> CapabilityInvocationContext:
    actor = CapabilityActor(
        client_id="runner-test", name="Runner test", client_type="automation",
        permission_mode="full", granted_capabilities=frozenset({"system:read"}),
    )
    return CapabilityInvocationContext.from_actor(
        actor, tenant_id=TENANT, actor_type="automation", project_id=PROJECT,
        integration_id=integration_id, connection_id=connection_id,
        resource_type="production_line_run", resource_id="run-1", entry_point="test.runner",
    )


def _enqueue(
    store: PlatformStore, integration_id: str, *, connection_id: str | None = None,
    value: str = "ok",
) -> dict[str, Any]:
    return IntegrationWorkerInvocationStore(store, tenant_id=TENANT).enqueue(
        contribution_id=f"{integration_id}:lynewright.fixture.runner.provider",
        integration_id=integration_id, execution_plane="container", operation="run",
        args=("run", {"value": value}), kwargs={},
        invocation_context=_context(integration_id, connection_id=connection_id),
    )


def test_runner_tokens_are_hashed_scoped_and_revocable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.services import integration_host

    store, opened = _store(tmp_path)
    monkeypatch.setattr(integration_host, "store", store)
    integration_a = "lynewright.integration.runner-a"
    integration_b = "lynewright.integration.runner-b"
    registry = _register(store, integration_a)
    _register(store, integration_b)
    service = IntegrationRunnerService(store, tenant_id=TENANT)
    token_a = service.create_or_rotate(integration_a)["token"]
    token_b = service.create_or_rotate(integration_b)["token"]
    request_a = _enqueue(store, integration_a)
    media_root = tmp_path / "token-isolation-media"
    media_root.mkdir()
    (media_root / "b-input.txt").write_text("integration B media", encoding="utf-8")
    monkeypatch.setenv("LYNEWRIGHT_MANAGED_MEDIA_DIR", str(media_root))
    b_media = MediaService(store, tenant_id=TENANT).create_managed(
        name="b-input.txt", content_type="text/plain", metadata={"stored_as": "b-input.txt"},
        project_id=PROJECT,
    )
    request_b = _enqueue(store, integration_b, value=f"managed://{b_media['id']}")
    app = FastAPI()
    app.include_router(runner_router)
    with TestClient(app) as client:
        claim_a = client.post("/api/runner/v1/claim", headers={"Authorization": f"Bearer {token_a}"}, json={"wait_seconds": 0})
        assert claim_a.status_code == 200, claim_a.text
        assert claim_a.json()["invocation"]["id"] == request_a["id"]
        foreign_claim = client.post(
            "/api/runner/v1/claim", headers={"Authorization": f"Bearer {token_a}"}, json={"wait_seconds": 0},
        )
        assert foreign_claim.status_code == 200 and foreign_claim.json()["invocation"] is None
        foreign_media = client.get(
            f"/api/runner/v1/invocations/{request_b['id']}/media/{b_media['id']}/link",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert foreign_media.status_code in {403, 404}, foreign_media.text
        claim_b = client.post("/api/runner/v1/claim", headers={"Authorization": f"Bearer {token_b}"}, json={"wait_seconds": 0})
        assert claim_b.status_code == 200 and claim_b.json()["invocation"]["id"] == request_b["id"]
        cross_finish = client.post(
            f"/api/runner/v1/{request_b['id']}/complete",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"claim_token": claim_b.json()["invocation"]["claim_token"], "result": {"value": "bad"}},
        )
        assert cross_finish.status_code in {403, 404}, cross_finish.text
        cross_fail = client.post(
            f"/api/runner/v1/{request_b['id']}/fail",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"claim_token": claim_b.json()["invocation"]["claim_token"], "error_code": "forged"},
        )
        assert cross_fail.status_code in {403, 404}, cross_fail.text
        cross_upload = client.post(
            f"/api/runner/v1/invocations/{request_b['id']}/artifacts",
            headers={"Authorization": f"Bearer {token_a}", "x-filename": "forged.txt", "content-type": "text/plain"},
            content=b"forged integration A artifact",
        )
        assert cross_upload.status_code in {403, 404}, cross_upload.text
        with store.connection() as db:
            b_row = db.execute(
                "SELECT status,error,result_json FROM integration_worker_invocations WHERE id=?", (request_b["id"],),
            ).fetchone()
        assert b_row["status"] == "running" and not b_row["error"] and not b_row["result_json"]
        b_identity = service.authenticate(token_b)
        assert b_identity
        IntegrationWorkerInvocationStore(store, tenant_id=TENANT).complete_for_runner(
            request_b["id"], integration_id=integration_b, runner_id=b_identity["runner_id"],
            claim_token=claim_b.json()["invocation"]["claim_token"], result={"value": "complete"},
        )
        service.revoke(integration_a)
        revoked = client.post("/api/runner/v1/heartbeat", headers={"Authorization": f"Bearer {token_a}"}, json={})
        assert revoked.status_code == 401, revoked.text
    with store.connection() as db:
        rows = db.execute("SELECT token_hash FROM integration_runner_tokens").fetchall()
    assert all(row["token_hash"] not in {token_a, token_b} for row in rows)
    for db in opened:
        db.close()


def test_claim_sends_only_declared_bound_credentials_and_never_persists_them(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    integration_id = "lynewright.integration.runner-credentials"
    store, opened = _store(tmp_path)
    registry = _register(store, integration_id, manifest=_manifest(
        integration_id, credential_refs=["declared_ref"],
    ))
    CredentialService(store).set({"name": "declared_ref", "secret": "only-this-secret"})
    CredentialService(store).set({"name": "not_declared", "secret": "must-not-leave-api"})
    connections = ConnectionProfileService(store, tenant_id=TENANT, registry=registry, endpoint_validator=lambda _url: {})
    connection = connections.create(
        integration_id, name="Runner connection", connection_id="runner-connection",
        project_id=PROJECT, credential_refs=["declared_ref", "not_declared"],
    )
    queued = _enqueue(store, integration_id, connection_id=str(connection["id"]))
    token = IntegrationRunnerService(store, tenant_id=TENANT).create_or_rotate(integration_id)["token"]
    identity = IntegrationRunnerService(store, tenant_id=TENANT).authenticate(token)
    assert identity
    claimed = IntegrationRunnerService(store, tenant_id=TENANT).claim(identity, wait_seconds=0)
    assert claimed and claimed["id"] == queued["id"]
    assert claimed["credentials"] == {"declared_ref": "only-this-secret"}
    with store.connection() as db:
        row = db.execute(
            "SELECT args_json,kwargs_json,context_json,result_json,error FROM integration_worker_invocations WHERE id=?",
            (queued["id"],),
        ).fetchone()
    assert all("only-this-secret" not in str(value) and "must-not-leave-api" not in str(value) for value in row)
    assert "only-this-secret" not in caplog.text and "must-not-leave-api" not in caplog.text
    for db in opened:
        db.close()


def test_runner_media_link_and_artifact_stay_in_the_claimed_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    integration_id = "lynewright.integration.runner-media"
    store, opened = _store(tmp_path)
    _register(store, integration_id)
    media_root = tmp_path / "managed-media"
    media_root.mkdir()
    (media_root / "input.bin").write_bytes(b"runner input")
    monkeypatch.setenv("LYNEWRIGHT_MANAGED_MEDIA_DIR", str(media_root))
    input_media = MediaService(store, tenant_id=TENANT).create_managed(
        name="input.bin", content_type="application/octet-stream",
        metadata={"stored_as": "input.bin"}, project_id=PROJECT,
    )
    queued = _enqueue(store, integration_id, value=f"managed://{input_media['id']}")
    service = IntegrationRunnerService(store, tenant_id=TENANT)
    token = service.create_or_rotate(integration_id)["token"]
    identity = service.authenticate(token)
    assert identity
    claimed = service.claim(identity, wait_seconds=0)
    assert claimed
    link = service.media_link(identity, request_id=claimed["id"], media_id=input_media["id"])
    assert "/api/media/l/" in link["url"]
    with pytest.raises(PlatformNotFoundError, match="Media not found in this invocation"):
        service.media_link(identity, request_id=claimed["id"], media_id="another-project-media-id")
    artifact = service.upload_artifact(
        identity, request_id=claimed["id"], body=b"runner output", name="result.bin",
        content_type="application/octet-stream",
    )
    assert artifact["project_id"] == PROJECT
    output_media = MediaService(store, tenant_id=TENANT).get(artifact["media_id"])
    assert output_media["project_id"] == PROJECT
    stored_as = output_media["metadata"]["stored_as"]
    assert (media_root / stored_as).read_bytes() == b"runner output"
    with store.connection() as db:
        link_row = db.execute("SELECT project_id,run_id FROM platform_media_links ORDER BY created_at DESC LIMIT 1").fetchone()
    assert link_row["project_id"] == PROJECT and link_row["run_id"] == "run-1"
    IntegrationWorkerInvocationStore(store, tenant_id=TENANT).complete_for_runner(
        claimed["id"], integration_id=integration_id, runner_id=identity["runner_id"],
        claim_token=claimed["claim_token"], result={"status": "done"},
    )
    with pytest.raises(PlatformOperationError, match="actively claimed"):
        service.media_link(identity, request_id=queued["id"], media_id=input_media["id"])
    for db in opened:
        db.close()


def test_container_invocation_fails_closed_without_healthy_runner(tmp_path: Path) -> None:
    from types import SimpleNamespace

    integration_id = "lynewright.integration.runner-unavailable"
    store, opened = _store(tmp_path)
    _register(store, integration_id)
    bridge = DatabaseContainerInvocationBridge(store)
    contribution = SimpleNamespace(id=f"{integration_id}:provider", integration_id=integration_id, metadata={})
    with pytest.raises(PlatformOperationError, match="runner not connected"):
        bridge.invoke(contribution, ("run", {"value": "x"}), {}, invocation_context=_context(integration_id))
    for db in opened:
        db.close()


@pytest.mark.parametrize("name", [
    "LYNEWRIGHT_DB_PATH", "LYNEWRIGHT_DATABASE_URL", "LYNEWRIGHT_DATA_DIR", "LYNEWRIGHT_MANAGED_MEDIA_DIR",
])
def test_runner_refuses_visible_database_configuration(tmp_path: Path, name: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith((
        "LYNEWRIGHT_DB_PATH", "LYNEWRIGHT_DATABASE_URL", "LYNEWRIGHT_DATA_DIR", "LYNEWRIGHT_MANAGED_MEDIA_DIR",
    ))}
    env[name] = str(tmp_path / "forbidden.sqlite3")
    env["PYTHONPATH"] = str(BACKEND)
    result = subprocess.run(
        [sys.executable, str(RUNNER)], cwd=BACKEND, env=env, capture_output=True,
        text=True, timeout=15, check=False,
    )
    assert result.returncode == 1
    assert "Lynewright runner refused: database or data environment configuration is present" in result.stderr
    assert not (tmp_path / "forbidden.sqlite3").exists()


def test_runtime_config_matches_hardened_compose_and_quadlet_snapshots() -> None:
    import yaml
    from app.services.runner_runtime_config import _systemd_quote, generate_runtime_config

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integration = {
        "id": "lynewright.integration.runner-snapshot", "name": "Runner Snapshot",
        "version": "1.2.3", "source": "package", "source_entrypoint": "snapshot_package.manifest",
    }
    fixture_dir = Path(__file__).parent / "fixtures" / "runner"
    compose = generate_runtime_config(store, integration, format="compose")
    assert compose["content"] == (fixture_dir / "compose-runtime.yaml").read_text(encoding="utf-8")
    compose_doc = yaml.safe_load(compose["content"])
    service = compose_doc["services"]["lynewright-runner-lynewright-integration-runner-snapshot"]
    assert service["read_only"] is True and service["user"] == "10001:10001"
    assert service["cap_drop"] == ["ALL"] and "no-new-privileges:true" in service["security_opt"]
    assert "ports" not in service and service["networks"] == ["lynewright-integration"]
    assert service["build"]["context"] == "${LYNEWRIGHT_RUNNER_BUILD_CONTEXT:-runner}"
    assert service["build"]["dockerfile"] == "Containerfile.integration"
    assert compose_doc["networks"]["lynewright-integration"]["internal"] is True
    assert "LYNEWRIGHT_RUNNER_TOKEN" not in compose["content"]

    quadlet = generate_runtime_config(store, integration, format="quadlet")
    assert quadlet["content"] == (fixture_dir / "quadlet-runtime.container").read_text(encoding="utf-8")
    assert quadlet["network_content"] == (fixture_dir / "quadlet-runtime.network").read_text(encoding="utf-8")
    assert "HealthCmd=" not in quadlet["content"]
    assert not any(line.startswith("HealthCmd=[") or 'HealthCmd="' in line for line in quadlet["content"].splitlines())
    assert all(
        not (" " in line.split("=", 1)[1] and not line.split("=", 1)[1].startswith('"'))
        for line in quadlet["content"].splitlines() if line.startswith("Environment=")
    )
    assert _systemd_quote("LYNEWRIGHT_RUNNER_NOTE=two words") == '"LYNEWRIGHT_RUNNER_NOTE=two words"'
    assert quadlet["token_env_file"] == "/etc/lynewright/runner-secrets/lynewright-integration-runner-snapshot.env"
    assert "LYNEWRIGHT_RUNNER_TOKEN" not in quadlet["content"]

    from app.services.catalogue import ensure_schema as ensure_catalogue_schema
    from app.services.integration_provenance import record_provenance

    index_bytes = (Path(__file__).parent / "fixtures" / "catalogue-index.json").read_bytes()
    wrapper_entry = next(
        item for item in json.loads(index_bytes)["entries"] if item["id"] == "fixture.wrapper"
    )
    ensure_catalogue_schema(db)
    db.execute(
        """INSERT OR REPLACE INTO integration_catalogue_cache
           (id,index_bytes,index_sha256,serial,generated_at,fetched_at,verified_key_id)
           VALUES (1,?,'',1,'','','fixture-key')""",
        (index_bytes,),
    )
    wrapper_digest = wrapper_entry["source"].rsplit("@", 1)[-1]
    record_provenance(
        store, TENANT, "fixture.wrapper", kind="catalogue",
        catalogue_entry_id="fixture.wrapper", entry_version="1.0.0",
        image_digest=wrapper_digest, catalogue_serial=1, signing_key_id="fixture-key",
        reviewer="Lynewright test", review_date="2026-10-15",
    )
    container_image = generate_runtime_config(
        store,
        {"id": "fixture.wrapper", "name": "Wrapper", "version": "1.0.0", "source": "catalogue"},
        format="compose",
    )
    container_service = yaml.safe_load(container_image["content"])["services"]["lynewright-runner-fixture-wrapper"]
    assert container_service["image"] == wrapper_entry["source"]
    assert container_service["mem_limit"] == "128m" and container_service["pids_limit"] == 32
    assert "env_file" not in container_service and "environment" not in container_service
    assert container_image["token_env_file"] is None
    db.close()


@pytest.mark.parametrize("catalogue_install", [False, True], ids=["outside-confirmed", "signed-catalogue-wheel"])
def test_template_native_starter_runs_in_a_runner_subprocess_from_a_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, catalogue_install: bool,
) -> None:
    if not (TEMPLATE_NATIVE / "example_native" / "provider.py").is_file():
        pytest.skip(f"Template native starter is unavailable at {TEMPLATE_NATIVE}; set LYNEWRIGHT_TEMPLATE_REPO.")
    import uvicorn
    from app.services import integration_host
    from app.services.integration_lifecycle import IntegrationLifecycleRuntime
    from app.services.integration_management import IntegrationManagementService
    from app.services.integration_operations import IntegrationOperationCatalog
    from app.services.line_engine import ProductionLineEngine
    from app.services.integration_worker_invocations import DatabaseContainerInvocationBridge

    module = "k53_template_native"
    distribution = "lynewright-integration-k53-template-runner"
    integration_id = "lynewright.integration.k53-template-runner"
    provider_imports = tmp_path / "provider-import-pids.txt"
    api_import_sentinel = tmp_path / "api-package-imported.txt"
    contract = "lynewright.fixture.k53-template-native"
    manifest = json.loads((TEMPLATE_NATIVE / "example_native" / "lynewright-manifest.json").read_text(encoding="utf-8"))
    manifest["id"] = integration_id
    manifest["name"] = "K5.3b Template Runner Fixture"
    manifest["requested_capabilities"] = ["media:create"]
    manifest["provides"][0]["contract"] = contract
    manifest["provides"][0]["implementation"] = f"{module}.provider:ExampleMediaProvider"
    package = tmp_path / module
    package.mkdir()
    api_import_guard = (
        "import os\nfrom pathlib import Path\n"
        f"if not os.environ.get('LYNEWRIGHT_RUNNER_TOKEN'):\n    Path({str(api_import_sentinel)!r}).write_text('imported', encoding='utf-8')\n"
        "    raise RuntimeError('package code imported in the API process')\n"
    )
    (package / "__init__.py").write_text(api_import_guard, encoding="utf-8")
    (package / "manifest.py").write_text(api_import_guard + f"FIXTURE_MANIFEST = {manifest!r}\n", encoding="utf-8")
    (package / "lynewright-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    provider_source = (TEMPLATE_NATIVE / "example_native" / "provider.py").read_text(encoding="utf-8")
    marker = f"from pathlib import Path\nimport os\nPath({str(provider_imports)!r}).open('a', encoding='utf-8').write(str(os.getpid()) + '\\n')\n"
    provider_source = provider_source.replace("from __future__ import annotations\n", "from __future__ import annotations\n" + marker, 1)
    (package / "provider.py").write_text(provider_source, encoding="utf-8")
    dist = tmp_path / "lynewright_integration_k53_template_runner-1.0.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 1.0.0\n\n", encoding="utf-8",
    )
    (dist / "entry_points.txt").write_text(
        f"[lynewright.integrations]\nfixture = {module}.manifest\n", encoding="utf-8",
    )
    (dist / "WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: Lynewright test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        encoding="utf-8",
    )
    wheel_files = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for root in (package, dist)
        for path in root.rglob("*")
        if path.is_file() and path.name != "RECORD"
    }
    record_rows = []
    for wheel_path, content in sorted(wheel_files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
        record_rows.append(f"{wheel_path},sha256={digest},{len(content)}\n")
    record_path = f"{dist.name}/RECORD"
    record_rows.append(f"{record_path},,\n")
    wheel_files[record_path] = "".join(record_rows).encode("utf-8")
    wheel_filename = "lynewright_integration_k53_template_runner-1.0.0-py3-none-any.whl"
    wheel_path = tmp_path / wheel_filename
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for wheel_path_name, content in sorted(wheel_files.items()):
            archive.writestr(wheel_path_name, content)
    wheel_bytes = wheel_path.read_bytes()
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", distribution)
    monkeypatch.delenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", raising=False)
    import importlib
    importlib.invalidate_caches()

    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(tmp_path / "runner.sqlite3"))
    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_DIR", str(tmp_path / "catalogue-packages"))
    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_HOST_DIR", str(tmp_path / "catalogue-packages"))
    from app.database import init_db
    init_db()
    store, opened = _store(tmp_path)
    registry = IntegrationRegistry(store, tenant_id=TENANT)
    connections = ConnectionProfileService(store, tenant_id=TENANT, registry=registry, endpoint_validator=lambda _url: {})
    contributions = ContributionRegistry(registry, connections=connections)
    contributions.container_bridge = DatabaseContainerInvocationBridge(store)
    runtime = IntegrationLifecycleRuntime(registry, contributions)
    management = IntegrationManagementService(registry, connections, contributions, lifecycle=runtime)
    from app.services.production_integrations import ensure_discovered_integrations_installed, compile_installed_definitions

    from app.routers import catalogue as catalogue_router
    from app.routers import integration_management as management_router
    from app.routers import production_lines as production_lines_router
    from app.routers import media_links as media_links_router
    api = FastAPI()
    api.include_router(catalogue_router.router)
    api.include_router(management_router.router)
    api.include_router(production_lines_router.router)
    api.include_router(runner_router)
    monkeypatch.setattr(management_router, "_service", lambda _context: management)
    project = "demo-project"
    context = {"tenant_id": TENANT, "user_id": 1, "role": "owner"}
    for dependency in (
        catalogue_router._ADMIN, management_router._READ, management_router._WRITE,
        management_router._ADMIN, production_lines_router._READ,
        production_lines_router._WRITE, production_lines_router._ADMIN,
    ):
        api.dependency_overrides[dependency] = lambda: context
    catalogue = IntegrationOperationCatalog(management)
    engine = ProductionLineEngine(store=store, tenant_id=TENANT)
    monkeypatch.setattr(production_lines_router, "_engine", lambda _context: engine)
    monkeypatch.setattr(media_links_router, "PlatformStore", lambda: store)
    monkeypatch.setattr(media_links_router, "_rate_limit", lambda _digest, _client_ip: True)
    api.include_router(media_links_router.router)

    import app.services.integration_operations as operation_module
    import app.services.line_runner as line_runner_module
    monkeypatch.setattr(operation_module, "build_tenant_operation_catalog", lambda _tenant: catalogue)
    monkeypatch.setattr(line_runner_module, "_worker_is_configured", lambda: False)

    runner_package_root = tmp_path
    if catalogue_install:
        # A verified stored wheel is sufficient for API compilation; no package
        # distribution may be installed or allowlisted in the API process.
        monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", "")
        from app.services.catalogue import ensure_schema as ensure_catalogue_schema
        from app.services.runner_runtime_config import generate_runtime_config

        entry = {
            "id": integration_id, "version": "1.0.0", "kind": "package",
            "source": f"https://catalogue.example.test/artifacts/{wheel_filename}",
            "sha256": hashlib.sha256(wheel_bytes).hexdigest(), "lynewright_version": "^0.1.0",
            "permissions": {"capabilities": ["media:create"], "egress": "none", "media": "write", "credential_refs": []},
            "limits": {"cpus": 1, "memory_mb": 256, "pids": 64, "timeout_seconds": 60},
            "review": {"reviewer": "runner integration test", "date": "2026-10-16", "catalogue_commit": "139cceb8570bfb1ba5e8da70377223987abb02c1"},
        }
        index_bytes = json.dumps({
            "format": "lynewright-catalogue/1", "serial": 22, "generated_at": "2026-10-16T00:00:00Z",
            "name": "Runner fixture", "entries": [entry],
        }, sort_keys=True).encode()
        with store.connection() as db:
            ensure_catalogue_schema(db)
            db.execute(
                "UPDATE integration_catalogue_cache SET index_bytes=?,index_sha256=?,serial=22,"
                "generated_at='2026-10-16T00:00:00Z',verified_key_id='runner-fixture' WHERE id=1",
                (index_bytes, hashlib.sha256(index_bytes).hexdigest()),
            )
            db.commit()

        class _WheelArtifactHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != f"/artifacts/{wheel_filename}":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(wheel_bytes)))
                self.end_headers()
                self.wfile.write(wheel_bytes)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        artifact_server = ThreadingHTTPServer(("127.0.0.1", 0), _WheelArtifactHandler)
        artifact_thread = threading.Thread(target=artifact_server.serve_forever, daemon=True)
        artifact_thread.start()
        try:
            monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "catalogue-only")
            monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_URL", "https://catalogue.example.test/catalogue.json")
            monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_INTERNAL_ENDPOINT_REF", "k55_runner_artifact")
            monkeypatch.setenv(
                "LYNEWRIGHT_INTERNAL_ENDPOINT__K55_RUNNER_ARTIFACT",
                f"http://127.0.0.1:{artifact_server.server_port}",
            )
            from app.services.catalogue import CatalogueService
            from app.services.platform_services import HTTPService

            wheel_catalogue = CatalogueService(http_service=HTTPService())
            wheel_catalogue.get = lambda requested_id, requested_version: (
                entry if (requested_id, requested_version) == (integration_id, "1.0.0") else None
            )
            wheel_catalogue._cache_row = lambda: {"serial": 22, "verified_key_id": "runner-fixture"}
            monkeypatch.setattr(catalogue_router, "CatalogueService", lambda: wheel_catalogue)
            with TestClient(api) as client:
                installed_response = client.post(
                    f"/api/catalogue/entries/{integration_id}/1.0.0/install",
                    json={"internal_endpoint_ref": "k55_runner_artifact"},
                )
            assert installed_response.status_code == 201, installed_response.text
            installed = installed_response.json()["integration"]
        finally:
            artifact_server.shutdown()
            artifact_server.server_close()
            artifact_thread.join(timeout=5)
        assert installed["provenance"]["kind"] == "catalogue"
        assert not api_import_sentinel.exists()
        runtime_config = generate_runtime_config(store, registry.get(integration_id), format="compose", tenant_id=TENANT)
        assert str(tmp_path / "catalogue-packages") in runtime_config["content"]
        assert "The API image does not need this package" in " ".join(runtime_config["warnings"])
        runner_package_root = tmp_path / "runner-package"
        runner_package_root.mkdir()
        with zipfile.ZipFile(wheel_path) as archive:
            archive.extractall(runner_package_root)
    else:
        ensure_discovered_integrations_installed(registry)
        with TestClient(api) as client:
            required = client.post(
                f"/api/integration-management/{integration_id}/confirm-install",
                json={"confirm": None},
            )
            assert required.status_code == 409, required.text
            summary_hash = required.json()["detail"]["summary_hash"]
            confirmed = client.post(
                f"/api/integration-management/{integration_id}/confirm-install",
                json={"confirm": {"integration_id": integration_id, "summary_hash": summary_hash}},
            )
            assert confirmed.status_code == 200, confirmed.text
    registry.set_authorization(integration_id, permission_mode="custom", granted_capabilities=["media:create"])
    assert not api_import_sentinel.exists()
    compiled = compile_installed_definitions(registry)
    definition = next((item for item in compiled["definitions"] if item.integration_id == integration_id), None)
    assert definition is not None, compiled
    assert runtime.definitions_for(integration_id), "installation/confirmation must bootstrap the live definition"

    monkeypatch.setattr(integration_host, "store", store)
    monkeypatch.setattr(integration_host, "integrations", registry)
    monkeypatch.setattr(integration_host, "connections", connections)
    monkeypatch.setattr(integration_host, "contributions", contributions)
    monkeypatch.setattr(integration_host, "runtime", runtime)
    monkeypatch.setattr(operation_module, "build_tenant_operation_catalog", lambda _tenant: catalogue)
    monkeypatch.setattr(line_runner_module, "_worker_is_configured", lambda: False)

    with TestClient(api) as client:
        enabled = client.post(f"/api/integration-management/{integration_id}/enable")
        assert enabled.status_code == 200, enabled.text
        listed = client.get("/api/production-lines/operations", params={"project_id": project})
        assert listed.status_code == 200, listed.text
        available_items = listed.json()["items"]
        available = next(
            item for item in available_items
            if item["integration_id"] == integration_id and item["operation"] == "render"
        )
        assert any(
            item["integration_id"] == integration_id and item["operation"] == "status"
            for item in available_items
        )
        line_definition = {"stages": [{
            "id": "invoke", "kind": "async_operation", "operation_id": available["operation_id"],
            "contract": contract, "operation": "render",
            "args_template": {"prompt": "small blue landscape"},
        }]}
        saved = client.post("/api/production-lines", json={
            "id": "runner-async-line", "name": "Runner async line", "project_id": project,
            "definition": line_definition, "enabled": True,
        })
        assert saved.status_code == 201, saved.text
    api_socket = socket.socket()
    api_socket.bind(("127.0.0.1", 0))
    api_port = api_socket.getsockname()[1]
    api_socket.close()
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=api_port, log_level="critical", access_log=False))
    api_thread = threading.Thread(target=server.run, daemon=True)
    api_thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started

    media_root = tmp_path / "managed-media"
    media_root.mkdir()
    monkeypatch.setenv("LYNEWRIGHT_MANAGED_MEDIA_DIR", str(media_root))

    token = IntegrationRunnerService(store, tenant_id=TENANT).create_or_rotate(integration_id)["token"]
    runner_env = {key: value for key, value in os.environ.items() if not key.startswith((
        "LYNEWRIGHT_DB_PATH", "LYNEWRIGHT_DATABASE_URL", "LYNEWRIGHT_DATA_DIR", "LYNEWRIGHT_MANAGED_MEDIA_DIR",
    ))}
    runner_env.update({
        "LYNEWRIGHT_RUNNER_API_URL": f"http://127.0.0.1:{api_port}",
        "LYNEWRIGHT_RUNNER_TOKEN": token,
        "LYNEWRIGHT_RUNNER_INTEGRATION_ID": integration_id,
        "LYNEWRIGHT_RUNNER_MANIFEST": f"{module}.manifest",
        "LYNEWRIGHT_INTEGRATION_PACKAGES": distribution,
        "PYTHONPATH": os.pathsep.join((str(BACKEND), str(runner_package_root))),
    })
    child = subprocess.Popen(
        [sys.executable, "-u", str(RUNNER)], cwd=BACKEND, env=runner_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        runner_service = IntegrationRunnerService(store, tenant_id=TENANT)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and runner_service.status(integration_id)["status"] != "connected":
            if child.poll() is not None:
                break
            time.sleep(0.05)
        assert runner_service.status(integration_id)["status"] == "connected", (
            child.poll(), child.stderr.readline() if child.poll() is not None else "runner did not heartbeat",
        )
        with TestClient(api) as client:
            started_response = client.post(
                "/api/production-lines/runner-async-line/runs",
                json={"project_id": project},
            )
            assert started_response.status_code == 202, started_response.text
            started = started_response.json()
            deadline = time.monotonic() + 20
            completed_response = client.get(f"/api/production-lines/runs/{started['id']}")
            while time.monotonic() < deadline:
                assert completed_response.status_code == 200, completed_response.text
                completed = completed_response.json()
                if completed["status"] in {"done", "failed", "cancelled", "rejected"}:
                    break
                time.sleep(0.03)
                completed_response = client.get(f"/api/production-lines/runs/{started['id']}")
            completed = completed_response.json()
        with store.connection() as db:
            queued_rows = [dict(row) for row in db.execute(
                "SELECT id,status,error,operation,contribution_id FROM integration_worker_invocations ORDER BY created_at",
            ).fetchall()]
            tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            execution_rows = [dict(row) for row in db.execute(
                "SELECT * FROM platform_integration_executions ORDER BY started_at",
            ).fetchall()] if "platform_integration_executions" in tables else []
            step_rows = [dict(row) for row in db.execute("SELECT * FROM contract_step_runs") .fetchall()] if "contract_step_runs" in tables else []
        assert completed["status"] == "done", json.dumps({
            "run": completed, "queue": queued_rows, "executions": execution_rows,
            "steps": step_rows,
        }, default=str)
        assert completed["stages"][0]["output"]["status"] == "done"
        artifact = completed["stages"][0]["output"]["media_refs"][0]
        stored_artifact = MediaService(store, tenant_id=TENANT).get(artifact["media_id"])
        assert stored_artifact["project_id"] == project
        output_bytes = (media_root / stored_artifact["metadata"]["stored_as"]).read_bytes()
        assert output_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(output_bytes) == completed["stages"][0]["output"]["bytes"]
        imported_by = [int(line) for line in provider_imports.read_text(encoding="utf-8").splitlines()]
        assert imported_by and all(process_id != os.getpid() for process_id in imported_by)
        assert any(event.get("detail", {}).get("phase") == "poll-1" for event in completed["timeline"] if event["type"] == "stage.queued")
    finally:
        child.terminate()
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
        server.should_exit = True
        api_thread.join(timeout=5)
        for db in opened:
            db.close()
