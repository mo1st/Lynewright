from __future__ import annotations

import sqlite3

from starlette.testclient import TestClient

from app.mcp_server import create_mcp_server
from app.services.integration_contributions import ContributionRegistry
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformStore


def _stack(*, with_contribution: bool, max_tool_calls_per_minute: int = 60):
    from app.mcp_server import create_mcp_server
    from app.services.integration_registry import IntegrationRegistry

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integrations = IntegrationRegistry(store)
    integration_id = "lynewright.integration.transport-acceptance"
    integrations.register({
        "schema_version": "1.0", "id": integration_id, "name": "Transport",
        "version": "0.1.0", "type": "service", "lynewright_version": "^0.1.0",
        "sdk_version": "^1.0.0", "requested_capabilities": ["jobs:create"],
        "service": {"kind": "http", "health_check": "/health"},
        "backend": "dist/backend.mjs",
    })
    integrations.enable(integration_id)
    contributions = ContributionRegistry(integrations)
    if with_contribution:
        contributions.register(
            integration_id=integration_id, local_id="tool", kind="mcp",
            metadata={
                "name": "lynewright.integration.transport.accept",
                "description": "Accept a transport operation.", "input_schema": {"type": "object"},
                "caller_capability": "system:read", "integration_capability": "jobs:create",
                "project_scope": "none",
            },
            handler=lambda _sdk, _args: {"ok": True},
        )
    mcp, registry = create_mcp_server(
        store, contributions=contributions,
        max_tool_calls_per_minute=max_tool_calls_per_minute,
    )
    return db, integrations, contributions, mcp, registry


def test_streamable_transport_discovers_contributions_through_generic_tools() -> None:
    db, _integrations, _contributions, mcp, registry = _stack(with_contribution=True)
    client = registry.clients.create_client(
        {"name": "transport", "permission_mode": "custom", "granted_capabilities": ["system:read"]},
        actor=CapabilityActor.platform_manager(),
    )
    headers = {
        "Authorization": f"Bearer {client['access_token']}",
        "Accept": "application/json, text/event-stream",
    }
    initialize = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    }
    listed = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    with TestClient(mcp.streamable_http_app(), base_url="http://127.0.0.1:8000") as http:
        assert http.post("/mcp", headers=headers, json=initialize).status_code == 200
        tool_names = {item["name"] for item in http.post("/mcp", headers=headers, json=listed).json()["result"]["tools"]}
        assert "lynewright.integrations.search_operations" in tool_names
        assert "lynewright.integration.transport.accept" not in tool_names
    actor = registry.clients.authenticate(client["access_token"])
    result = registry.invoke(
        "lynewright.integrations.search_operations", actor,
        {"project_id": "project-1", "query": "transport accept"},
    )
    assert any(item["operation"] == "lynewright.integration.transport.accept" for item in result["operations"])
    db.close()


def test_contributed_and_core_tools_share_the_mcp_transport_rate_limiter(monkeypatch) -> None:
    class OneCallRateLimiter:
        def __init__(self) -> None:
            self.calls = []

        def consume(self, **kwargs):
            self.calls.append(kwargs)
            return len(self.calls) == 1

    limiter = OneCallRateLimiter()
    monkeypatch.setattr("app.services.rate_limiter.get_rate_limiter", lambda: limiter)
    db, _integrations, _contributions, mcp, registry = _stack(
        with_contribution=True, max_tool_calls_per_minute=1,
    )
    client = registry.clients.create_client(
        {"name": "rate", "permission_mode": "full"},
        actor=CapabilityActor.platform_manager(),
    )
    headers = {
        "Authorization": f"Bearer {client['access_token']}",
        "Accept": "application/json, text/event-stream",
    }
    initialize = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    }
    first_call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "lynewright.integrations.search_operations", "arguments": {
            "arguments": {"project_id": "project-1", "query": "transport accept"},
        }},
    }
    second_call = {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "lynewright.integrations.describe_operation", "arguments": {
            "arguments": {"project_id": "project-1", "operation_id": "missing"},
        }},
    }
    with TestClient(mcp.streamable_http_app(), base_url="http://127.0.0.1:8000") as http:
        assert http.post("/mcp", headers=headers, json=initialize).status_code == 200
        first = http.post("/mcp", headers=headers, json=first_call)
        assert first.status_code == 200 and not first.json()["result"].get("isError", False)
        second = http.post("/mcp", headers=headers, json=second_call)
        assert second.status_code == 200 and second.json()["result"]["isError"] is True, (second.json(), limiter.calls)
    assert len(limiter.calls) == 2
    assert all(call["namespace"] == "mcp_tool_call" for call in limiter.calls)
    assert all(call["maximum"] == 1 and call["window_seconds"] == 60 for call in limiter.calls)
    db.close()


def test_contributions_do_not_add_provider_specific_mcp_tool_names() -> None:
    db_without, _ir_without, _c_without, _mcp_without, empty_registry = _stack(with_contribution=False)
    db_with, _ir_with, _c_with, _mcp_with, contributed_registry = _stack(with_contribution=True)
    without = {definition.name for definition in empty_registry.definitions()}
    with_contribution = {definition.name for definition in contributed_registry.definitions()}
    assert with_contribution == without
    assert "lynewright.integrations.search_operations" in with_contribution
    assert "lynewright.integration.transport.accept" not in with_contribution
    db_without.close()
    db_with.close()
