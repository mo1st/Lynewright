from __future__ import annotations

import sqlite3
import asyncio

import pytest

from app.services.mcp_client import MCPError
from app.services.platform_mcp_client import MCPClientService
from app.services.platform_services import CredentialService, PlatformOperationError, PlatformStore, PlatformValidationError


class FakeClient:
    def __init__(self, endpoint: str, token: str, fail: bool = False) -> None:
        self.token, self.fail = token, fail

    async def initialize(self):
        if self.fail:
            raise MCPError("network", "offline")
        return {"protocolVersion": "2025-06-18"}

    async def list_tools(self):
        return [{"name": "safe_echo", "description": "fixture", "inputSchema": {"type": "object"}}]

    async def call_tool(self, name, arguments):
        return {"name": name, "arguments": arguments, "credential_used": bool(self.token)}


@pytest.fixture()
def service() -> MCPClientService:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    result = MCPClientService(store, credentials=CredentialService(store), client_factory=lambda endpoint, token: FakeClient(endpoint, token))
    yield result
    db.close()


def test_configure_connect_list_call_and_disconnect(service: MCPClientService) -> None:
    service.credentials.set({"name": "fixture.token", "secret": "not-readable"})
    server = service.configure_server(name="Fixture", endpoint_url="https://example.com/mcp", credential_name="fixture.token")
    assert asyncio.run(service.test_connection(server["id"]))["status"] == "connected"
    assert asyncio.run(service.list_tools(server["id"]))[0]["name"] == "safe_echo"
    assert asyncio.run(service.call_tool(server["id"], "safe_echo", {"safe": True}))["credential_used"] is True
    assert service.disconnect(server["id"])["status"] == "disconnected"
    assert "not-readable" not in str(service.get_server_status(server["id"]))


def test_outbound_client_rejects_private_hosts_and_large_calls(service: MCPClientService) -> None:
    with pytest.raises(PlatformValidationError):
        service.configure_server(name="Private", endpoint_url="http://127.0.0.1/mcp")
    server = service.configure_server(name="Public", endpoint_url="https://example.com/mcp")
    with pytest.raises(PlatformValidationError):
        asyncio.run(service.call_tool(server["id"], "safe_echo", {"large": "x" * (65 * 1024)}))


def test_mcp_client_revalidates_endpoint_on_every_request(monkeypatch) -> None:
    """A hostname that later re-resolves to an internal address is refused."""
    from app.services import url_security
    from app.services.mcp_client import MCPClient

    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    monkeypatch.setattr(url_security, "resolve_hostname_addresses", lambda _host: next(answers))
    client = MCPClient("https://mcp.example.com/mcp")

    first = asyncio.run(client._pinned_transport())
    assert first.network_backend.target.addresses == ("93.184.216.34",)
    with pytest.raises(MCPError) as caught:
        asyncio.run(client._pinned_transport())
    assert caught.value.category == "protocol"
    assert "not allowed" in caught.value.message
