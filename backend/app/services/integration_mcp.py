"""Safe, connection-scoped consumption of approved external MCP servers.

This adapter is deliberately not connected to Lynewright's own MCP server.  An
integration may inspect and call the tools it explicitly approves, but those
tools never become public Lynewright MCP tools implicitly.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_sdk import IntegrationSDK
from app.services.mcp_client import MCPError, MCP_PROTOCOL_VERSION, _decode_tool_result, _parse_response, _tool_error_message
from app.services.platform_services import PlatformOperationError, PlatformValidationError


_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
_MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(slots=True)
class _MCPSession:
    adapter: "IntegrationMCPAdapter"
    sdk: IntegrationSDK
    connection: dict[str, Any]
    token: str
    session_id: str = ""
    request_id: int = 0

    def initialize(self) -> dict[str, Any]:
        result, headers = self.rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "Lynewright", "version": "0.1.0"},
            },
            include_headers=True,
        )
        self.session_id = headers.get("mcp-session-id", "").strip()
        protocol = str(result.get("protocolVersion") or "")
        if not protocol:
            raise PlatformOperationError("MCP initialize response did not include a protocol version.")
        self.notify_initialized()
        return result

    def notify_initialized(self) -> None:
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def rpc(self, method: str, params: dict[str, Any], *, include_headers: bool = False) -> Any:
        self.request_id += 1
        body, headers, status_code = self.post({
            "jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params,
        })
        if not isinstance(body, dict):
            raise PlatformOperationError(f"MCP {method} returned an invalid JSON-RPC response.")
        error = body.get("error")
        if error:
            message = str(error.get("message") if isinstance(error, dict) else error)
            raise PlatformOperationError(message or f"MCP {method} failed.")
        result = body.get("result")
        if not isinstance(result, dict):
            raise PlatformOperationError(f"MCP {method} response did not include an object result.")
        return (result, headers) if include_headers else result

    def post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], int]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        try:
            internal_endpoint_ref = (
                str(self.connection.get("internal_endpoint_ref") or "")
                if str(self.connection.get("origin_kind") or "public") == "internal_endpoint"
                else ""
            )
            response = self.sdk.http.request(
                str(self.connection["endpoint_url"]), method="POST", headers=headers,
                json_body=payload, max_bytes=_MAX_RESPONSE_BYTES, timeout_seconds=20,
                **({"internal_endpoint_ref": internal_endpoint_ref} if internal_endpoint_ref else {}),
            )
        except Exception as exc:
            raise PlatformOperationError("External MCP request failed.") from exc
        status_code = int(response["status_code"])
        if 300 <= status_code < 400:
            raise PlatformOperationError("MCP endpoint redirects are not allowed; save the final HTTPS URL.")
        if status_code in {401, 403}:
            raise PlatformOperationError("External MCP authentication failed.")
        if status_code >= 400:
            raise PlatformOperationError(f"External MCP request failed with HTTP {status_code}.")
        raw = response.get("body") or b""
        if status_code in {202, 204} and not raw:
            return {}, self._headers(response), status_code
        try:
            return _parse_response(raw, str(response.get("content_type") or "")), self._headers(response), status_code
        except MCPError as exc:
            raise PlatformOperationError(str(exc)) from exc

    @staticmethod
    def _headers(response: dict[str, Any]) -> dict[str, str]:
        return {str(key).lower(): str(value) for key, value in dict(response.get("headers") or {}).items()}


class IntegrationMCPAdapter:
    """Consumes an explicitly-approved external MCP connection through the SDK."""

    def __init__(self, connections: ConnectionProfileService, *, clock: callable = time.monotonic) -> None:
        self.connections = connections
        self.clock = clock

    def discover(self, sdk: IntegrationSDK, *, connection_id: str | None = None) -> dict[str, Any]:
        selected = connection_id or sdk.connection_id
        started = self.clock()
        try:
            result = self._with_session(sdk, selected, self._discover)
            connection = self.connections.record_health(
                sdk.integration_id, selected, state="HEALTHY",
                latency_ms=max(0, int((self.clock() - started) * 1000)),
                remote_version=str(result["protocol_version"]),
            )
            return {**result, "connection": connection}
        except Exception as exc:
            self._record_failure(sdk, selected, exc)
            raise

    def call_tool(
        self,
        sdk: IntegrationSDK,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        connection_id: str | None = None,
        require_read_only: bool = False,
        record_health: bool = True,
    ) -> Any:
        selected = connection_id or sdk.connection_id
        if _TOOL_NAME_RE.fullmatch(tool_name) is None:
            raise PlatformValidationError("External MCP tool name is invalid.")
        if not isinstance(arguments or {}, dict):
            raise PlatformValidationError("External MCP tool arguments must be an object.")
        try:
            if len(json.dumps(arguments or {}, sort_keys=True).encode("utf-8")) > _MAX_TOOL_ARGUMENT_BYTES:
                raise PlatformValidationError("External MCP tool arguments exceed the 64 KiB limit.")
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("External MCP tool arguments must be JSON serializable.") from exc
        connection = self._connection(sdk, selected)
        approved = self._approved_tools(connection)
        if tool_name not in approved:
            raise PlatformOperationError("External MCP tool is not approved for this connection.")
        if require_read_only and not self._is_explicit_read_only(connection, tool_name):
            raise PlatformOperationError(
                "MCP probe requires an approved tool explicitly marked read-only and non-destructive."
            )
        started = self.clock()
        try:
            result = self._with_session(sdk, selected, lambda session: self._call(session, tool_name, arguments or {}))
            if record_health:
                self.connections.record_health(
                    sdk.integration_id, selected, state="HEALTHY",
                    latency_ms=max(0, int((self.clock() - started) * 1000)),
                )
            return result
        except Exception as exc:
            if record_health:
                self._record_failure(sdk, selected, exc)
            raise

    def tool_input_schema(
        self,
        sdk: IntegrationSDK,
        *,
        tool_name: str,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Read one live input schema without persisting untrusted remote tool list data."""
        selected = connection_id or sdk.connection_id
        if _TOOL_NAME_RE.fullmatch(tool_name) is None:
            raise PlatformValidationError("External MCP tool name is invalid.")
        return self._with_session(
            sdk,
            selected,
            lambda session: self._input_schema(session, tool_name),
        )

    def health(self, sdk: IntegrationSDK, *, connection_id: str | None = None) -> dict[str, Any]:
        selected = connection_id or sdk.connection_id
        try:
            discovered = self.discover(sdk, connection_id=selected)
            return {"state": "HEALTHY", "protocol_version": discovered["protocol_version"], "connection": discovered["connection"]}
        except Exception as exc:
            return {"state": "UNAVAILABLE", "error": type(exc).__name__, "connection": self.connections.get(sdk.integration_id, selected)}

    def _with_session(self, sdk: IntegrationSDK, connection_id: str | None, operation: Any) -> Any:
        connection = self._connection(sdk, connection_id)
        credential_ref = str(connection["credential_refs"][0]) if connection["credential_refs"] else ""

        def run(token: str) -> Any:
            return operation(_MCPSession(self, sdk, connection, token))

        return sdk.credentials.use(credential_ref, run) if credential_ref else run("")

    @staticmethod
    def _discover(session: _MCPSession) -> dict[str, Any]:
        initialized = session.initialize()
        tools = session.rpc("tools/list", {}).get("tools")
        resources = session.rpc("resources/list", {}).get("resources")
        if not isinstance(tools, list) or not isinstance(resources, list):
            raise PlatformOperationError("MCP discovery response did not include tool and resource arrays.")
        approved = IntegrationMCPAdapter._approved_tools(session.connection)
        return {
            "protocol_version": str(initialized["protocolVersion"]),
            "tools": [IntegrationMCPAdapter._tool_metadata(tool, approved) for tool in tools if isinstance(tool, dict)],
            "resources": [IntegrationMCPAdapter._resource_metadata(resource) for resource in resources if isinstance(resource, dict)],
        }

    @staticmethod
    def _call(session: _MCPSession, tool_name: str, arguments: dict[str, Any]) -> Any:
        session.initialize()
        result = session.rpc("tools/call", {"name": tool_name, "arguments": arguments})
        if result.get("isError"):
            raise PlatformOperationError(_tool_error_message(result))
        return _decode_tool_result(result)

    @staticmethod
    def _input_schema(session: _MCPSession, tool_name: str) -> dict[str, Any]:
        session.initialize()
        tools = session.rpc("tools/list", {}).get("tools")
        if not isinstance(tools, list):
            raise PlatformOperationError("MCP tools/list response did not include a tools array.")
        tool = next(
            (item for item in tools if isinstance(item, dict) and str(item.get("name") or "") == tool_name),
            None,
        )
        if tool is None:
            raise PlatformOperationError("External MCP tool is no longer present in the live remote tool list.")
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            raise PlatformOperationError("External MCP tool does not declare an object input schema.")
        return schema

    def _connection(self, sdk: IntegrationSDK, connection_id: str | None) -> dict[str, Any]:
        if not connection_id:
            raise PlatformOperationError("A connection profile is required for an external MCP request.")
        connection = self.connections.get(sdk.integration_id, connection_id)
        if not connection["enabled"]:
            raise PlatformOperationError("Integration connection is disabled.")
        if not connection["endpoint_url"]:
            raise PlatformValidationError("Integration connection does not define an MCP endpoint URL.")
        if str(connection.get("origin_kind") or "public") == "internal_endpoint":
            if not str(connection.get("internal_endpoint_ref") or "").strip():
                raise PlatformValidationError("Internal MCP connections must name a configured endpoint reference.")
        else:
            # Public MCP connections use the standard SSRF-safe URL policy.
            sdk.http.validate_url(str(connection["endpoint_url"]))
        return connection

    @staticmethod
    def _approved_tools(connection: dict[str, Any]) -> set[str]:
        mcp = connection.get("configuration", {}).get("mcp", {})
        values = mcp.get("approved_tools", []) if isinstance(mcp, dict) else []
        if not isinstance(values, list) or any(not isinstance(value, str) or _TOOL_NAME_RE.fullmatch(value) is None for value in values):
            raise PlatformValidationError("MCP approved_tools must be a list of valid tool names.")
        return set(values)

    @staticmethod
    def _tool_metadata(tool: dict[str, Any], approved: set[str]) -> dict[str, Any]:
        name = str(tool.get("name") or "")
        annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        read_only = annotations.get("readOnlyHint", tool.get("readOnlyHint"))
        destructive = annotations.get("destructiveHint", tool.get("destructiveHint"))
        read_only_value = read_only if isinstance(read_only, bool) else None
        destructive_value = destructive if isinstance(destructive, bool) else None
        input_schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        schema_type = input_schema.get("type") if isinstance(input_schema.get("type"), str) else None
        return {
            "name": name,
            "description": str(tool.get("description") or "")[:2_000],
            # Provider schemas are untrusted and may be arbitrarily large. The
            # management connection stores discovery metadata in a bounded
            # record; the probe deliberately uses the shared scalar validator
            # instead of treating a remote schema as an execution language.
            "input_schema": {"type": schema_type} if schema_type else {},
            "approved": name in approved,
            "read_only": read_only_value,
            "destructive": destructive_value,
            "safety_known": read_only_value is not None and destructive_value is not None,
        }

    @staticmethod
    def _is_explicit_read_only(connection: dict[str, Any], tool_name: str) -> bool:
        metadata = connection.get("metadata") if isinstance(connection.get("metadata"), dict) else {}
        mcp = metadata.get("mcp") if isinstance(metadata, dict) else {}
        tools = mcp.get("tools", []) if isinstance(mcp, dict) else []
        tool = next((item for item in tools if isinstance(item, dict) and item.get("name") == tool_name), None)
        return bool(
            isinstance(tool, dict)
            and tool.get("read_only") is True
            and tool.get("destructive") is False
            and tool.get("safety_known") is True
        )

    @staticmethod
    def _resource_metadata(resource: dict[str, Any]) -> dict[str, Any]:
        return {
            "uri": str(resource.get("uri") or "")[:2_000],
            "name": str(resource.get("name") or "")[:500],
            "description": str(resource.get("description") or "")[:2_000],
            "mime_type": str(resource.get("mimeType") or "")[:200],
        }

    def _record_failure(self, sdk: IntegrationSDK, connection_id: str | None, exc: Exception) -> None:
        if connection_id:
            self.connections.record_health(
                sdk.integration_id, connection_id, state="UNAVAILABLE", error_code=type(exc).__name__.lower(),
            )
