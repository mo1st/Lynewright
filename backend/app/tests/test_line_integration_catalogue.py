from __future__ import annotations

import base64
import json
import hashlib
import importlib
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.control_plane_registry import CapabilityDescriptor, ControlPlaneRegistry
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_file import IntegrationFileService
from app.services.integration_drafts import IntegrationDraftService
from app.services.integration_lifecycle import IntegrationLifecycleRuntime
from app.services.integration_management import IntegrationManagementService
from app.services.integration_operations import IntegrationOperationCatalog
from app.services.integration_registry import IntegrationRegistry
from app.services.line_engine import ProductionLineEngine
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import HTTPService, MediaService, PlatformOperationError, PlatformStore, PlatformValidationError
from app.services.operation_trace import OperationTraceService
from app.services.production_integrations import (
    PACKAGE_ENTRY_POINT_GROUP,
    compile_installed_definitions,
    discover_shipped_manifests,
    ensure_discovered_integrations_installed,
    integration_package_statuses,
)


FIXTURES = Path(__file__).parent / "fixtures" / "integration_files"
PROJECT = "line-dispatch-project"
TARGET_CONTRACT = "lynewright.fixture.line"
TARGET_OPERATION = "echo"
TARGET_OUTPUT = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _legacy_line_import_tests_use_open_install_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this suite focused on line dispatch, with policy behavior tested separately."""
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "open")
    monkeypatch.setattr("app.services.application_logging.log_event", lambda **_event: None)


class _LineStub(BaseHTTPRequestHandler):
    def _send(self, value: dict[str, Any]) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/openapi.json":
            host, port = self.server.server_address[:2]
            self._send({
                "openapi": "3.0.3",
                "info": {"title": "Drafted line fixture", "version": "1.0.0"},
                "servers": [{"url": f"http://{host}:{port}"}],
                "paths": {"/health": {"get": {"operationId": "health", "summary": "Health check",
                    "responses": {"200": {"description": "ok", "content": {"application/json": {
                        "schema": {"type": "object", "properties": {"status": {"type": "string"}},
                                   "required": ["status"], "additionalProperties": False}}}}}}}},
            })
        elif parsed.path == "/health":
            self._send({"status": "ok"})
        elif parsed.path == "/media-source":
            self._send(self.server.media_source)  # type: ignore[attr-defined]
        elif parsed.path == "/status":
            polls = self.server.polls  # type: ignore[attr-defined]
            job_id = parse_qs(parsed.query).get("job_id", [""])[0]
            polls[job_id] = polls.get(job_id, 0) + 1
            self._send({"job_id": job_id, "status": "pending" if polls[job_id] == 1 else "done", "result": "finished"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        raw = self._read_request_body()
        if self.path == "/upload":
            content_type = str(self.headers.get("Content-Type") or "")
            message = BytesParser(policy=email_policy).parsebytes(
                b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii")
                + b"\r\n\r\n" + raw,
            )
            uploaded = next((part for part in message.iter_parts() if part.get_filename()), None)
            if uploaded is None:
                self.send_error(400)
                return
            body = uploaded.get_payload(decode=True) or b""
            self._send({
                "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                "content_type": str(uploaded.get_content_type()),
            })
            return
        request = json.loads(raw or b"{}")
        if self.path == "/start":
            self.server.submits += 1  # type: ignore[attr-defined]
            self._send({"job_id": f"job-{self.server.submits}", "status": "accepted"})  # type: ignore[attr-defined]
            return
        method = request.get("method")
        if method == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "line-stub", "version": "1"}}
        elif method == "notifications/initialized":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "Echo input", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]}
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "tools/call":
            result = {"structuredContent": {"value": str((request.get("params") or {}).get("arguments", {}).get("query") or "ok")}}
        else:
            result = {}
        self._send({"jsonrpc": "2.0", "id": request.get("id"), "result": result})

    def _read_request_body(self) -> bytes:
        if str(self.headers.get("Transfer-Encoding") or "").lower() != "chunked":
            return self.rfile.read(int(self.headers.get("Content-Length", "0")))
        chunks: list[bytes] = []
        while True:
            line = self.rfile.readline().split(b";", 1)[0].strip()
            size = int(line, 16)
            if size == 0:
                while self.rfile.readline().strip():
                    pass
                break
            chunks.append(self.rfile.read(size))
            self.rfile.read(2)
        return b"".join(chunks)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _stub() -> Iterator[tuple[str, ThreadingHTTPServer]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LineStub)
    server.polls = {}  # type: ignore[attr-defined]
    server.submits = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@contextmanager
def _template_openapi_stub() -> Iterator[str]:
    """Run the pinned OpenAPI starter app on a local socket for K5.5 dispatch."""
    import importlib.util
    import socket
    import uvicorn

    source = Path(__file__).parent / "fixtures" / "catalogue-k55" / "openapi_example_app.py"
    spec = importlib.util.spec_from_file_location("lynewright_k55_template_openapi", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("Pinned template OpenAPI example fixture could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(uvicorn.Config(
        module.app, host="127.0.0.1", port=port, log_level="critical", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("Pinned template OpenAPI example did not start.")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _manager(tmp_path: Path) -> tuple[IntegrationManagementService, list[sqlite3.Connection]]:
    opened: list[sqlite3.Connection] = []
    database_path = tmp_path / "integration-line.sqlite3"

    def connect() -> sqlite3.Connection:
        db = sqlite3.connect(database_path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        opened.append(db)
        return db

    store = PlatformStore(connect)
    integrations = IntegrationRegistry(store)
    connections = ConnectionProfileService(store, registry=integrations, endpoint_validator=lambda _url: {})
    contributions = ContributionRegistry(integrations, connections=connections)
    control_plane = ControlPlaneRegistry()
    control_plane.capabilities.register(CapabilityDescriptor(
        id="fixture.line.echo", version="1.0.0", contract_id=TARGET_CONTRACT,
        operation=TARGET_OPERATION, name="Fixture echo", description="Local line test operation",
        required_actor_capability="network:use", risk_class="LOW_RISK",
        project_scope="required", resource_scope="none", connection_scope="connection",
        output_schema=TARGET_OUTPUT,
    ))
    manager = IntegrationManagementService(
        integrations, connections, contributions, control_plane=control_plane,
    )
    return manager, opened


def _close(connections: list[sqlite3.Connection]) -> None:
    for connection in connections:
        connection.close()


def _authority() -> CapabilityActor:
    return CapabilityActor(
        client_id="line-test-authority", name="Line test authority", client_type="automation",
        permission_mode="full", granted_capabilities=frozenset(),
    )


def _import(manager: IntegrationManagementService, name: str, endpoint: str) -> dict[str, Any]:
    value = IntegrationFileService.parse((FIXTURES / name).read_bytes())
    value["endpoint_url"] = endpoint + ("/mcp" if value["runtime_kind"] == "mcp" else "")
    imported = IntegrationFileService(manager).import_value(
        value, artifact_bytes=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )
    detail = imported["integration"]
    return {"id": detail["integration"]["id"], "detail": detail, "file": value}


def _authorize(manager: IntegrationManagementService, integration_id: str) -> None:
    manager.integrations.set_authorization(
        integration_id, permission_mode="custom", granted_capabilities=["network:use"],
    )


def _local_connection(
    manager: IntegrationManagementService, integration_id: str, *, openapi: bool = False,
    project_id: str | None = PROJECT,
) -> str:
    connection = manager.connections.create(
        integration_id, connection_id="line-local", name="Line local", project_id=project_id,
        origin_kind="internal_endpoint", internal_endpoint_ref="line_stub",
        configuration={"openapi": {"approved_operations": ["health", "submit", "status"]}} if openapi else {"mcp": {"approved_tools": []}},
    )
    return str(connection["id"])


def _catalog(manager: IntegrationManagementService, monkeypatch: pytest.MonkeyPatch) -> IntegrationOperationCatalog:
    import app.services.integration_operations as operation_module
    import app.services.line_engine as line_engine_module
    import app.services.line_runner as line_runner_module

    catalogue = IntegrationOperationCatalog(manager)
    monkeypatch.setattr(operation_module, "build_tenant_operation_catalog", lambda _tenant: catalogue)
    engine = ProductionLineEngine(store=manager.store, tenant_id=manager.tenant_id)
    monkeypatch.setattr(line_engine_module, "ProductionLineEngine", lambda tenant_id: engine)
    monkeypatch.setattr(line_runner_module, "_worker_is_configured", lambda: False)
    return catalogue


def _save_and_start(
    manager: IntegrationManagementService, catalogue: IntegrationOperationCatalog,
    definition: dict[str, Any], *, project_id: str = PROJECT,
) -> tuple[ProductionLineEngine, dict[str, Any]]:
    engine = ProductionLineEngine(store=manager.store, tenant_id=manager.tenant_id)
    engine.db.put_line(
        "catalogue-line", name="Catalogue line", project_id=project_id,
        definition=definition, enabled=True, actor_id="test",
        operation_directory=catalogue, operation_actor=_authority(), create_only=True,
    )
    return engine, engine.start("catalogue-line", variables={}, project_id=project_id, actor={"user_id": "test"})


def _wait(engine: ProductionLineEngine, run_id: str, *, timeout: float = 8) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = engine.get_run(run_id)
        if run["status"] in {"done", "failed", "cancelled", "rejected"}:
            return run
        time.sleep(0.02)
    return engine.get_run(run_id)


def _stage(item: dict[str, Any], *, kind: str = "operation", args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": "invoke", "kind": kind, "operation_id": item["operation_id"],
        "contract": item["contract"], "operation": item["operation"],
        "args_template": args or {},
    }


def _mapped_trace_count(manager: IntegrationManagementService) -> int:
    traces = OperationTraceService(manager.store, tenant_id=manager.tenant_id)
    with manager.store.connection() as db:
        ids = [str(row[0]) for row in db.execute(
            "SELECT trace_id FROM platform_operation_traces WHERE tenant_id=? AND project_id=?",
            (manager.tenant_id, PROJECT),
        ).fetchall()]
    return len(ids)


def _native_package(
    root: Path, *, distribution: str, module: str, integration_id: str,
    distribution_version: str = "1.0.0", compatibility: str = "^0.1.0",
    manifest_version: str = "1.0.0", manifest_marker: Path | None = None,
    provider_marker: Path | None = None, include_entrypoint: bool = True,
) -> Path:
    """Write an isolated .dist-info package fixture; install nothing globally."""
    package = root / module
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    contract = f"lynewright.fixture.{module.replace('_', '-')}.writer"
    manifest = {
        "schema_version": "1.2", "id": integration_id, "name": "Fixture Native Writer",
        "version": manifest_version, "type": "native", "lynewright_version": compatibility,
        "sdk_version": "1.0.0", "requested_capabilities": ["system:read"],
        "contributions": ["services", "settings"],
        "settings_schema": {"schema_id": "fixture_writer", "fields": [{"key": "tone", "label": "Tone", "type": "text", "default": "plain"}]},
        "provides": [{
            "contract": contract, "implementation": f"{module}.provider:FixtureProvider",
            "version": "1.0.0", "capabilities": ["run"],
            "operation_schemas": {"run": {
                "type": "object", "properties": {"value": {"type": "string"}},
                "required": ["value"], "additionalProperties": False,
                "caller_capability": "system:read", "read_only": True,
                "risk_class": "READ_ONLY", "description": "Run the fixture writer.",
            }},
        }],
    }
    manifest_side_effect = (
        f"from pathlib import Path\nPath({str(manifest_marker)!r}).write_text('imported', encoding='utf-8')\n"
        if manifest_marker else ""
    )
    (package / "manifest.py").write_text(
        f"{manifest_side_effect}FIXTURE_MANIFEST = {manifest!r}\n", encoding="utf-8",
    )
    (package / "lynewright-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    provider_side_effect = (
        f"from pathlib import Path\nPath({str(provider_marker)!r}).write_text('imported', encoding='utf-8')\n"
        if provider_marker else ""
    )
    (package / "provider.py").write_text(
        provider_side_effect + """
class FixtureProvider:
    operation_schemas = {
        "run": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
            "caller_capability": "system:read",
            "read_only": True,
            "risk_class": "READ_ONLY",
            "description": "Return the supplied fixture value.",
        },
    }

    def invoke(self, operation, payload, *, invocation_context=None):
        if operation != "run":
            raise ValueError("Unsupported fixture operation.")
        return {"value": payload["value"]}
""", encoding="utf-8",
    )
    (package / "settings.py").write_text(
        "SETTINGS_SCHEMA = {'schema_id': 'fixture_writer', 'fields': [{'key': 'tone', 'label': 'Tone', 'type': 'text', 'default': 'plain'}]}\n",
        encoding="utf-8",
    )
    info = root / f"{distribution.replace('-', '_')}-{distribution_version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {distribution_version}\n\n",
        encoding="utf-8",
    )
    if include_entrypoint:
        (info / "entry_points.txt").write_text(
            f"[{PACKAGE_ENTRY_POINT_GROUP}]\nfixture = {module}.manifest\n", encoding="utf-8",
        )
    listed_files = [
        f"{module}/{path.name}" for path in package.iterdir() if path.is_file()
    ] + [f"{info.name}/METADATA", f"{info.name}/entry_points.txt"]
    (info / "RECORD").write_text(
        "".join(f"{name},,\n" for name in listed_files if not name.endswith("/entry_points.txt") or include_entrypoint)
        + f"{info.name}/RECORD,,\n",
        encoding="utf-8",
    )
    return info / "METADATA"


def test_unlisted_python_distribution_is_invisible_and_its_manifest_is_never_imported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    module = "k42_unlisted_fixture"
    integration_id = "lynewright.integration.fixture-unlisted"
    manifest_marker = tmp_path / "manifest-imported"
    provider_marker = tmp_path / "provider-imported"
    _native_package(
        tmp_path, distribution="lynewright-integration-unlisted", module=module,
        integration_id=integration_id, manifest_marker=manifest_marker,
        provider_marker=provider_marker,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", "")
    importlib.invalidate_caches()

    assert all(item.manifest.get("id") != integration_id for item in discover_shipped_manifests())
    assert not manifest_marker.exists()
    assert not provider_marker.exists()


def test_listed_native_package_is_installed_shown_and_runs_in_a_production_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    module = "k42_line_fixture"
    distribution = "lynewright-integration-line-fixture"
    integration_id = "lynewright.integration.fixture-line"
    manifest_marker = tmp_path / "manifest-imported"
    provider_marker = tmp_path / "provider-imported"
    _native_package(
        tmp_path, distribution=distribution, module=module,
        integration_id=integration_id, manifest_marker=manifest_marker,
        provider_marker=provider_marker,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", distribution)
    monkeypatch.setenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", "1")
    importlib.invalidate_caches()

    manager, opened = _manager(tmp_path)
    outcome = ensure_discovered_integrations_installed(manager.integrations)
    assert outcome[integration_id] == "installed"
    assert not manifest_marker.exists(), "data-only package discovery must not import manifest code"
    assert not provider_marker.exists(), "installation must not import the implementation"
    detail = manager.detail(integration_id)
    assert detail["integration"]["source"] == "package"
    assert detail["integration"]["source_distribution"] == distribution
    assert detail["integration"]["source_version"] == "1.0.0"
    assert detail["integration"]["trusted_inprocess"] is True
    assert detail["provenance"]["kind"] == "outside"
    assert detail["provenance"]["source"].startswith(f"package:{distribution}==1.0.0; installed-files-sha256:")
    assert len(detail["provenance"]["source"].rsplit(":", 1)[-1]) == 64
    assert detail["availability"]["package_status"] == "available"
    assert detail["management"]["settings_schema"]["fields"][0]["key"] == "tone"
    assert not provider_marker.exists(), "discovery and settings load must not import the implementation"

    compiled = compile_installed_definitions(manager.integrations)
    definition = next((item for item in compiled["definitions"] if item.integration_id == integration_id), None)
    assert definition is not None, compiled
    assert provider_marker.exists(), "compilation is the implementation import boundary"
    runtime = IntegrationLifecycleRuntime(manager.integrations, manager.contributions)
    runtime.register_definition(definition)
    manager.integrations.enable(integration_id)
    runtime.activate(integration_id)

    catalogue = _catalog(manager, monkeypatch)
    available = next(item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                     if item["integration_id"] == integration_id)
    engine, started = _save_and_start(
        manager, catalogue,
        {"stages": [_stage(available, args={"value": "from package"})]},
    )
    completed = _wait(engine, started["id"])
    assert completed["status"] == "done", completed
    assert completed["stages"][0]["output"] == {"value": "from package"}

    # Removing the distribution from the allowlist hides the same durable
    # contribution at save and run time, without deleting its integration row.
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", "")
    with pytest.raises(PlatformValidationError, match="unavailable"):
        engine.db.put_line(
            "package-delisted-save", name="Delisted", project_id=PROJECT,
            definition={"stages": [_stage(available, args={"value": "blocked"})]},
            enabled=True, actor_id="test", operation_directory=catalogue,
            operation_actor=_authority(), create_only=True,
        )
    failed = engine.start("catalogue-line", variables={}, project_id=PROJECT, actor={"user_id": "test"})
    failed = _wait(engine, failed["id"])
    assert failed["status"] == "failed", failed
    assert manager.integrations.get(integration_id)["source"] == "package"

    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", distribution)
    restored = engine.start("catalogue-line", variables={}, project_id=PROJECT, actor={"user_id": "test"})
    assert _wait(engine, restored["id"])["status"] == "done"
    _close(opened)


def test_package_data_manifest_supports_api_flows_without_importing_package_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.services.integration_lifecycle import IntegrationLifecycleRuntime
    from app.services.production_integrations import compile_installed_definitions
    from app.services.runner_runtime_config import generate_runtime_config

    module = "k53_data_manifest_fixture"
    distribution = "lynewright-integration-data-manifest-fixture"
    integration_id = "lynewright.integration.fixture-data-manifest"
    sentinel = tmp_path / "package-imported"
    _native_package(tmp_path, distribution=distribution, module=module, integration_id=integration_id)
    package = tmp_path / module
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('imported')\nraise RuntimeError('package imported')\n",
        encoding="utf-8",
    )
    (package / "manifest.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('manifest imported')\nraise RuntimeError('manifest imported')\n",
        encoding="utf-8",
    )
    # The real package file list comes from wheel RECORD; refresh the local
    # dist-info RECORD after replacing the sentinel modules.
    info = next(tmp_path.glob("*.dist-info"))
    files = [f"{module}/{path.name}" for path in package.iterdir() if path.is_file()]
    (info / "RECORD").write_text(
        "".join(f"{name},,\n" for name in files)
        + f"{info.name}/METADATA,,\n{info.name}/entry_points.txt,,\n{info.name}/RECORD,,\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", distribution)
    monkeypatch.delenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", raising=False)
    importlib.invalidate_caches()

    manager, opened = _manager(tmp_path)
    assert ensure_discovered_integrations_installed(manager.integrations)[integration_id] == "installed"
    manager.integrations.set_authorization(integration_id, permission_mode="custom", granted_capabilities=["system:read"])
    compiled = compile_installed_definitions(manager.integrations)
    runtime = IntegrationLifecycleRuntime(manager.integrations, manager.contributions)
    for definition in compiled["definitions"]:
        runtime.register_definition(definition)
    manager.integrations.enable(integration_id)
    runtime.activate(integration_id)
    detail = manager.detail(integration_id)
    assert detail["management"]["settings_schema"]["fields"][0]["key"] == "tone"
    assert not detail["integration"]["trusted_inprocess"]
    catalogue = _catalog(manager, monkeypatch)
    operation = next(item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                     if item["integration_id"] == integration_id and item["operation"] == "run")
    engine = ProductionLineEngine(store=manager.store, tenant_id=manager.tenant_id)
    engine.db.put_line(
        "data-manifest-line", name="Data manifest line", project_id=PROJECT,
        definition={"stages": [_stage(operation, args={"value": "valid"})]}, enabled=True,
        actor_id="test", operation_directory=catalogue, operation_actor=_authority(), create_only=True,
    )
    config = generate_runtime_config(
        manager.store, manager.integrations.get(integration_id), format="compose", tenant_id=manager.tenant_id,
    )
    assert "lynewright-manifest.json" in " ".join(config["warnings"])
    assert not sentinel.exists()
    _close(opened)


def test_pep660_editable_manifest_is_read_from_source_data_without_importing_package_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    module = "k553_editable_manifest_fixture"
    distribution = "lynewright-integration-editable-manifest-fixture"
    integration_id = "lynewright.integration.fixture-editable-manifest"
    sentinel = tmp_path / "package-imported"
    _native_package(
        tmp_path, distribution=distribution, module=module,
        integration_id=integration_id, manifest_marker=sentinel,
    )

    # PEP 660 metadata lives in site-packages, while package data stays in the
    # editable source tree. The API must resolve direct_url.json without
    # importing either __init__.py or manifest.py.
    metadata_root = tmp_path / "editable-site-packages"
    metadata_root.mkdir()
    info = next(tmp_path.glob("*.dist-info"))
    installed_info = info.rename(metadata_root / info.name)
    (installed_info / "direct_url.json").write_text(
        json.dumps({"url": tmp_path.as_uri(), "dir_info": {"editable": True}}),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(metadata_root))
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", distribution)
    monkeypatch.delenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", raising=False)
    importlib.invalidate_caches()

    manifests = discover_shipped_manifests()
    assert any(item.manifest.get("id") == integration_id for item in manifests)
    assert not sentinel.exists()


def test_listed_native_package_defaults_to_container_and_inprocess_is_explicit_optin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    module = "k53_default_container_fixture"
    distribution = "lynewright-integration-default-container"
    integration_id = "lynewright.integration.fixture-default-container"
    manifest_marker = tmp_path / "manifest-imported"
    provider_marker = tmp_path / "provider-imported"
    _native_package(
        tmp_path, distribution=distribution, module=module,
        integration_id=integration_id, manifest_marker=manifest_marker,
        provider_marker=provider_marker,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", distribution)
    monkeypatch.delenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", raising=False)
    importlib.invalidate_caches()

    manager, opened = _manager(tmp_path)
    assert ensure_discovered_integrations_installed(manager.integrations)[integration_id] == "installed"
    compiled = compile_installed_definitions(manager.integrations)
    definition = next(item for item in compiled["definitions"] if item.integration_id == integration_id)
    runtime = IntegrationLifecycleRuntime(manager.integrations, manager.contributions)
    runtime.register_definition(definition)
    manager.integrations.enable(integration_id)
    runtime.activate(integration_id)
    contribution = manager.contributions.list(integration_id=integration_id)[0]
    assert contribution.metadata["execution_plane"] == "container"
    assert not provider_marker.exists(), "default container compilation must not import provider code"
    detail = manager.detail(integration_id)
    assert detail["integration"]["execution_plane"] == "container"
    assert detail["integration"]["trusted_inprocess"] is False

    monkeypatch.setenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", "1")
    optin_root = tmp_path / "optin"
    optin_root.mkdir()
    optin_manager, optin_opened = _manager(optin_root)
    assert ensure_discovered_integrations_installed(optin_manager.integrations)[integration_id] == "installed"
    compiled_inprocess = compile_installed_definitions(optin_manager.integrations)
    inprocess_definition = next(
        item for item in compiled_inprocess["definitions"] if item.integration_id == integration_id
    )
    optin_runtime = IntegrationLifecycleRuntime(optin_manager.integrations, optin_manager.contributions)
    optin_runtime.register_definition(inprocess_definition)
    optin_manager.integrations.enable(integration_id)
    optin_runtime.activate(integration_id)
    inprocess = optin_manager.contributions.list(integration_id=integration_id)[0]
    assert inprocess.metadata["execution_plane"] == "direct"
    assert provider_marker.exists(), "explicit opt-in compiles provider code in process"
    detail = optin_manager.detail(integration_id)
    assert detail["integration"]["trusted_inprocess"] is True
    assert "running in-process by explicit opt-in" in caplog.text
    _close(opened)
    _close(optin_opened)


def test_incompatible_package_has_visible_status_and_is_not_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    distribution = "lynewright-integration-incompatible"
    integration_id = "lynewright.integration.fixture-incompatible"
    _native_package(
        tmp_path, distribution=distribution, module="k42_incompatible_fixture",
        integration_id=integration_id, compatibility=">=99.0.0",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", distribution)
    manager, opened = _manager(tmp_path)
    assert all(item.manifest.get("id") != integration_id for item in discover_shipped_manifests("0.1.0"))
    statuses = integration_package_statuses(manager.integrations, lynewright_version="0.1.0")
    status = next(item for item in statuses if item.get("integration_id") == integration_id)
    assert status["status"] == "incompatible"
    assert integration_id not in {item["id"] for item in manager.integrations.list()}
    _close(opened)


def test_bundled_integration_wins_an_id_collision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    distribution = "lynewright-integration-bundled-collision"
    _native_package(
        tmp_path, distribution=distribution, module="k42_bundle_collision",
        integration_id="lynewright.integration.demo-writer",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("LYNEWRIGHT_DEMO", "1")
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", distribution)
    import sys
    sys.modules.pop("app.integrations.demo_writer.manifest", None)
    items = [item for item in discover_shipped_manifests("0.1.0") if item.manifest.get("id") == "lynewright.integration.demo-writer"]
    assert len(items) == 1 and items[0].source == "bundled"
    status = next(item for item in integration_package_statuses() if item.get("distribution") == distribution)
    assert status["status"] == "id_collision"


def test_two_selected_distributions_with_the_same_id_are_both_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    integration_id = "lynewright.integration.fixture-duplicate"
    distributions = ["lynewright-integration-duplicate-a", "lynewright-integration-duplicate-b"]
    for index, distribution in enumerate(distributions):
        _native_package(
            tmp_path, distribution=distribution, module=f"k42_duplicate_{index}",
            integration_id=integration_id,
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", ",".join(distributions))
    assert all(item.manifest.get("id") != integration_id for item in discover_shipped_manifests("0.1.0"))
    statuses = [item for item in integration_package_statuses() if item.get("integration_id") == integration_id]
    assert {item["distribution"] for item in statuses} == set(distributions)
    assert all(item["status"] == "id_collision" for item in statuses)


def test_distribution_version_bump_reconciles_the_definition_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.services.integration_activation import _reconcile_compatible_deployed_definitions

    distribution = "lynewright-integration-upgrade-fixture"
    integration_id = "lynewright.integration.fixture-upgrade"
    metadata_path = _native_package(
        tmp_path, distribution=distribution, module="k42_upgrade_fixture",
        integration_id=integration_id,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", distribution)
    manager, opened = _manager(tmp_path)
    ensure_discovered_integrations_installed(manager.integrations)
    first_compile = compile_installed_definitions(manager.integrations)
    first = next((item for item in first_compile["definitions"] if item.integration_id == integration_id), None)
    assert first is not None, first_compile
    runtime = IntegrationLifecycleRuntime(manager.integrations, manager.contributions)
    runtime.register_definition(first)
    manager.integrations.enable(integration_id)
    runtime.activate(integration_id)
    before = runtime.identities.active(integration_id)

    metadata_path.write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 1.1.0\n\n", encoding="utf-8",
    )
    importlib.invalidate_caches()
    ensure_discovered_integrations_installed(manager.integrations)
    second = next(item for item in compile_installed_definitions(manager.integrations)["definitions"] if item.integration_id == integration_id)
    assert second.manifest_fingerprint != first.manifest_fingerprint
    # Model a process restart: the prior activation lock persists, while its
    # old Python definition is no longer registered in the new process.
    runtime.deactivate(integration_id)
    runtime = IntegrationLifecycleRuntime(manager.integrations, manager.contributions)
    runtime.register_definition(second)
    outcome = _reconcile_compatible_deployed_definitions(runtime, [integration_id], source="package-upgrade-test")
    assert [item["integration_id"] for item in outcome["auto_upgraded"]] == [integration_id], outcome
    assert runtime.identities.active(integration_id) != before
    assert manager.integrations.get(integration_id)["source_version"] == "1.1.0"
    _close(opened)


def test_listed_missing_distribution_and_distribution_without_entry_point_are_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    distribution = "lynewright-integration-no-entrypoint"
    _native_package(
        tmp_path, distribution=distribution, module="k42_no_entry_fixture",
        integration_id="lynewright.integration.fixture-no-entry", include_entrypoint=False,
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    missing = "lynewright-integration-not-installed"
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", f"{missing},{distribution}")
    statuses = integration_package_statuses()
    assert next(item for item in statuses if item["distribution"] == missing)["status"] == "not_installed"
    assert next(item for item in statuses if item["distribution"] == distribution)["status"] == "no_entry_point"


def test_imported_openapi_example_runs_inside_line_and_is_traced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _stub() as (endpoint, _server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        imported = _import(manager, "openapi.yaml", endpoint)
        integration_id = imported["id"]
        _authorize(manager, integration_id)
        _local_connection(manager, integration_id, openapi=True)
        manager.integrations.enable(integration_id)
        catalogue = _catalog(manager, monkeypatch)
        operation = next(item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                         if item["connection_id"] == "line-local" and item["operation"] == "health")
        engine, started = _save_and_start(manager, catalogue, {"stages": [_stage(operation)]})
        completed = _wait(engine, started["id"])
        assert completed["status"] == "done", completed
        assert completed["stages"][0]["output"] == {"status": "ok"}
        assert _mapped_trace_count(manager) == 1
        _close(db)


def test_openapi_import_without_enabled_is_not_approved_and_line_save_explains_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    with _stub() as (endpoint, _server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        value = IntegrationFileService.parse((FIXTURES / "openapi.yaml").read_bytes())
        value["endpoint_url"] = endpoint
        value["operations"][0].pop("enabled")
        imported = IntegrationFileService(manager).import_value(
            value, artifact_bytes=json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        assert imported["approval_message"] == "0 operations approved; approve them in Integrations."
        integration_id = value["id"]
        _authorize(manager, integration_id)
        manager.connections.create(
            integration_id, connection_id="line-local", name="Line local", project_id=PROJECT,
            origin_kind="internal_endpoint", internal_endpoint_ref="line_stub",
            configuration={"openapi": {"approved_operations": []}},
        )
        manager.integrations.enable(integration_id)
        catalogue = _catalog(manager, monkeypatch)
        engine = ProductionLineEngine(store=manager.store, tenant_id=manager.tenant_id)
        definition = {"stages": [{
            "id": "invoke", "kind": "operation", "contract": "lynewright.openapi",
            "operation": "health", "args_template": {},
        }]}
        with pytest.raises(PlatformValidationError, match="not approved"):
            engine.db.put_line(
                "unapproved-openapi-line", name="Unapproved OpenAPI line", project_id=PROJECT,
                definition=definition, enabled=True, actor_id="test",
                operation_directory=catalogue, operation_actor=_authority(), create_only=True,
            )
        _close(db)


def test_drafted_openapi_probe_enable_then_runs_in_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _stub() as (endpoint, _server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        with manager.store.connection() as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS tenant_projects(
                project_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active')""")
            connection.execute(
                "INSERT OR IGNORE INTO tenant_projects(project_id,tenant_id,name,status) VALUES(?,?,?,?)",
                (PROJECT, manager.tenant_id, "Draft line project", "active"),
            )
        drafts = IntegrationDraftService(
            manager, project_id=PROJECT, actor_id="draft-test", http=HTTPService(),
        )
        draft = drafts.create(
            source_kind="openapi_url", url=endpoint + "/openapi.json",
            internal_endpoint_ref="line_stub",
        )
        definition = dict(draft["draft_json"])
        definition["operations"] = [{**definition["operations"][0], "enabled": True}]
        draft = drafts.update(draft["id"], definition)
        draft = drafts.probe(draft["id"], operation_id="health", arguments={})
        assert draft["status"] == "probed"
        enabled = drafts.enable(draft["id"])
        assert enabled["status"] == "enabled"
        integration_id = enabled["enabled_integration_id"]

        catalogue = _catalog(manager, monkeypatch)
        operation = next(item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                         if item["integration_id"] == integration_id and item["operation"] == "health")
        engine, started = _save_and_start(manager, catalogue, {"stages": [_stage(operation)]}, project_id=PROJECT)
        completed = _wait(engine, started["id"])
        assert completed["status"] == "done", completed
        assert completed["stages"][0]["output"] == {"status": "ok"}
        _close(db)


def test_imported_openapi_contract_mapping_runs_inside_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _stub() as (endpoint, _server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        imported = _import(manager, "openapi.yaml", endpoint)
        integration_id = imported["id"]
        _authorize(manager, integration_id)
        _local_connection(manager, integration_id, openapi=True)
        manager.create_openapi_contract_mapping(integration_id, "line-local", {
            "mapping_id": "health-status", "operation_id": "health",
            "target_contract": TARGET_CONTRACT, "target_operation": TARGET_OPERATION,
            "argument_mapping": {}, "output_mapping": {"value": "status"},
        })
        manager.integrations.enable(integration_id)
        catalogue = _catalog(manager, monkeypatch)
        operation = next(item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                         if item["source"] == "openapi_contract_mapping")
        engine, started = _save_and_start(manager, catalogue, {"stages": [_stage(operation)]})
        completed = _wait(engine, started["id"])
        assert completed["status"] == "done", completed
        assert completed["stages"][0]["output"]["output"] == {"value": "ok"}
        assert _mapped_trace_count(manager) == 1
        _close(db)


def test_imported_mcp_example_runs_inside_line_and_is_traced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _stub() as (endpoint, _server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        imported = _import(manager, "mcp.yaml", endpoint)
        integration_id = imported["id"]
        _authorize(manager, integration_id)
        connection_id = _local_connection(manager, integration_id)
        manager.integrations.enable(integration_id)
        manager.discover(integration_id, connection_id)
        manager.approve_mcp_tools(integration_id, connection_id, ["echo"])
        manager.create_mcp_contract_mapping(integration_id, connection_id, {
            "mapping_id": "echo-value", "tool_name": "echo",
            "target_contract": TARGET_CONTRACT, "target_operation": TARGET_OPERATION,
            "argument_mapping": {"query": "query"}, "output_mapping": {"value": "value"},
            "enabled": True,
        })
        catalogue = _catalog(manager, monkeypatch)
        operation = next(item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                         if item["source"] == "mcp_contract_mapping")
        engine, started = _save_and_start(manager, catalogue, {"stages": [_stage(operation, args={"query": "hello"})]})
        completed = _wait(engine, started["id"])
        assert completed["status"] == "done", completed
        assert completed["stages"][0]["output"]["output"] == {"value": "hello"}
        assert _mapped_trace_count(manager) == 1
        _close(db)


def test_imported_openapi_async_file_polls_pending_then_done(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _stub() as (endpoint, server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        imported = _import(manager, "async-openapi.yaml", endpoint)
        integration_id = imported["id"]
        _authorize(manager, integration_id)
        _local_connection(manager, integration_id, openapi=True)
        manager.integrations.enable(integration_id)
        catalogue = _catalog(manager, monkeypatch)
        operation = next(item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                         if item["connection_id"] == "line-local" and item["operation"] == "submit")
        engine, started = _save_and_start(manager, catalogue, {"stages": [_stage(operation, kind="async_operation", args={"body": {"text": "hello"}})]})
        completed = _wait(engine, started["id"])
        assert completed["status"] == "done", json.dumps({
            "run": completed["status"], "stage": completed["stages"],
            "polls": server.polls, "timeline": completed["timeline"][-8:],
        }, default=str)
        assert completed["stages"][0]["output"]["status"] == "done"
        assert server.polls["job-1"] == 2
        assert any(event["detail"].get("phase") == "poll-1" for event in completed["timeline"] if event["type"] == "stage.queued")
        assert _mapped_trace_count(manager) == 3  # submit plus both status polls.
        _close(db)


def test_unapproved_mcp_mapping_is_rejected_at_save_and_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _stub() as (endpoint, _server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        integration_id = _import(manager, "mcp.yaml", endpoint)["id"]
        _authorize(manager, integration_id)
        connection_id = _local_connection(manager, integration_id)
        manager.integrations.enable(integration_id)
        manager.discover(integration_id, connection_id)
        manager.approve_mcp_tools(integration_id, connection_id, ["echo"])
        manager.create_mcp_contract_mapping(integration_id, connection_id, {
            "mapping_id": "echo-value", "tool_name": "echo",
            "target_contract": TARGET_CONTRACT, "target_operation": TARGET_OPERATION,
            "argument_mapping": {"query": "query"}, "output_mapping": {"value": "value"},
        })
        catalogue = _catalog(manager, monkeypatch)
        operation = next(item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                         if item["kind"] == "mcp")
        definition = {"stages": [_stage(operation, args={"query": "hello"})]}
        manager.approve_mcp_tools(integration_id, connection_id, [])
        engine = ProductionLineEngine(store=manager.store, tenant_id=manager.tenant_id)
        with pytest.raises(PlatformValidationError, match="unavailable"):
            engine.db.put_line("catalogue-line", name="Catalogue line", project_id=PROJECT,
                definition=definition, enabled=True, actor_id="test", operation_directory=catalogue,
                operation_actor=_authority(), create_only=True)
        manager.approve_mcp_tools(integration_id, connection_id, ["echo"])
        engine.db.put_line("catalogue-line", name="Catalogue line", project_id=PROJECT,
            definition=definition, enabled=True, actor_id="test", operation_directory=catalogue,
            operation_actor=_authority(), create_only=True)
        manager.approve_mcp_tools(integration_id, connection_id, [])
        failed = engine.start("catalogue-line", variables={}, project_id=PROJECT, actor={"user_id": "test"})
        assert failed["status"] == "failed", failed
        assert "unavailable" in failed["stages"][0]["error"].lower()
        _close(db)


def test_disabled_integration_and_connection_fail_closed_at_run_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _stub() as (endpoint, _server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        integration_id = _import(manager, "openapi.yaml", endpoint)["id"]
        _authorize(manager, integration_id)
        _local_connection(manager, integration_id, openapi=True)
        manager.integrations.enable(integration_id)
        catalogue = _catalog(manager, monkeypatch)
        operation = next(item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                         if item["connection_id"] == "line-local")
        engine, started = _save_and_start(manager, catalogue, {"stages": [_stage(operation)]})
        # The first run may finish before this test can disable the integration;
        # use a second definition and disable before starting it.
        assert _wait(engine, started["id"])["status"] == "done"
        engine.db.put_line("disabled-line", name="Disabled line", project_id=PROJECT,
            definition={"stages": [_stage(operation)]}, enabled=True, actor_id="test",
            operation_directory=catalogue, operation_actor=_authority(), create_only=True)
        engine.db.put_line("disabled-save-line", name="Disabled save line", project_id=PROJECT,
            definition={"stages": [_stage(operation)]}, enabled=True, actor_id="test",
            operation_directory=catalogue, operation_actor=_authority(), create_only=True)
        manager.connections.set_enabled(integration_id, "line-local", enabled=False)
        with pytest.raises(PlatformValidationError, match="unavailable"):
            engine.db.put_line("disabled-connection-save", name="Disabled connection save", project_id=PROJECT,
                definition={"stages": [_stage(operation)]}, enabled=True, actor_id="test",
                operation_directory=catalogue, operation_actor=_authority(), create_only=True)
        connection_failed = engine.start("disabled-line", variables={}, project_id=PROJECT, actor={"user_id": "test"})
        assert connection_failed["status"] == "failed" and "unavailable" in connection_failed["stages"][0]["error"].lower()
        manager.connections.set_enabled(integration_id, "line-local", enabled=True)
        manager.integrations.disable(integration_id)
        with pytest.raises(PlatformValidationError, match="unavailable"):
            engine.db.put_line("disabled-integration-save", name="Disabled integration save", project_id=PROJECT,
                definition={"stages": [_stage(operation)]}, enabled=True, actor_id="test",
                operation_directory=catalogue, operation_actor=_authority(), create_only=True)
        disabled = engine.start("disabled-save-line", variables={}, project_id=PROJECT, actor={"user_id": "test"})
        assert disabled["status"] == "failed" and "unavailable" in disabled["stages"][0]["error"].lower()
        _close(db)


def test_cross_project_connection_cannot_be_saved_or_resolved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _stub() as (endpoint, _server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        integration_id = _import(manager, "openapi.yaml", endpoint)["id"]
        _authorize(manager, integration_id)
        _local_connection(manager, integration_id, openapi=True, project_id="another-project")
        manager.integrations.enable(integration_id)
        catalogue = _catalog(manager, monkeypatch)
        operation = next(item for item in catalogue.operations_for_lines(_authority(), project_id="another-project")
                         if item["connection_id"] == "line-local")
        engine = ProductionLineEngine(store=manager.store, tenant_id=manager.tenant_id)
        with pytest.raises(PlatformValidationError, match="unavailable"):
            engine.db.put_line("cross-project-line", name="Cross project line", project_id=PROJECT,
                definition={"stages": [_stage(operation)]}, enabled=True, actor_id="test",
                operation_directory=catalogue, operation_actor=_authority(), create_only=True)
        _close(db)


def test_declarative_openapi_multipart_upload_runs_in_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    body = b"line media upload fixture\x00\xff"
    media_root = tmp_path / "managed-media"
    media_root.mkdir()
    monkeypatch.setenv("LYNEWRIGHT_MANAGED_MEDIA_DIR", str(media_root))
    (media_root / "source.stored").write_bytes(body)

    with _stub() as (endpoint, server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        media = MediaService(manager.store, tenant_id=manager.tenant_id).create_managed(
            name="line-source.bin", content_type="application/octet-stream",
            metadata={"stored_as": "source.stored", "storage_kind": "managed_file"},
            project_id=PROJECT,
        )
        server.media_source = {"media_refs": [{
            "media_id": str(media["id"]), "name": str(media["name"]),
            "content_type": str(media["content_type"]), "url": f"managed://{media['id']}",
        }]}  # type: ignore[attr-defined]
        declaration = {
            "format": "lynewright-integration/1",
            "id": "lynewright.integration.fixture-multipart",
            "name": "Multipart Media Fixture",
            "version": "1.0.0",
            "lynewright_version": "^0.1.0",
            "runtime_kind": "openapi",
            "endpoint_url": endpoint,
            "openapi": {"inline": {
                "openapi": "3.0.3", "info": {"title": "Multipart fixture", "version": "1.0.0"},
                "paths": {
                    "/media-source": {"get": {"operationId": "mediaSource", "responses": {"200": {"description": "source"}}}},
                    "/upload": {"post": {
                        "operationId": "uploadMedia",
                        "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                            "type": "object", "required": ["file"],
                            "properties": {"file": {"type": "string", "format": "binary"}},
                        }}}},
                        "responses": {"200": {"description": "uploaded"}},
                    }},
                },
            }},
            "operations": [
                {"operation_id": "mediaSource", "description": "Return the test media ref."},
                {"operation_id": "uploadMedia", "description": "Accept a multipart file.",
                 "input_hint": {"media": {"body.file": "lynewright-upload"}}},
            ],
        }
        imported = IntegrationFileService(manager).import_value(
            declaration, artifact_bytes=json.dumps(declaration, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        integration_id = imported["integration"]["integration"]["id"]
        _authorize(manager, integration_id)
        manager.connections.create(
            integration_id, connection_id="line-local", name="Line local",
            project_id=PROJECT, origin_kind="internal_endpoint", internal_endpoint_ref="line_stub",
            configuration={"openapi": {"approved_operations": ["mediaSource", "uploadMedia"]}},
        )
        manager.integrations.enable(integration_id)
        catalogue = _catalog(manager, monkeypatch)
        available = [item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                     if item["integration_id"] == integration_id]
        source = next(item for item in available if item["operation"] == "mediaSource")
        upload = next(item for item in available if item["operation"] == "uploadMedia")
        upload_schema = upload["input_schema"]["properties"]["body"]["properties"]["file"]
        assert upload_schema["format"] == "lynewright-upload", upload_schema
        definition = {"stages": [
            {**_stage(source), "id": "source"},
            {**_stage(upload, args={"body": {"file": "{{stages.source.output.media_refs.0}}"}}), "id": "upload"},
        ]}
        engine, started = _save_and_start(manager, catalogue, definition)
        completed = _wait(engine, started["id"], timeout=20)
        assert completed["status"] == "done", completed
        result = completed["stages"][1]["output"]
        assert result == {
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "content_type": "application/octet-stream",
        }
        assert not list(media_root.glob("*.part"))
        _close(db)


def test_catalogue_installed_openapi_file_runs_in_a_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.services.catalogue import ensure_schema as ensure_catalogue_schema
    from app.services.catalogue_installation import CatalogueInstaller
    from app.services.integration_provenance import get_provenance

    with _template_openapi_stub() as endpoint:
        key_id = "1122334455667788"
        public = base64.b64encode(b"Ed" + bytes.fromhex(key_id) + b"\x03" * 32).decode("ascii")
        monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", public)
        monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "0")
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "catalogue-only")
        manager, db = _manager(tmp_path)
        body = (FIXTURES / "openapi.yaml").read_bytes()
        source = "https://catalogue.example.test/artifacts/openapi.yaml"
        entry = {
            "id": "lynewright.integration.fixture-openapi", "version": "1.0.0", "kind": "file",
            "source": source, "sha256": hashlib.sha256(body).hexdigest(), "lynewright_version": "^0.1.0",
            "permissions": {"capabilities": ["network:use"], "egress": "none", "media": "none", "credential_refs": []},
            "review": {"reviewer": "line fixture", "date": "2026-10-16", "catalogue_commit": "139cceb8570bfb1ba5e8da70377223987abb02c1"},
        }
        index_bytes = json.dumps({
            "format": "lynewright-catalogue/1", "serial": 12, "generated_at": "2026-10-16T00:00:00Z",
            "name": "Line fixture", "entries": [entry],
        }, sort_keys=True).encode()
        with manager.store.connection() as connection:
            ensure_catalogue_schema(connection)
            connection.execute(
                "UPDATE integration_catalogue_cache SET index_bytes=?,index_sha256=?,serial=12,"
                "generated_at='2026-10-16T00:00:00Z',verified_key_id=? WHERE id=1",
                (index_bytes, hashlib.sha256(index_bytes).hexdigest(), key_id),
            )
            connection.commit()

        class _CatalogueHTTP:
            def get(self, url: str, **_kwargs: Any) -> dict[str, bytes]:
                assert url == source
                return {"body": body}

        class _Catalogue:
            http = _CatalogueHTTP()

            @staticmethod
            @contextmanager
            def stream_artifact(_url: str, *, max_bytes: int, timeout_seconds: float) -> Any:
                with tempfile.TemporaryDirectory(prefix="lynewright-test-artifact-") as artifact_dir:
                    artifact_path = Path(artifact_dir) / "file.part"
                    artifact_path.write_bytes(body)
                    assert len(body) <= max_bytes
                    yield {"status_code": 200, "path": artifact_path, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "headers": {}}

            @staticmethod
            def get(integration_id: str, version: str) -> dict[str, Any] | None:
                return entry if (integration_id, version) == (entry["id"], entry["version"]) else None

            @staticmethod
            def _cache_row() -> dict[str, Any]:
                return {"serial": 12, "verified_key_id": key_id}

        installed = CatalogueInstaller(_Catalogue(), manager).install(entry["id"], entry["version"], internal_endpoint_ref="line_stub")
        assert installed["integration"]["id"] == entry["id"]
        provenance = get_provenance(manager.store, manager.tenant_id, entry["id"])
        assert provenance and provenance["kind"] == "catalogue" and provenance["sha256"] == entry["sha256"]

        _authorize(manager, entry["id"])
        _local_connection(manager, entry["id"], openapi=True)
        manager.integrations.enable(entry["id"])
        operation_catalogue = _catalog(manager, monkeypatch)
        operation = next(item for item in operation_catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                         if item["integration_id"] == entry["id"] and item["operation"] == "health")
        engine, started = _save_and_start(manager, operation_catalogue, {"stages": [_stage(operation)]})
        completed = _wait(engine, started["id"])
        assert completed["status"] == "done", completed
        assert completed["stages"][0]["output"] == {"status": "ok"}
        assert _mapped_trace_count(manager) == 1
        _close(db)


def test_for_each_reuses_catalogue_and_async_runner_per_item(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _stub() as (endpoint, server):
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__LINE_STUB", endpoint)
        manager, db = _manager(tmp_path)
        integration_id = _import(manager, "async-openapi.yaml", endpoint)["id"]
        _authorize(manager, integration_id)
        _local_connection(manager, integration_id, openapi=True)
        manager.integrations.enable(integration_id)
        catalogue = _catalog(manager, monkeypatch)
        operation = next(item for item in catalogue.operations_for_lines(_authority(), project_id=PROJECT)
                         if item["connection_id"] == "line-local" and item["operation"] == "submit")
        definition = {"stages": [{
            "id": "batch", "kind": "for_each", "items": "{{run.vars.values}}",
            "operation": operation["operation_id"], "input": {"body": {"text": "{{item.text}}"}},
        }]}
        engine = ProductionLineEngine(store=manager.store, tenant_id=manager.tenant_id)
        engine.db.put_line(
            "foreach-async-line", name="For each async line", project_id=PROJECT,
            definition=definition, enabled=True, actor_id="test", operation_directory=catalogue,
            operation_actor=_authority(), create_only=True,
        )
        started = engine.start(
            "foreach-async-line", variables={"values": [{"text": "one"}, {"text": "two"}]},
            project_id=PROJECT, actor={"user_id": "test"},
        )
        run = started
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = engine.get_run(started["id"])
            if run["status"] in {"done", "done_with_errors", "failed", "cancelled", "rejected"}:
                break
            time.sleep(0.02)
        assert run["status"] == "done", json.dumps({"status": run["status"], "stages": run["stages"], "timeline": run["timeline"][-10:]}, default=str)
        assert run["stages"][0]["output"]["total"] == 2
        assert run["stages"][0]["output"]["succeeded"] == 2
        assert [item["status"] for item in engine.stage_items(started["id"], "batch")["items"]] == ["succeeded", "succeeded"]
        assert server.polls == {"job-1": 2, "job-2": 2}
        assert _mapped_trace_count(manager) == 6
        _close(db)
