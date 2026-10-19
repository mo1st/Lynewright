"""Outbound MCP client service, deliberately separate from Lynewright MCP Server."""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from typing import Any

from app.services.mcp_client import MCPClient, MCPError
from app.services.platform_services import CredentialService, HTTPService, PlatformConflictError, PlatformNotFoundError, PlatformOperationError, PlatformStore, PlatformValidationError


class MCPClientService:
    def __init__(self, store: PlatformStore | None = None, *, credentials: CredentialService | None = None, client_factory: Callable[[str, str], MCPClient] | None = None) -> None:
        self.store = store or PlatformStore()
        self.credentials = credentials or CredentialService(self.store)
        self.client_factory = client_factory or (lambda endpoint, token: MCPClient(endpoint, token, timeout=20))

    def list_servers(self) -> list[dict[str, Any]]:
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM platform_mcp_servers ORDER BY name,id").fetchall()
        return [self._public(row) for row in rows]

    def get_server_status(self, server_id: str) -> dict[str, Any]:
        return self._public(self._row(server_id, require_enabled=False))

    def configure_server(self, *, name: str, endpoint_url: str, credential_name: str = "", enabled: bool = True) -> dict[str, Any]:
        if not name.strip() or len(name.strip()) > 120:
            raise PlatformValidationError("MCP server name is required and must be at most 120 characters.")
        HTTPService().validate_public_url(endpoint_url)
        if credential_name and not self.credentials.get_status(credential_name)["configured"]:
            raise PlatformValidationError("Configured credential was not found.")
        server_id = f"mcp_{secrets.token_hex(12)}"
        try:
            with self.store.connection() as db, db:
                db.execute("INSERT INTO platform_mcp_servers (id,name,endpoint_url,credential_name,enabled,status) VALUES (?,?,?,?,?,?)", (server_id, name.strip(), endpoint_url.rstrip("/"), credential_name, int(enabled), "configured" if enabled else "disabled"))
        except sqlite3.IntegrityError as exc:
            raise PlatformConflictError("An MCP server with that name already exists.") from exc
        return self.get_server_status(server_id)

    async def test_connection(self, server_id: str) -> dict[str, Any]:
        server = self._row(server_id)
        try:
            initialized, tools = await self._with_client(server, self._discover)
        except MCPError as exc:
            self._record(server_id, "error", "", str(exc))
            raise PlatformOperationError("External MCP connection failed.") from exc
        self._record(server_id, "connected", str(initialized.get("protocolVersion") or ""), "")
        return self.get_server_status(server_id) | {"tool_count": len(tools)}

    async def list_tools(self, server_id: str) -> list[dict[str, Any]]:
        _, tools = await self._with_client(self._row(server_id), self._discover)
        return [{"name": str(tool.get("name") or ""), "description": str(tool.get("description") or ""), "input_schema": tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}} for tool in tools]

    async def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        if not tool_name or len(tool_name) > 200 or len(json.dumps(arguments or {}).encode("utf-8")) > 64 * 1024:
            raise PlatformValidationError("Invalid external MCP tool input.")
        async def call(client: MCPClient) -> Any:
            await client.initialize()
            return await client.call_tool(tool_name, arguments or {})
        try:
            return await self._with_client(self._row(server_id), call)
        except MCPError as exc:
            self._record(server_id, "error", "", str(exc))
            raise PlatformOperationError("External MCP tool call failed.") from exc

    def disconnect(self, server_id: str) -> dict[str, Any]:
        self._row(server_id, require_enabled=False)
        self._record(server_id, "disconnected", "", "")
        return self.get_server_status(server_id)

    async def _with_client(self, server: sqlite3.Row, operation: Callable[[MCPClient], Any]) -> Any:
        async def run(token: str) -> Any:
            return await operation(self.client_factory(str(server["endpoint_url"]), token))
        name = str(server["credential_name"] or "")
        return await self.credentials.use(name, run) if name else await run("")

    @staticmethod
    async def _discover(client: MCPClient) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return await client.initialize(), await client.list_tools()

    def _row(self, server_id: str, *, require_enabled: bool = True) -> sqlite3.Row:
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM platform_mcp_servers WHERE id=?", (server_id,)).fetchone()
        if row is None:
            raise PlatformNotFoundError("MCP server not found.")
        if require_enabled and not bool(row["enabled"]):
            raise PlatformOperationError("MCP server is disabled.")
        return row

    def _record(self, server_id: str, status: str, protocol_version: str, error: str) -> None:
        with self.store.connection() as db, db:
            db.execute("UPDATE platform_mcp_servers SET status=?,protocol_version=?,last_error=?,last_checked_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, protocol_version, error[:500], server_id))

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": str(row["id"]), "name": str(row["name"]), "endpoint_url": str(row["endpoint_url"]), "credential_name": str(row["credential_name"]), "credential_configured": bool(str(row["credential_name"])), "enabled": bool(row["enabled"]), "status": str(row["status"]), "protocol_version": str(row["protocol_version"]), "last_error": str(row["last_error"]), "last_checked_at": row["last_checked_at"], "updated_at": row["updated_at"]}
