from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.request import urlopen

import pytest
import yaml
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from starlette.testclient import TestClient

from app.routers import integration_management as integration_management_router
from app.security import require_tenant_context
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_file import IntegrationFileService, IntegrationFileValidationError
from app.services.integration_management import IntegrationManagementService
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformConflictError, PlatformStore


FIXTURES = Path(__file__).parent / "fixtures" / "integration_files"


@pytest.fixture(autouse=True)
def _legacy_import_tests_use_open_install_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests exercise file semantics; K5.4 policy cases live in their own suite."""
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "open")
    monkeypatch.setattr("app.services.application_logging.log_event", lambda **_event: None)


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size) or b"{}")
        method = request.get("method")
        if method == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "Echo input", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _local_stub() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _manager() -> tuple[IntegrationManagementService, sqlite3.Connection]:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    registry = IntegrationRegistry(store)
    connections = ConnectionProfileService(store, registry=registry, endpoint_validator=lambda _url: {})
    manager = IntegrationManagementService(
        registry, connections, ContributionRegistry(registry, connections=connections),
    )
    return manager, db


def _fixture(name: str) -> dict[str, Any]:
    return IntegrationFileService.parse((FIXTURES / name).read_bytes())


def _generated_json(value: Any) -> bytes:
    """Serialize an in-test declaration as the exact bytes passed to importer."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_schema_examples_and_invalid_files() -> None:
    schema = IntegrationFileService.schema()
    Draft202012Validator.check_schema(schema)
    manager, db = _manager()
    importer = IntegrationFileService(manager)
    valid_openapi = _fixture("openapi.yaml")
    valid_mcp = _fixture("mcp.yaml")
    assert importer.validate(valid_openapi) == []
    assert importer.validate(valid_mcp) == []
    assert any("format" in error for error in importer.validate(_fixture("invalid-schema.yaml")))
    secret_errors = importer.validate(_fixture("invalid-secret.yaml"))
    assert any("password" in error and "credential ref" in error for error in secret_errors)
    version_errors = importer.validate(_fixture("incompatible-version.yaml"))
    assert any("does not include Lynewright" in error for error in version_errors)
    db.close()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("password", "fixture-placeholder"),
        ("token", "fixture-placeholder"),
        ("secret", "fixture-placeholder"),
        ("api_key", "fixture-placeholder"),
        ("apikey", "fixture-placeholder"),
        ("authorization", "fixture-placeholder"),
        ("bearer", "fixture-placeholder"),
        ("client" + "_" + "secret", "fixture-placeholder"),
        ("private" + "_" + "key", "fixture-placeholder"),
    ],
)
def test_sensitive_named_literal_values_are_rejected(key: str, value: str) -> None:
    manager, db = _manager()
    candidate = _fixture("mcp.yaml")
    candidate["operations"][0]["input_hint"] = {key: value}
    errors = IntegrationFileService(manager).validate(candidate)
    assert any("operations.0.input_hint" in error and "credential ref" in error for error in errors)
    db.close()


@pytest.mark.parametrize(
    "value",
    [
        "sk" + "-" + "a" * 24,
        "ghp_" + "a" * 36,
        "AIza" + "a" * 35,
        "xoxb-" + "a" * 24,
        "123456789:" + "a" * 35,
        "AKIA" + "A" * 16,
        "-----BEGIN " + "PRIVATE KEY" + "-----",
    ],
)
def test_key_and_token_patterns_are_rejected(value: str) -> None:
    manager, db = _manager()
    candidate = _fixture("mcp.yaml")
    candidate["operations"][0]["description"] = value
    errors = IntegrationFileService(manager).validate(candidate)
    assert any("resembles a key or token" in error for error in errors)
    db.close()


def test_import_export_round_trip_is_idempotent_and_settings_remain_data_only() -> None:
    with _local_stub() as endpoint:
        assert json.load(urlopen(endpoint, timeout=2)) == {"status": "ok"}
        manager, db = _manager()
        importer = IntegrationFileService(manager)
        value = _fixture("openapi.yaml")
        value["endpoint_url"] = endpoint
        value["settings_schema"] = {
            "schema_id": "fixture_settings",
            "fields": [{"key": "region", "label": "Region", "type": "text", "default": "west"}],
        }
        created = importer.import_value(value, artifact_bytes=_generated_json(value))
        integration_id = created["integration"]["integration"]["id"]
        operation = manager.openapi_operations(integration_id)[0]
        assert operation["approved"] is True
        assert operation["summary"] == "Read the local stub health response."
        exported = importer.export_value(integration_id)
        assert exported == value
        repeated = importer.import_value(exported, artifact_bytes=_generated_json(exported))
        assert repeated["created"] is False
        assert repeated["idempotent"] is True
        settings = manager.settings(integration_id)
        assert settings["schema_id"] == "fixture_settings"
        assert settings["values"] == {"region": "west"}
        assert "connection_id" not in json.dumps(exported)
        assert "tenant_id" not in json.dumps(exported)
        db.close()


def test_openapi_import_requires_explicit_enabled_and_reports_zero_approvals() -> None:
    with _local_stub() as endpoint:
        manager, db = _manager()
        value = _fixture("openapi.yaml")
        value["endpoint_url"] = endpoint
        value["operations"][0].pop("enabled")
        result = IntegrationFileService(manager).import_value(value, artifact_bytes=_generated_json(value))
        assert result["approval_message"] == "0 operations approved; approve them in Integrations."
        operation = manager.openapi_operations(value["id"])[0]
        assert operation["approved"] is False
        db.close()


def test_imports_openapi_and_mcp_examples_against_test_local_stub() -> None:
    with _local_stub() as endpoint:
        manager, db = _manager()
        importer = IntegrationFileService(manager)
        openapi = _fixture("openapi.yaml")
        openapi["endpoint_url"] = endpoint
        mcp = _fixture("mcp.yaml")
        mcp["endpoint_url"] = endpoint + "/mcp"
        openapi_result = importer.import_value(openapi, artifact_bytes=_generated_json(openapi))
        mcp_result = importer.import_value(mcp, artifact_bytes=_generated_json(mcp))
        assert openapi_result["created"] and mcp_result["created"]
        assert importer.export_value(openapi["id"])["operations"][0]["operation_id"] == "health"
        assert importer.export_value(mcp["id"])["operations"][0]["tool_name"] == "echo"
        connection_id = mcp_result["integration"]["connections"][0]["id"]
        assert manager.connections.get(mcp["id"], connection_id)["configuration"]["mcp"]["approved_tools"] == []
        db.close()


def test_explicitly_enabled_mcp_tool_still_approves_on_import() -> None:
    with _local_stub() as endpoint:
        manager, db = _manager()
        value = _fixture("mcp.yaml")
        value["id"] = "lynewright.integration.fixture-mcp-enabled"
        value["endpoint_url"] = endpoint + "/mcp"
        value["operations"][0]["enabled"] = True
        importer = IntegrationFileService(manager)
        requested_approvals: list[str] = []
        importer._discover_and_approve_imported_mcp = lambda _id, tools, _refs, _project: requested_approvals.extend(tools)  # type: ignore[method-assign]
        result = importer.import_value(value, artifact_bytes=_generated_json(value))
        assert requested_approvals == ["echo"]
        assert result["approval_message"] is None
        db.close()


def test_openapi_url_reference_uses_injected_document_fetcher_and_round_trips() -> None:
    manager, db = _manager()
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Fetched fixture", "version": "1.0.0"},
        "paths": {"/health": {"get": {"operationId": "health", "responses": {"200": {"description": "ok"}}}}},
    }
    requested: list[str] = []

    def fetch(url: str, **_kwargs: Any) -> dict[str, Any]:
        requested.append(url)
        return {"body": yaml.safe_dump(document).encode("utf-8")}

    importer = IntegrationFileService(manager, document_fetcher=fetch)
    value = _fixture("openapi.yaml")
    value["openapi"] = {"url": "https://spec.example.test/openapi.yaml"}
    importer.import_value(value, artifact_bytes=_generated_json(value))
    exported = importer.export_value(value["id"])
    assert exported["openapi"] == value["openapi"]
    importer.import_value(exported, artifact_bytes=_generated_json(exported))
    assert requested == [value["openapi"]["url"], value["openapi"]["url"]]
    db.close()


def test_version_change_is_rejected_without_mutating_existing_declaration() -> None:
    manager, db = _manager()
    importer = IntegrationFileService(manager)
    value = _fixture("mcp.yaml")
    importer.import_value(value, artifact_bytes=_generated_json(value))
    changed = copy.deepcopy(value)
    changed["version"] = "2.0.0"
    with pytest.raises(PlatformConflictError, match="cannot be upgraded"):
        importer.import_value(changed, artifact_bytes=_generated_json(changed))
    assert manager.integrations.get(value["id"])["version"] == "1.0.0"
    db.close()


def test_manual_creation_uses_the_same_import_path() -> None:
    manager, db = _manager()
    result = IntegrationFileService(manager).import_manual({
        "name": "Manual route fixture",
        "runtime_kind": "mcp",
        "endpoint_url": "http://127.0.0.1:8000/mcp",
        "credential_refs": [],
        "payload_mapping": {},
    })
    assert result["created"] is True
    assert result["integration"]["integration"]["id"].startswith("lynewright.integration.manual-")
    provenance = result["integration"]["provenance"]
    assert provenance["kind"] == "outside"
    assert provenance["source"].startswith("file sha256:")
    assert provenance["confirmed_by"] == "system"
    db.close()


def test_admin_import_and_export_routes_accept_yaml_json_and_multipart(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, db = _manager()
    application = FastAPI()
    application.include_router(integration_management_router.router)
    application.dependency_overrides[require_tenant_context] = lambda: {
        "tenant_id": "integration-file-test", "user_id": 1, "role": "admin",
    }
    def service_for_context(context):
        manager.actor_id = str(context.get("user_id") or "admin")
        return manager

    monkeypatch.setattr(integration_management_router, "_service", service_for_context)
    with TestClient(application) as client:
        value = _fixture("mcp.yaml")
        yaml_body = yaml.safe_dump(value)
        yaml_result = client.post(
            "/api/integration-management/import",
            content=yaml_body,
            headers={"content-type": "application/yaml"},
        )
        assert yaml_result.status_code == 201, yaml_result.text
        assert yaml_result.json()["integration"]["provenance"]["kind"] == "outside"
        assert yaml_result.json()["integration"]["provenance"]["confirmed_by"] == "1"
        assert yaml_result.json()["integration"]["provenance"]["sha256"] == hashlib.sha256(yaml_body.encode()).hexdigest()
        json_export = client.get(f"/api/integration-management/{value['id']}/export?format=json")
        assert json_export.status_code == 200
        assert json.loads(json_export.text) == value
        yaml_export = client.get(f"/api/integration-management/{value['id']}/export")
        assert yaml_export.status_code == 200
        assert yaml.safe_load(yaml_export.text) == value
        json_file = _fixture("mcp.yaml")
        json_file["id"] = "lynewright.integration.fixture-json"
        json_body = json.dumps(json_file)
        json_result = client.post(
            "/api/integration-management/import",
            content=json_body,
            headers={"content-type": "application/json"},
        )
        assert json_result.status_code == 201, json_result.text
        assert json_result.json()["integration"]["provenance"]["sha256"] == hashlib.sha256(json_body.encode()).hexdigest()
        multipart = _fixture("mcp.yaml")
        multipart["id"] = "lynewright.integration.fixture-multipart"
        multipart_body = yaml.safe_dump(multipart)
        upload_result = client.post(
            "/api/integration-management/import",
            files={"file": ("integration.yaml", multipart_body, "application/yaml")},
        )
        assert upload_result.status_code == 201, upload_result.text
        assert upload_result.json()["integration"]["provenance"]["sha256"] == hashlib.sha256(multipart_body.encode()).hexdigest()
        manual_body = json.dumps({
            "name": "Manual API fixture",
            "runtime_kind": "mcp",
            "endpoint_url": "http://127.0.0.1:8000/mcp",
        })
        manual_result = client.post("/api/integration-management/manual", content=manual_body, headers={"content-type": "application/json"})
        assert manual_result.status_code == 201, manual_result.text
        assert manual_result.json()["integration"]["id"].startswith("lynewright.integration.manual-")
        assert manual_result.json()["provenance"]["kind"] == "outside"
        assert manual_result.json()["provenance"]["confirmed_by"] == "1"
        assert manual_result.json()["provenance"]["sha256"] == hashlib.sha256(manual_body.encode()).hexdigest()
        invalid = _fixture("invalid-schema.yaml")
        response = client.post(
            "/api/integration-management/import",
            content=yaml.safe_dump(invalid),
            headers={"content-type": "text/plain"},
        )
        assert response.status_code == 422
        assert isinstance(response.json()["detail"]["errors"], list)
        viewer_app = FastAPI()
        viewer_app.include_router(integration_management_router.router)
        viewer_app.dependency_overrides[require_tenant_context] = lambda: {
            "tenant_id": "integration-file-test", "user_id": 1, "role": "viewer",
        }
        monkeypatch.setattr(integration_management_router, "_service", lambda _context: manager)
        denied = TestClient(viewer_app).post(
            "/api/integration-management/import",
            content=yaml.safe_dump(multipart),
            headers={"content-type": "application/yaml"},
        )
        assert denied.status_code == 403
    db.close()
