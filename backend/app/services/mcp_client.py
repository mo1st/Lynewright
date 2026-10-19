from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx


MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(slots=True)
class MCPError(Exception):
    category: str
    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        return self.message


class MCPClient:
    """Minimal Streamable HTTP MCP client with a bounded, retryable transport."""

    def __init__(
        self, endpoint: str, token: str = "", *, timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.transport = transport
        self.session_id = ""
        self._request_id = 0
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def initialize(self) -> dict[str, Any]:
        result, response = await self._rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "Lynewright", "version": "0.1.0"},
            },
            include_response=True,
        )
        self.session_id = response.headers.get("mcp-session-id", "").strip()
        protocol = str(result.get("protocolVersion") or "")
        if not protocol:
            raise MCPError("protocol", "MCP initialize response did not include a protocol version.")
        await self.notify_initialized()
        return result

    async def notify_initialized(self) -> None:
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise MCPError("protocol", "MCP tools/list response did not include a tools array.")
        return [item for item in tools if isinstance(item, dict)]

    async def ping(self) -> dict[str, Any]:
        return await self._rpc("ping", {})

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            raise MCPError("tool", _tool_error_message(result))
        return _decode_tool_result(result)

    async def _rpc(
        self, method: str, params: dict[str, Any], *, include_response: bool = False,
    ) -> Any:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0", "id": self._request_id,
            "method": method, "params": params,
        }
        body, response = await self._post(payload)
        if not isinstance(body, dict):
            raise MCPError("protocol", f"MCP {method} returned an invalid JSON-RPC response.")
        error = body.get("error")
        if error:
            message = str(error.get("message") if isinstance(error, dict) else error)
            category = "auth" if response.status_code in {401, 403} else "tool" if method == "tools/call" else "protocol"
            raise MCPError(category, message or f"MCP {method} failed.", response.status_code)
        result = body.get("result")
        if not isinstance(result, dict):
            raise MCPError("protocol", f"MCP {method} response did not include an object result.")
        return (result, response) if include_response else result

    async def _pinned_transport(self) -> httpx.AsyncBaseTransport:
        """Validate the endpoint on every request and pin TCP to that answer.

        Checking only when the server is saved would let a hostname re-resolve
        to an internal address later (DNS rebinding).
        """
        from app.services.pinned_http_transport import AsyncPinnedHTTPTransport
        from app.services.url_security import DNSResolutionError, URLSecurityError, resolve_public_target

        try:
            target = await asyncio.to_thread(resolve_public_target, self.endpoint)
        except URLSecurityError as exc:
            raise MCPError("protocol", f"MCP endpoint is not allowed: {exc}") from exc
        except DNSResolutionError as exc:
            raise MCPError("network", f"MCP endpoint could not be resolved: {exc}") from exc
        return AsyncPinnedHTTPTransport(target)

    async def _post(self, payload: dict[str, Any]) -> tuple[dict[str, Any], httpx.Response]:
        headers = dict(self.headers)
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                transport = self.transport or await self._pinned_transport()
                async with httpx.AsyncClient(
                    timeout=self.timeout, follow_redirects=False, transport=transport,
                ) as client:
                    async with client.stream("POST", self.endpoint, headers=headers, json=payload) as response:
                        if 300 <= response.status_code < 400:
                            raise MCPError("protocol", "MCP endpoint redirects are not allowed; save the final HTTPS URL.", response.status_code)
                        if response.status_code in {401, 403}:
                            raise MCPError("auth", f"MCP request failed with HTTP {response.status_code}.", response.status_code)
                        if response.status_code >= 400:
                            raise MCPError("network", f"MCP request failed with HTTP {response.status_code}.", response.status_code)
                        raw = await _read_bounded(response)
                        if response.status_code in {202, 204} and not raw:
                            return {}, response
                        return _parse_response(raw, response.headers.get("content-type", "")), response
            except MCPError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
        raise MCPError("network", f"MCP network request failed: {last_error}") from last_error


async def _read_bounded(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise MCPError("protocol", "MCP response exceeded the 2 MB size limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_response(raw: bytes, content_type: str) -> dict[str, Any]:
    lowered = content_type.lower()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MCPError("protocol", "MCP endpoint returned non-UTF-8 content.") from exc
    if "text/event-stream" in lowered:
        for event in text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            if data and data != "[DONE]":
                try:
                    value = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and ("result" in value or "error" in value):
                    return value
        raise MCPError("protocol", "MCP event stream did not contain a JSON-RPC response.")
    if "application/json" not in lowered:
        raise MCPError("protocol", "MCP endpoint returned an unsupported content type.")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPError("protocol", "MCP endpoint returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise MCPError("protocol", "MCP endpoint returned an invalid JSON-RPC response.")
    return value


def _decode_tool_result(result: dict[str, Any]) -> Any:
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    content = result.get("content")
    if not isinstance(content, list):
        return result
    values: list[Any] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = str(item.get("text") or "")
        try:
            values.append(json.loads(text))
        except json.JSONDecodeError:
            values.append(text)
    if len(values) == 1:
        return values[0]
    return values


def _tool_error_message(result: dict[str, Any]) -> str:
    decoded = _decode_tool_result(result)
    return str(decoded or "MCP tool call failed.")[:1000]
