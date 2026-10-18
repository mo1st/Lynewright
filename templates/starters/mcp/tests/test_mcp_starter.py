from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn


ROOT = Path(__file__).resolve().parents[1]


def test_declaration_names_the_example_tool() -> None:
    import yaml

    declaration = yaml.safe_load((ROOT / "lynewright-integration.yaml").read_text(encoding="utf-8"))
    assert declaration["runtime_kind"] == "mcp"
    assert [item["tool_name"] for item in declaration["operations"]] == ["echo"]


@pytest.mark.lynewright
def test_mcp_tool_is_discovered_approved_and_run_in_a_line(lynewright) -> None:
    from example_mcp.server import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
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
            internal_endpoint="mcp_stub", endpoint_url=endpoint,
        )
        lynewright.authorize(integration_id, ["network:use"])
        lynewright.enable(integration_id, activate_native=False)
        lynewright.discover_mcp(integration_id, connection_id)
        lynewright.mcp_approve_and_map(integration_id, connection_id, tool_name="echo")
        operation = next(item for item in lynewright.operations(integration_id=integration_id) if item["operation"] == "invoke")
        run = lynewright.run_line(operation, arguments={"query": "hello from MCP"})
        assert run["status"] == "done", run
        assert run["stages"][0]["output"]["output"] == {"value": "hello from MCP"}, run
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        sock.close()
