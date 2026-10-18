from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn


ROOT = Path(__file__).resolve().parents[1]


def test_declaration_contains_sync_and_async_operations() -> None:
    import yaml

    declaration = yaml.safe_load((ROOT / "lynewright-integration.yaml").read_text(encoding="utf-8"))
    assert {item["operation_id"] for item in declaration["operations"]} == {"health", "startJob", "jobStatus"}
    assert declaration["async"] == {
        "operation_id": "startJob",
        "status_operation_id": "jobStatus",
        "job_id_path": "job_id",
        "status_argument_path": "path.job_id",
    }


@pytest.mark.lynewright
def test_openapi_sync_and_async_operations_run_in_lines(lynewright) -> None:
    from example_openapi.app import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    endpoint = f"http://{host}:{port}"
    try:
        integration_id = lynewright.import_file(ROOT / "lynewright-integration.yaml", endpoint_url=endpoint)
        lynewright.connections.remove(integration_id, "primary")
        connection_id = lynewright.connect(
            integration_id, connection_id="primary",
            internal_endpoint="openapi_stub", endpoint_url=endpoint,
        )
        lynewright.authorize(integration_id, ["network:use"])
        lynewright.enable(integration_id, activate_native=False)
        lynewright.openapi_approve(integration_id, connection_id, ["health", "startJob", "jobStatus"])
        operations = lynewright.operations(integration_id=integration_id)
        health = next(item for item in operations if item["operation"] == "health")
        health_run = lynewright.run_line(health)
        assert health_run["status"] == "done", health_run
        assert health_run["stages"][0]["output"] == {"status": "ok"}

        start = next(item for item in operations if item["operation"] == "startJob")
        run = lynewright.run_line(
            start, arguments={"body": {"value": "starter-job"}},
            stage_kind="async_operation",
        )
        assert run["status"] == "done", run
        assert run["stages"][0]["output"]["status"] == "done"
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        sock.close()
