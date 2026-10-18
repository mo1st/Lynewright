from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import textwrap
import zipfile
from typing import Any, Iterator

import pytest

from app.services.catalogue import ensure_schema as ensure_catalogue_schema
from app.services.catalogue_installation import CatalogueInstaller
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_management import IntegrationManagementService
from app.services.integration_provenance import get_provenance
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import HTTPService, PlatformConflictError, PlatformOperationError, PlatformStore, PlatformValidationError


class _HTTP:
    def __init__(self, artifacts: dict[str, bytes]) -> None:
        self.artifacts = artifacts

    def get(self, url: str, **_kwargs: Any) -> dict[str, bytes]:
        if url not in self.artifacts:
            raise OSError("fixture artifact unreachable")
        return {"body": self.artifacts[url]}

    @contextmanager
    def stream_to_temp_file(self, url: str, *, max_bytes: int, **_kwargs: Any) -> Iterator[dict[str, Any]]:
        body = self.get(url)["body"]
        if len(body) > max_bytes:
            raise PlatformOperationError("HTTP response exceeded the configured size limit.")
        with tempfile.TemporaryDirectory(prefix="lynewright-test-artifact-") as directory:
            path = Path(directory) / "response.part"
            path.write_bytes(body)
            yield {
                "status_code": 200, "path": path, "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(), "headers": {},
            }


def _manager(tmp_path: Path) -> tuple[IntegrationManagementService, list[sqlite3.Connection]]:
    opened: list[sqlite3.Connection] = []
    path = tmp_path / "catalogue-install.sqlite3"

    def connect() -> sqlite3.Connection:
        db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        opened.append(db)
        return db

    store = PlatformStore(connect)
    integrations = IntegrationRegistry(store, tenant_id="ten_default", lynewright_version="0.1.0")
    connections = ConnectionProfileService(
        store, tenant_id="ten_default", registry=integrations,
        endpoint_validator=lambda _url: {},
    )
    contributions = ContributionRegistry(integrations, connections=connections)
    return IntegrationManagementService(integrations, connections, contributions), opened


def _entry(kind: str, artifact: bytes | None = None, *, integration_id: str = "lynewright.integration.catalogue-fixture") -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": integration_id, "version": "1.0.0", "kind": kind,
        "source": "https://catalogue.example.test/artifacts/fixture.yaml",
        "lynewright_version": "^0.1.0",
        "permissions": {"capabilities": [], "egress": "none", "media": "none", "credential_refs": []},
        "review": {"reviewer": "K5.5 test", "date": "2026-10-16", "catalogue_commit": "1234567890abcdef"},
    }
    if kind in {"file", "package"}:
        entry["sha256"] = hashlib.sha256(artifact or b"").hexdigest()
    if kind == "package":
        entry["limits"] = {"cpus": 1, "memory_mb": 256, "pids": 64, "timeout_seconds": 30}
        entry["permissions"]["media"] = "write"
    if kind == "container":
        entry["source"] = "localhost/lynewright/thumbnail@sha256:" + "a" * 64
        entry["limits"] = {"cpus": 1, "memory_mb": 256, "pids": 64, "timeout_seconds": 30}
    return entry


def _service(
    entry: dict[str, Any], artifacts: dict[str, bytes], *, verified_key_id: str = "fixture-key",
) -> Any:
    from app.services.catalogue import CatalogueService

    service = CatalogueService(http_service=_HTTP(artifacts))
    service.get = lambda integration_id, version: entry if integration_id == entry["id"] and version == entry["version"] else None
    service._cache_row = lambda: {"serial": 9, "verified_key_id": verified_key_id}
    return service


def _cache_index(
    manager: IntegrationManagementService, entries: list[dict[str, Any]], *, verified_key_id: str = "fixture-key",
) -> None:
    encoded = json.dumps({
        "format": "lynewright-catalogue/1", "serial": 9, "generated_at": "2026-10-16T00:00:00Z",
        "name": "fixture", "entries": entries,
    }, sort_keys=True).encode()
    with manager.store.connection() as db:
        ensure_catalogue_schema(db)
        db.execute(
            """UPDATE integration_catalogue_cache SET index_bytes=?,index_sha256=?,serial=9,
               generated_at='2026-10-16T00:00:00Z',verified_key_id=? WHERE id=1""",
            (encoded, hashlib.sha256(encoded).hexdigest(), verified_key_id),
        )
        db.commit()


def _package_wheel(manifest: dict[str, Any], *, module: str = "fixture_native", sentinel: Path | None = None) -> bytes:
    side_effect = (
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('imported', encoding='utf-8')\nraise RuntimeError('imported in API')\n"
        if sentinel else ""
    )
    files = {
        f"{module}/__init__.py": side_effect,
        f"{module}/manifest.py": "raise RuntimeError('manifest imported in API')\n",
        f"{module}/provider.py": "class Provider:\n    def invoke(self, *args, **kwargs):\n        return {'status': 'done'}\n",
        f"{module}/lynewright-manifest.json": json.dumps(manifest, sort_keys=True),
        "lynewright_fixture_native-1.0.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: lynewright-fixture-native\nVersion: 1.0.0\n\n",
        "lynewright_fixture_native-1.0.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: K5.5 test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "lynewright_fixture_native-1.0.0.dist-info/entry_points.txt": f"[lynewright.integrations]\nfixture = {module}.manifest\n",
        "lynewright_fixture_native-1.0.0.dist-info/RECORD": "",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in sorted(files.items()):
            archive.writestr(name, body.encode("utf-8"))
    return output.getvalue()


def _native_manifest(integration_id: str) -> dict[str, Any]:
    return {
        "schema_version": "1.2", "id": integration_id, "name": "Catalogue fixture",
        "version": "1.0.0", "type": "native", "lynewright_version": "^0.1.0",
        "sdk_version": "1.0.0", "requested_capabilities": ["system:read"],
        "contributions": ["services"],
        "provides": [{
            "contract": "lynewright.fixture.catalogue", "implementation": "fixture_native.provider:Provider",
            "version": "1.0.0", "capabilities": ["run"],
            "operation_schemas": {"run": {
                "type": "object", "properties": {}, "additionalProperties": False,
                "caller_capability": "system:read", "read_only": True,
                "risk_class": "READ_ONLY", "description": "Catalogue fixture operation.",
            }},
        }],
    }


def test_catalogue_file_install_uses_exact_hash_and_records_signed_provenance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "catalogue-only")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "1")
    from app.services.catalogue import _BUILTIN_DEV_PUBLIC_KEY
    dev_key_id = base64.b64decode(_BUILTIN_DEV_PUBLIC_KEY)[2:10].hex()
    trusted_key_id = f"dev:{dev_key_id}"
    body = (Path(__file__).parent / "fixtures" / "integration_files" / "openapi.yaml").read_bytes()
    parsed_id = "lynewright.integration.fixture-openapi"
    entry = _entry("file", body, integration_id=parsed_id)
    entry["source"] = "https://catalogue.example.test/artifacts/openapi.yaml"
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry], verified_key_id=trusted_key_id)
    result = CatalogueInstaller(
        _service(entry, {entry["source"]: body}, verified_key_id=trusted_key_id), manager,
    ).install(parsed_id, "1.0.0")
    assert result["integration"]["id"] == parsed_id
    provenance = get_provenance(manager.store, manager.tenant_id, parsed_id)
    assert provenance and provenance["kind"] == "catalogue"
    assert provenance["sha256"] == hashlib.sha256(body).hexdigest()
    assert provenance["catalogue_serial"] == 9
    for db in opened:
        db.close()


def test_catalogue_package_is_stored_without_import_and_runtime_config_uses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.services.runner_runtime_config import generate_runtime_config

    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_DIR", str(tmp_path / "packages"))
    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_HOST_DIR", str(tmp_path / "host-packages"))
    integration_id = "lynewright.integration.catalogue-native"
    manifest = _native_manifest(integration_id)
    sentinel = tmp_path / "package-imported"
    wheel = _package_wheel(manifest, sentinel=sentinel)
    entry = _entry("package", wheel, integration_id=integration_id)
    entry["source"] = "https://catalogue.example.test/artifacts/lynewright_fixture_native-1.0.0-py3-none-any.whl"
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry])
    installed = CatalogueInstaller(_service(entry, {entry["source"]: wheel}), manager).install(integration_id, "1.0.0")
    assert not sentinel.exists()
    row = manager.integrations.get(integration_id)
    assert row["source"] == "package"
    wheel_dir = Path(row["configuration"]["catalogue_package"]["wheel_directory"])
    wheel_path = wheel_dir / row["configuration"]["catalogue_package"]["wheel_filename"]
    assert wheel_path.read_bytes() == wheel
    assert (wheel_dir / "Containerfile.integration").is_file()
    assert installed["provenance"]["kind"] == "catalogue"
    runtime = generate_runtime_config(manager.store, row, format="compose", tenant_id=manager.tenant_id)
    assert str(tmp_path / "host-packages" / integration_id / "1.0.0") in runtime["content"]
    assert str(wheel_dir) not in runtime["content"]
    assert "The API image does not need this package" in " ".join(runtime["warnings"])
    assert not sentinel.exists()
    for db in opened:
        db.close()


def test_runner_containerfile_is_packaged_and_matches_repository_copy() -> None:
    packaged = Path(__file__).parents[1] / "resources" / "runner" / "Containerfile.integration"
    repository_copy = Path(__file__).resolve().parents[3] / "runner" / "Containerfile.integration"
    assert packaged.read_bytes() == repository_copy.read_bytes()


def test_catalogue_package_install_succeeds_in_backend_app_only_layout(tmp_path: Path) -> None:
    """Regression: deployed API images have backend/app, but no repo-level runner/."""
    app_root = Path(__file__).parents[1]
    image_root = tmp_path / "image"
    image_backend = image_root / "backend"
    image_backend.mkdir(parents=True)
    shutil.copytree(
        app_root, image_backend / "app",
        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"),
    )
    wheel = _package_wheel(_native_manifest("lynewright.integration.image-layout"))
    wheel_path = tmp_path / "lynewright_fixture_native-1.0.0-py3-none-any.whl"
    wheel_path.write_bytes(wheel)
    script = textwrap.dedent("""
        import hashlib, json, sys
        from pathlib import Path
        import app.services.catalogue_installation as installer_module
        from app.services.catalogue_installation import CatalogueInstaller
        from app.services.platform_services import PlatformNotFoundError

        class Integrations:
            lynewright_version = "0.1.0"
            def get(self, _integration_id): raise PlatformNotFoundError("missing")
            def register(self, manifest, *, configuration): self.manifest, self.configuration = manifest, configuration
            def set_source_metadata(self, *_args, **_kwargs): pass
        class Management:
            tenant_id = "ten_default"
            actor_id = "image-layout-test"
            store = object()
            def __init__(self): self.integrations = Integrations()
            def ensure_runtime_definition_registered(self, _integration_id): return {"registered": False}
            def detail(self, integration_id): return {"integration": {"id": integration_id}}
        class Catalogue:
            def _cache_row(self): return {"serial": 2, "verified_key_id": "fixture"}
        installer_module.record_provenance = lambda *_args, **_kwargs: {}
        wheel_path = Path(sys.argv[1])
        body = wheel_path.read_bytes()
        entry = {
            "id": "lynewright.integration.image-layout", "version": "1.0.0",
            "kind": "package", "source": "https://catalogue.invalid/lynewright_fixture_native-1.0.0-py3-none-any.whl",
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        result = CatalogueInstaller(Catalogue(), Management())._install_package(entry, wheel_path)
        target = Path(__import__("os").environ["LYNEWRIGHT_PACKAGES_DIR"]) / entry["id"] / entry["version"]
        assert (target / "Containerfile.integration").is_file()
        assert (target / wheel_path.name).read_bytes() == body
        print(json.dumps({"installed": result["integration"]["id"], "containerfile": True}))
    """)
    packages_dir = tmp_path / "packages"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(image_backend)
    environment["LYNEWRIGHT_PACKAGES_DIR"] = str(packages_dir)
    environment["LYNEWRIGHT_PACKAGES_HOST_DIR"] = str(tmp_path / "host-packages")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(wheel_path)], cwd=image_root,
        env=environment, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '"installed": "lynewright.integration.image-layout"' in completed.stdout
    assert '"containerfile": true' in completed.stdout


def test_catalogue_file_endpoint_refusal_points_at_operator_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    entry = _entry("file", b"fixture")
    note = "Start the example service and configure LYNEWRIGHT_INTERNAL_ENDPOINT__OPENAPI."
    entry["requires_operator_setup"] = [note]
    manager, opened = _manager(tmp_path)

    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise PlatformValidationError("Outbound URL port is not allowed.")

    monkeypatch.setattr("app.services.catalogue_installation.IntegrationFileService.import_value", refused)
    with pytest.raises(PlatformValidationError, match="Catalogue operator setup") as error:
        CatalogueInstaller(_service(entry, {}), manager)._install_file(
            entry, b"id: lynewright.integration.catalogue-fixture\nversion: 1.0.0\n", internal_endpoint_ref="",
        )
    assert note in str(error.value)
    for db in opened:
        db.close()


def test_catalogue_container_install_pins_signed_digest_in_runtime_config(tmp_path: Path) -> None:
    from app.services.runner_runtime_config import generate_runtime_config

    entry = _entry("container", integration_id="lynewright.integration.catalogue-thumbnail")
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry])
    installed = CatalogueInstaller(_service(entry, {}), manager).install(entry["id"], entry["version"])
    config = generate_runtime_config(manager.store, manager.integrations.get(entry["id"]), format="compose", tenant_id=manager.tenant_id)
    assert entry["source"] in config["content"]
    assert installed["provenance"]["image_digest"] == "sha256:" + "a" * 64
    for db in opened:
        db.close()


@pytest.mark.parametrize("failure", ["hash", "unreachable", "yanked", "incompatible"])
def test_catalogue_install_failures_leave_no_integration_or_package_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_DIR", str(tmp_path / "packages"))
    body = b"not an integration file"
    entry = _entry("file", body)
    if failure == "yanked":
        entry["yanked"] = {"reason": "withdrawn", "date": "2026-10-16"}
    if failure == "incompatible":
        entry["lynewright_version"] = ">=1.0.0"
    artifacts = {entry["source"]: body}
    if failure == "hash":
        entry["sha256"] = "0" * 64
    if failure == "unreachable":
        artifacts = {}
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry])
    installer = CatalogueInstaller(_service(entry, artifacts), manager)
    with pytest.raises((PlatformConflictError, PlatformOperationError, ValueError, OSError)):
        installer.install(entry["id"], entry["version"])
    assert manager.integrations.list() == []
    assert not (tmp_path / "packages").exists()
    for db in opened:
        db.close()


@contextmanager
def _local_artifact_server(routes: dict[str, bytes | str], seen: list[str] | None = None) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            route = self.path.split("?", 1)[0]
            if seen is not None:
                seen.append(route)
            if route == "/redirect-private":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.2:{self.server.server_port}/private-artifact")
                self.end_headers()
                return
            if route == "/upstream-error":
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if route == "/slow":
                import time
                self.send_response(200)
                self.send_header("Content-Length", "4")
                self.end_headers()
                time.sleep(0.5)
                self.wfile.write(b"slow")
                return
            payload = routes.get(route)
            if payload is None:
                self.send_error(404)
                return
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _real_http_catalogue(
    entry: dict[str, Any], manager: IntegrationManagementService, monkeypatch: pytest.MonkeyPatch, origin: str,
    *, verified_key_id: str,
) -> Any:
    from app.services.catalogue import CatalogueService

    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_URL", "https://catalogue.example.test/catalogue.json")
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_INTERNAL_ENDPOINT_REF", "k55_local")
    monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__K55_LOCAL", origin)
    service = CatalogueService(http_service=HTTPService())
    service.get = lambda integration_id, version: entry if (integration_id, version) == (entry["id"], entry["version"]) else None
    service._cache_row = lambda: {"serial": 9, "verified_key_id": verified_key_id}
    return service


@contextmanager
def _catalogue_api_client(monkeypatch: pytest.MonkeyPatch, catalogue: Any, manager: IntegrationManagementService) -> Iterator[Any]:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import catalogue as catalogue_router
    from app.routers import integration_management as management_router

    monkeypatch.setattr(catalogue_router, "CatalogueService", lambda: catalogue)
    monkeypatch.setattr(management_router, "_service", lambda _context: manager)
    application = FastAPI()
    application.include_router(catalogue_router.router)
    application.dependency_overrides[catalogue_router._ADMIN] = lambda: {
        "tenant_id": manager.tenant_id, "user_id": manager.actor_id,
    }
    with TestClient(application) as client:
        yield client


def _configure_http_test_key(monkeypatch: pytest.MonkeyPatch) -> str:
    public_key_file = Path(__file__).parent / "fixtures" / "catalogue" / "minisign-interop" / "minisign.pub"
    operator_public_key = public_key_file.read_text(encoding="ascii").splitlines()[1]
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", operator_public_key)
    return base64.b64decode(operator_public_key)[2:10].hex()


def _watch_http_temp_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "http-temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.mark.parametrize("kind", ["file", "package"])
def test_catalogue_install_streams_file_and_package_with_real_http_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str,
) -> None:
    from app.services.integration_file import IntegrationFileService

    temp_root = _watch_http_temp_files(monkeypatch, tmp_path)
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "catalogue-only")
    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_DIR", str(tmp_path / "packages"))
    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_HOST_DIR", str(tmp_path / "host-packages"))
    if kind == "file":
        body = (Path(__file__).parent / "fixtures" / "integration_files" / "openapi.yaml").read_bytes()
        integration_id = str(IntegrationFileService.parse(body)["id"])
        artifact_name = "integration.yaml"
    else:
        integration_id = "lynewright.integration.real-http-wheel"
        manifest = _native_manifest(integration_id)
        body = _package_wheel(manifest)
        artifact_name = "lynewright_fixture_native-1.0.0-py3-none-any.whl"
    route = f"/artifacts/{artifact_name}"
    entry = _entry(kind, body, integration_id=integration_id)
    entry["source"] = f"https://catalogue.example.test{route}"
    if kind == "package":
        entry["sha256"] = hashlib.sha256(body).hexdigest()

    # This interop fixture is an explicit operator trust root for the test.
    operator_key_id = _configure_http_test_key(monkeypatch)

    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry], verified_key_id=operator_key_id)
    with _local_artifact_server({route: body}) as origin:
        catalogue = _real_http_catalogue(
            entry, manager, monkeypatch, origin, verified_key_id=operator_key_id,
        )
        installed = CatalogueInstaller(catalogue, manager).install(integration_id, "1.0.0")
        assert installed["integration"]["id"] == integration_id
        assert get_provenance(manager.store, manager.tenant_id, integration_id)["sha256"] == hashlib.sha256(body).hexdigest()
        if kind == "package":
            package = manager.integrations.get(integration_id)["configuration"]["catalogue_package"]
            stored = Path(package["wheel_directory"]) / package["wheel_filename"]
            assert stored.read_bytes() == body
    assert list(temp_root.iterdir()) == []
    for db in opened:
        db.close()


@pytest.mark.parametrize("kind", ["file", "package"])
def test_reinstall_of_same_catalogue_version_and_hash_returns_409_without_redownloading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str,
) -> None:
    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_DIR", str(tmp_path / "packages"))
    if kind == "file":
        body = (Path(__file__).parent / "fixtures" / "integration_files" / "openapi.yaml").read_bytes()
        from app.services.integration_file import IntegrationFileService
        integration_id = str(IntegrationFileService.parse(body)["id"])
        filename = "integration.yaml"
    else:
        integration_id = "lynewright.integration.duplicate-wheel"
        body = _package_wheel(_native_manifest(integration_id))
        filename = "lynewright_fixture_native-1.0.0-py3-none-any.whl"
    route = f"/artifacts/{filename}"
    entry = _entry(kind, body, integration_id=integration_id)
    entry["source"] = f"https://catalogue.example.test{route}"
    if kind == "package":
        entry["sha256"] = hashlib.sha256(body).hexdigest()
    key_id = _configure_http_test_key(monkeypatch)
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry], verified_key_id=key_id)
    requests: list[str] = []
    with _local_artifact_server({route: body}, requests) as origin:
        catalogue = _real_http_catalogue(entry, manager, monkeypatch, origin, verified_key_id=key_id)
        with _catalogue_api_client(monkeypatch, catalogue, manager) as client:
            first = client.post(f"/api/catalogue/entries/{integration_id}/1.0.0/install")
            assert first.status_code == 201, first.text
            second = client.post(f"/api/catalogue/entries/{integration_id}/1.0.0/install")
            assert second.status_code == 409
            assert second.json()["detail"] == "Already installed."
    assert requests == [route]
    for db in opened:
        db.close()


def test_consumer_error_after_real_200_download_is_not_mislabeled_as_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _watch_http_temp_files(monkeypatch, tmp_path)
    integration_id = "lynewright.integration.local-stage-error"
    body = _package_wheel(_native_manifest(integration_id))
    route = "/artifacts/lynewright_fixture_native-1.0.0-py3-none-any.whl"
    entry = _entry("package", body, integration_id=integration_id)
    entry["source"] = f"https://catalogue.example.test{route}"
    key_id = _configure_http_test_key(monkeypatch)
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry], verified_key_id=key_id)
    with _local_artifact_server({route: body}) as origin:
        catalogue = _real_http_catalogue(entry, manager, monkeypatch, origin, verified_key_id=key_id)
        installer = CatalogueInstaller(catalogue, manager)

        def fail_after_download(_entry: dict[str, Any], _wheel_path: Path) -> dict[str, Any]:
            raise OSError("local package staging failed")

        installer._install_package = fail_after_download
        with pytest.raises(OSError, match="local package staging failed") as error:
            installer.install(integration_id, "1.0.0")
        assert "could not be reached" not in str(error.value)
    for db in opened:
        db.close()


def test_missing_catalogue_wheel_returns_incomplete_conflict_and_explicit_repair_restores_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.services.catalogue_installation import catalogue_installation_incomplete

    _watch_http_temp_files(monkeypatch, tmp_path)
    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_DIR", str(tmp_path / "packages"))
    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_HOST_DIR", str(tmp_path / "host-packages"))
    integration_id = "lynewright.integration.repair-wheel"
    body = _package_wheel(_native_manifest(integration_id))
    route = "/artifacts/lynewright_fixture_native-1.0.0-py3-none-any.whl"
    entry = _entry("package", body, integration_id=integration_id)
    entry["source"] = f"https://catalogue.example.test{route}"
    key_id = _configure_http_test_key(monkeypatch)
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry], verified_key_id=key_id)
    requests: list[str] = []
    with _local_artifact_server({route: body}, requests) as origin:
        catalogue = _real_http_catalogue(entry, manager, monkeypatch, origin, verified_key_id=key_id)
        with _catalogue_api_client(monkeypatch, catalogue, manager) as client:
            first = client.post(f"/api/catalogue/entries/{integration_id}/1.0.0/install")
            assert first.status_code == 201, first.text
            current = manager.integrations.get(integration_id)
            provenance = get_provenance(manager.store, manager.tenant_id, integration_id)
            package = current["configuration"]["catalogue_package"]
            package_directory = Path(package["wheel_directory"])
            (package_directory / package["wheel_filename"]).unlink()
            assert catalogue_installation_incomplete(current, provenance, entry) is True
            from app.routers.catalogue import _entry_status
            from app.services import integration_provenance
            monkeypatch.setattr(
                "app.services.extension_registry.ExtensionRegistry",
                lambda _store, tenant_id: manager.integrations,
            )
            monkeypatch.setattr(
                integration_provenance, "get_provenance",
                lambda _store, tenant_id, item_id: get_provenance(manager.store, tenant_id, item_id),
            )
            catalogue.list_installable = lambda: [entry]
            assert _entry_status(catalogue, manager.tenant_id)[0]["installation_incomplete"] is True
            repeated = client.post(f"/api/catalogue/entries/{integration_id}/1.0.0/install")
            assert repeated.status_code == 409
            assert "incomplete; remove it or repair it" in repeated.json()["detail"]
            repaired = client.post(f"/api/catalogue/entries/{integration_id}/1.0.0/repair")
            assert repaired.status_code == 200, repaired.text
            assert repaired.json()["repaired"] is True
            restored_wheel = package_directory / package["wheel_filename"]
            assert hashlib.sha256(restored_wheel.read_bytes()).hexdigest() == entry["sha256"]
            assert (package_directory / "Containerfile.integration").is_file()
    assert requests == [route, route]
    assert list((tmp_path / "http-temp").iterdir()) == []
    for db in opened:
        db.close()


def test_catalogue_transport_error_log_keeps_underlying_exception_and_request_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import httpx
    from contextlib import closing
    from app import database
    from app.services.application_logging import bind_request_context, reset_request_context

    log_path = tmp_path / "error-log.sqlite3"
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(log_path))
    entry = _entry("file", b"yaml")
    manager, opened = _manager(tmp_path)
    catalogue = _service(entry, {})
    underlying = httpx.ConnectError("connection refused")

    @contextmanager
    def failed_stream(*_args: Any, **_kwargs: Any) -> Iterator[dict[str, Any]]:
        raise PlatformOperationError("Outbound HTTP request failed.") from underlying
        yield {}

    catalogue.stream_artifact = failed_stream
    request_token = bind_request_context(
        correlation_id="u5b-request-identity", request_path="/api/catalogue/entries/install",
        http_method="POST",
    )
    try:
        with pytest.raises(PlatformOperationError, match="could not be reached"):
            with CatalogueInstaller(catalogue, manager)._artifact(entry):
                pytest.fail("failed transport must not yield an artifact")
    finally:
        reset_request_context(request_token)
    with closing(database.get_connection(log_path)) as db:
        row = db.execute(
            "SELECT details_json,correlation_id,request_path FROM application_logs WHERE source='lynewright.catalogue.install' ORDER BY id DESC LIMIT 1",
        ).fetchone()
    assert row is not None
    detail = json.loads(str(row["details_json"]))
    assert row["correlation_id"] == "u5b-request-identity"
    assert row["request_path"] == "/api/catalogue/entries/install"
    assert detail["error_type"] == "ConnectError"
    assert "connection refused" in detail["error_message"]
    for db in opened:
        db.close()


def test_catalogue_streaming_file_limit_aborts_without_install_or_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    temp_root = _watch_http_temp_files(monkeypatch, tmp_path)
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_FILE_MAX_BYTES", "32")
    body = b"x" * 33
    route = "/artifacts/oversize.yaml"
    entry = _entry("file", body)
    entry["source"] = f"https://catalogue.example.test{route}"
    operator_key_id = _configure_http_test_key(monkeypatch)
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry], verified_key_id=operator_key_id)
    with _local_artifact_server({route: body}) as origin:
        catalogue = _real_http_catalogue(entry, manager, monkeypatch, origin, verified_key_id=operator_key_id)
        with pytest.raises(PlatformOperationError, match="size limit"):
            CatalogueInstaller(catalogue, manager).install(entry["id"], entry["version"])
    assert manager.integrations.list() == []
    assert list(temp_root.iterdir()) == []
    for db in opened:
        db.close()


def test_catalogue_streaming_wheel_limit_aborts_without_install_or_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    temp_root = _watch_http_temp_files(monkeypatch, tmp_path)
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_WHEEL_MAX_BYTES", "32")
    body = b"w" * 33
    route = "/artifacts/lynewright_fixture_native-1.0.0-py3-none-any.whl"
    entry = _entry("package", body, integration_id="lynewright.integration.oversize-wheel")
    entry["source"] = f"https://catalogue.example.test{route}"
    operator_key_id = _configure_http_test_key(monkeypatch)
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry], verified_key_id=operator_key_id)
    with _local_artifact_server({route: body}) as origin:
        catalogue = _real_http_catalogue(entry, manager, monkeypatch, origin, verified_key_id=operator_key_id)
        with pytest.raises(PlatformOperationError, match="size limit"):
            CatalogueInstaller(catalogue, manager).install(entry["id"], entry["version"])
    assert manager.integrations.list() == []
    assert not (tmp_path / "packages").exists()
    assert list(temp_root.iterdir()) == []
    for db in opened:
        db.close()


def test_catalogue_streaming_hash_mismatch_leaves_nothing_installed_or_staged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    temp_root = _watch_http_temp_files(monkeypatch, tmp_path)
    body = (Path(__file__).parent / "fixtures" / "integration_files" / "openapi.yaml").read_bytes()
    route = "/artifacts/wrong-hash.yaml"
    entry = _entry("file", body)
    entry["source"] = f"https://catalogue.example.test{route}"
    entry["sha256"] = "0" * 64
    operator_key_id = _configure_http_test_key(monkeypatch)
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry], verified_key_id=operator_key_id)
    with _local_artifact_server({route: body}) as origin:
        catalogue = _real_http_catalogue(entry, manager, monkeypatch, origin, verified_key_id=operator_key_id)
        with pytest.raises(ValueError, match="SHA-256"):
            CatalogueInstaller(catalogue, manager).install(entry["id"], entry["version"])
    assert manager.integrations.list() == []
    assert list(temp_root.iterdir()) == []
    for db in opened:
        db.close()


def test_catalogue_redirect_to_private_ip_is_refused_and_temp_dir_is_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    temp_root = _watch_http_temp_files(monkeypatch, tmp_path)
    entry = _entry("file", b"unused")
    entry["source"] = "https://catalogue.example.test/redirect-private"
    operator_key_id = _configure_http_test_key(monkeypatch)
    manager, opened = _manager(tmp_path)
    _cache_index(manager, [entry], verified_key_id=operator_key_id)
    with _local_artifact_server({}) as origin:
        catalogue = _real_http_catalogue(entry, manager, monkeypatch, origin, verified_key_id=operator_key_id)
        with pytest.raises(PlatformValidationError, match="pinned origin"):
            CatalogueInstaller(catalogue, manager).install(entry["id"], entry["version"])
    assert manager.integrations.list() == []
    assert list(temp_root.iterdir()) == []
    for db in opened:
        db.close()


@pytest.mark.parametrize("route, timeout", [("/upstream-error", 1.0), ("/slow", 0.1)])
def test_stream_download_cleans_temp_dir_on_http_error_and_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str, timeout: float,
) -> None:
    from app.services.platform_services import PlatformOperationError

    temp_root = _watch_http_temp_files(monkeypatch, tmp_path)
    with _local_artifact_server({}) as origin:
        monkeypatch.setenv("LYNEWRIGHT_INTERNAL_ENDPOINT__K55_DIRECT_TEST", origin)
        with pytest.raises(PlatformOperationError):
            with HTTPService().stream_to_temp_file(
                origin + route, max_bytes=1024, timeout_seconds=timeout,
                internal_endpoint_ref="k55_direct_test",
            ):
                pytest.fail("failed HTTP response should not yield a downloaded artifact")
    assert list(temp_root.iterdir()) == []


def test_catalogue_download_limits_default_and_clamp_to_hard_maxima(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.delenv("LYNEWRIGHT_CATALOGUE_FILE_MAX_BYTES", raising=False)
    monkeypatch.delenv("LYNEWRIGHT_CATALOGUE_WHEEL_MAX_BYTES", raising=False)
    settings = get_settings()
    assert settings.catalogue_file_max_bytes == 1_048_576
    assert settings.catalogue_wheel_max_bytes == 100 * 1024 * 1024
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_FILE_MAX_BYTES", str(5 * 1024 * 1024))
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_WHEEL_MAX_BYTES", str(200 * 1024 * 1024))
    settings = get_settings()
    assert settings.catalogue_file_max_bytes == 1_048_576
    assert settings.catalogue_wheel_max_bytes == 100 * 1024 * 1024


def test_real_signed_k55_index_refresh_then_installs_file_and_native_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Use the checked-in minisign output; HTTP is stubbed only at the transport boundary."""
    import sys
    from app.services.catalogue import CatalogueService

    fixture_root = Path(__file__).parent / "fixtures" / "catalogue-k55"
    index_bytes = (fixture_root / "catalogue.json").read_bytes()
    signature = (fixture_root / "catalogue.json.minisig").read_bytes()
    public_lines = (fixture_root / "public.key").read_text(encoding="ascii").splitlines()
    public_key = public_lines[-1]
    url = "https://catalogue.example.invalid/catalogue.json"
    fake = _HTTP({
        url: index_bytes,
        url + ".minisig": signature,
    })
    index = json.loads(index_bytes)
    for entry in index["entries"]:
        artifact_path = fixture_root / "artifacts" / Path(entry["source"]).name
        fake.artifacts[entry["source"]] = artifact_path.read_bytes()

    db_path = tmp_path / "catalogue-install.sqlite3"
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(db_path))
    monkeypatch.setenv("LYNEWRIGHT_PACKAGES_DIR", str(tmp_path / "packages"))
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_URL", url)
    monkeypatch.setenv("LYNEWRIGHT_CATALOGUE_KEYS", public_key)
    monkeypatch.setenv("LYNEWRIGHT_INSTALL_POLICY", "catalogue-only")
    manager, opened = _manager(tmp_path)
    catalogue = CatalogueService(http_service=fake)

    status = catalogue.refresh()
    assert status["status"] == "ready", status
    assert status["serial"] == 1
    assert status["verified_key_id"] and not status["verified_key_id"].startswith("dev:")
    assert {item["kind"] for item in catalogue.list_installable()} == {"file", "package"}

    file_entry = next(item for item in catalogue.list_installable() if item["id"] == "lynewright.integration.template-openapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import catalogue as catalogue_router
    from app.routers import integration_management as management_router

    monkeypatch.setattr(catalogue_router, "CatalogueService", lambda: catalogue)
    monkeypatch.setattr(management_router, "_service", lambda _context: manager)
    api = FastAPI()
    api.include_router(catalogue_router.router)
    with TestClient(api) as client:
        api.dependency_overrides[catalogue_router._ADMIN] = lambda: {
            "tenant_id": manager.tenant_id, "user_id": manager.actor_id,
        }
        response = client.post(f"/api/catalogue/entries/{file_entry['id']}/{file_entry['version']}/install")
    assert response.status_code == 201, response.text
    assert response.json()["integration"]["integration"]["id"] == file_entry["id"]
    file_provenance = get_provenance(manager.store, manager.tenant_id, file_entry["id"])
    assert file_provenance and file_provenance["kind"] == "catalogue"
    assert file_provenance["sha256"] == file_entry["sha256"]

    package_entry = next(item for item in catalogue.list_installable() if item["kind"] == "package")
    package_result = CatalogueInstaller(catalogue, manager).install(package_entry["id"], package_entry["version"])
    package = manager.integrations.get(package_entry["id"])
    stored = package["configuration"]["catalogue_package"]
    wheel = Path(stored["wheel_directory"]) / stored["wheel_filename"]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == package_entry["sha256"]
    assert package_result["provenance"]["kind"] == "catalogue"
    assert "example_native.provider" not in sys.modules
    for db in opened:
        db.close()
