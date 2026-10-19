"""Isolated end-to-end driver for the CLI wrapper integration."""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

import uvicorn
import yaml
from starlette.testclient import TestClient


BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
TEMPLATE = REPO / "templates" / "cli-wrapper"
sys.path.insert(0, str(BACKEND))


def _png_1px() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + chunk(b"IEND", b"")


def _wait(client: TestClient, token: str, run_id: str, terminal: set[str], label: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + 35
    latest = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/production-lines/runs/{run_id}", headers=headers)
        if response.status_code != 200:
            raise AssertionError(f"{label}: HTTP {response.status_code}: {response.text[:500]}")
        latest = response.json()
        if latest["status"] in terminal:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {label}: {json.dumps(latest, default=str)[:1500]}")


def _start_wrapper() -> tuple[uvicorn.Server, threading.Thread, socket.socket, str, Any]:
    module_spec = importlib.util.spec_from_file_location("lynewright_cli_wrapper_e2e", TEMPLATE / "app.py")
    assert module_spec and module_spec.loader
    wrapper = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = wrapper
    module_spec.loader.exec_module(wrapper)
    image = _png_1px()
    script = (
        "import pathlib,sys; pathlib.Path(sys.argv[2]).write_bytes("
        "pathlib.Path(sys.argv[1]).read_bytes())"
    )
    command = {
        "argv": [sys.executable, "-c", script, "{input}", "{output}"],
        "params": {"task": {"type": "enum", "enum": ["thumbnail"]}},
        "inputs": {"input": {"extensions": [".png"], "max_bytes": 200000, "required": True}},
        "outputs": {"glob": "*.png"},
        "limits": {"timeout_seconds": 8, "max_output_bytes": 200000, "max_concurrent_jobs": 2},
    }
    api = wrapper.create_app(
        command,
        work_root=Path(os.environ["LYNEWRIGHT_MANAGED_MEDIA_DIR"]).parent / "wrapper-work",
        environ={"WRAPPER_ALLOW_PRIVATE_FETCH": "0"},
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(api, log_level="info", access_log=True, lifespan="on"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urlopen(origin + "/health", timeout=0.5) as response:
                if response.status == 200:
                    return server, thread, listener, origin, api
        except OSError:
            time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=3)
    listener.close()
    raise AssertionError("CLI wrapper did not start.")


def run() -> None:
    server, thread, listener, origin, wrapper_api = _start_wrapper()
    os.environ["LYNEWRIGHT_INTERNAL_ENDPOINT__CLI_WRAPPER"] = origin
    try:
        from app.main import app
        from app.services.platform_services import HTTPService, MediaService, PlatformStore
        from app.services.tenancy import create_tenant_project
        from app.services.tenant_schema import DEFAULT_TENANT_ID

        project_id = str(create_tenant_project(
            tenant_id=DEFAULT_TENANT_ID, name="CLI Wrapper E2E", project_id="cli-wrapper-e2e",
        )["project_id"])
        media_root = Path(os.environ["LYNEWRIGHT_MANAGED_MEDIA_DIR"])
        media_root.mkdir(parents=True, exist_ok=True)
        source = media_root / "source.stored"
        source.write_bytes(_png_1px())
        media = MediaService(PlatformStore(), tenant_id=DEFAULT_TENANT_ID).create_managed(
            name="tiny.png", content_type="image/png",
            metadata={"stored_as": source.name, "storage_kind": "managed_file"},
            project_id=project_id,
        )

        declaration = yaml.safe_load((TEMPLATE / "lynewright-integration.yaml").read_text(encoding="utf-8"))
        with TestClient(app) as client:
            probe = HTTPService().request(
                origin + "/health", method="GET", internal_endpoint_ref="cli_wrapper",
            )
            assert probe["status_code"] == 200, probe
            login = client.post("/api/auth/login", json={
                "email": os.environ["LYNEWRIGHT_BOOTSTRAP_EMAIL"],
                "password": os.environ["LYNEWRIGHT_BOOTSTRAP_PASSWORD"],
            })
            assert login.status_code == 200, login.text
            token = login.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            declaration_bytes = yaml.safe_dump(declaration, sort_keys=False).encode("utf-8")
            imported = client.post(
                "/api/integration-management/import",
                content=declaration_bytes,
                headers={**headers, "Content-Type": "application/yaml"},
            )
            if imported.status_code == 409 and imported.json().get("detail", {}).get("requires_confirmation"):
                detail = imported.json()["detail"]
                confirmation = {
                    "integration_id": detail["install_summary"]["integration_id"],
                    "summary_hash": detail["summary_hash"],
                }
                imported = client.post(
                    "/api/integration-management/import",
                    content=declaration_bytes,
                    headers={
                        **headers,
                        "Content-Type": "application/yaml",
                        "X-Lynewright-Install-Confirm": json.dumps(confirmation),
                    },
                )
            assert imported.status_code == 201, imported.text
            integration_id = imported.json()["integration"]["integration"]["id"]
            connection = client.post(
                f"/api/integration-management/{integration_id}/connections",
                headers=headers,
                json={
                    "name": "Local CLI wrapper",
                    "connection_id": "cli-wrapper",
                    "project_id": project_id,
                    "origin_kind": "internal_endpoint",
                    "internal_endpoint_ref": "cli_wrapper",
                },
            )
            assert connection.status_code == 200, connection.text
            assert client.post(
                f"/api/integration-management/{integration_id}/authorization",
                headers=headers,
                json={"permission_mode": "custom", "granted_capabilities": ["network:use"]},
            ).status_code == 200
            assert client.post(
                f"/api/integration-management/{integration_id}/enable", headers=headers,
            ).status_code == 200
            approved = client.post(
                f"/api/integration-management/{integration_id}/connections/cli-wrapper/openapi/approve",
                headers=headers,
                json={"operation_ids": ["health", "submitJob", "getJob", "getOutputFile"]},
            )
            assert approved.status_code == 200, approved.text

            operations = client.get(
                "/api/production-lines/operations",
                headers=headers, params={"project_id": project_id},
            )
            assert operations.status_code == 200, operations.text
            start = next(
                item for item in operations.json()["items"]
                if item.get("integration_id") == integration_id
                and item.get("connection_id") == "cli-wrapper"
                and item.get("operation") == "submitJob"
            )
            upload_schema = start["input_schema"]["properties"]["body"]["properties"]["input"]
            assert upload_schema.get("format") == "lynewright-upload", upload_schema
            assert start.get("async", {}).get("status_operation_id") == "getJob", start
            definition = {"stages": [
                {
                    "id": "thumbnail",
                    "kind": "async_operation",
                    "contract": start["contract"],
                    "operation": start["operation"],
                    "operation_id": start["operation_id"],
                    "args_template": {
                        "body": {
                            "args": json.dumps({"task": "thumbnail"}),
                            "input": "{{run.vars.source_media}}",
                        },
                    },
                },
                {
                    "id": "review",
                    "kind": "gate:review",
                    "items_template": [{
                        "id": "thumbnail-output",
                        "label": "CLI thumbnail",
                        "media_refs": "{{stages.thumbnail.output.media_refs}}",
                    }],
                },
            ]}
            saved = client.post(
                "/api/production-lines",
                headers=headers,
                json={"id": "cli-wrapper-line", "name": "CLI wrapper line", "project_id": project_id, "definition": definition},
            )
            assert saved.status_code == 201, saved.text
            started = client.post(
                "/api/production-lines/cli-wrapper-line/runs",
                headers=headers,
                json={"project_id": project_id, "vars": {"source_media": {
                    "media_id": str(media["id"]), "url": f"managed://{media['id']}",
                    "name": media["name"], "content_type": media["content_type"],
                }}},
            )
            assert started.status_code == 202, started.text
            run_id = started.json()["id"]
            waiting = _wait(client, token, run_id, {"waiting", "failed", "rejected"}, "review gate")
            if waiting["status"] != "waiting":
                jobs = wrapper_api.state.job_manager.jobs
                print("wrapper jobs:", [
                    {"status": job.status, "error": job.error, "progress": list(job.progress), "outputs": job.outputs}
                    for job in jobs.values()
                ])
            assert waiting["status"] == "waiting", waiting
            review = next(item for item in waiting["stages"] if item["stage_id"] == "review")
            preview = review["output"]["items"][0]["media_previews"][0]
            assert preview["kind"] == "preview" and preview["content_type"] == "image/png", preview
            preview_url = urlsplit(preview["url"])
            preview_response = client.get(preview_url.path + "?" + preview_url.query)
            assert preview_response.status_code == 200, preview_response.text[:300]
            assert preview_response.content.startswith(b"\x89PNG\r\n\x1a\n")
            materialized = waiting["stages"][0]["output"]["media_refs"][0]
            assert materialized["media_id"] and materialized["url"].startswith("managed://")

            approved_review = client.post(
                f"/api/production-lines/runs/{run_id}/continue",
                headers=headers,
                json={"value": {"approved": True, "items": [{"id": "thumbnail-output", "decision": "approved"}]}},
            )
            assert approved_review.status_code == 202, approved_review.text
            completed = _wait(client, token, run_id, {"done", "failed", "rejected"}, "line completion")
            assert completed["status"] == "done", completed
            print("CLI line E2E: multipart managed upload -> async wrapper done -> managed PNG review preview HTTP 200 -> approved -> run done")
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


if __name__ == "__main__":
    run()
