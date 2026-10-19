#!/usr/bin/env python3
"""Record a review-gated demo run driven through Lynewright's MCP tools."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


def request(
    base_url: str,
    path: str,
    *,
    token: str = "",
    tenant_id: str = "",
    project_id: str = "",
    payload: dict | None = None,
    expected: int = 200,
    method: str | None = None,
) -> dict | list:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if tenant_id:
        headers["X-Lynewright-Tenant-ID"] = tenant_id
    if project_id:
        headers["X-Lynewright-Project-ID"] = project_id
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    try:
        with urlopen(Request(f"{base_url}{path}", data=body, headers=headers, method=method), timeout=15) as response:
            status, content = response.status, response.read()
    except HTTPError as exc:
        status, content = exc.code, exc.read()
    if status != expected:
        raise RuntimeError(f"{path}: expected HTTP {expected}, got {status}: {content[:400].decode(errors='replace')}")
    return json.loads(content) if content else {}


def _tool_data(result: object) -> dict:
    if getattr(result, "isError", False):
        message = "; ".join(getattr(item, "text", "") for item in getattr(result, "content", []))
        raise RuntimeError(f"MCP tool call failed: {message}")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []):
        text = getattr(item, "text", "")
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"MCP tool returned no structured result: {result}")


async def _run_mcp_sequence(mcp_url: str, token: str, project_id: str, line_id: str) -> tuple[str, str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(mcp_url, headers=headers) as (read_stream, write_stream, _session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print("assistant demo: MCP session initialized")
            lines = _tool_data(await session.call_tool("lines.list", {"project_id": project_id}))
            if not any(item.get("id") == line_id for item in lines.get("items", []) if isinstance(item, dict)):
                raise RuntimeError(f"MCP lines.list did not return {line_id}.")
            run = _tool_data(await session.call_tool("runs.start", {
                "line_id": line_id, "project_id": project_id,
                "vars": {"topic": "reliable local workflows"},
            }))
            run_id = str(run.get("id") or "")
            if not run_id:
                raise RuntimeError(f"MCP runs.start returned no run id: {run}")
            print(f"assistant demo: runs.start created {run_id}")
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                run = _tool_data(await session.call_tool("runs.get", {"run_id": run_id, "project_id": project_id}))
                if run.get("status") in {"waiting", "failed", "rejected", "done"}:
                    break
                await asyncio.sleep(1)
            if run.get("status") != "waiting" or run.get("current_stage") != "review":
                raise RuntimeError(f"The demo line did not wait at review: {run}")
            print("assistant demo: run is waiting at review")
            waiting = _tool_data(await session.call_tool("reviews.list", {"project_id": project_id}))
            if not any(item.get("run_id") == run_id for item in waiting.get("items", []) if isinstance(item, dict)):
                raise RuntimeError("MCP reviews.list did not return the waiting run.")
            review = _tool_data(await session.call_tool("reviews.get", {
                "run_id": run_id, "stage_id": "review", "project_id": project_id,
            }))
            if not review.get("items"):
                raise RuntimeError("MCP reviews.get returned no review items.")
            decision = _tool_data(await session.call_tool("reviews.decide", {
                "run_id": run_id, "stage_id": "review", "decision": "approve", "project_id": project_id,
            }))
            if decision.get("decision") != "approve":
                raise RuntimeError(f"MCP reviews.decide did not approve the item: {decision}")
            print("assistant demo: review approved through MCP")
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                run = _tool_data(await session.call_tool("runs.get", {"run_id": run_id, "project_id": project_id}))
                if run.get("status") in {"done", "failed", "rejected"}:
                    break
                await asyncio.sleep(1)
            if run.get("status") != "done":
                raise RuntimeError(f"The demo line did not finish as done: {run}")
            return run_id, str(run["status"])


def main() -> int:
    base_url = os.environ.get("LYNEWRIGHT_ASSISTANT_DEMO_URL", "http://127.0.0.1:8080").rstrip("/")
    mcp_url = os.environ.get("LYNEWRIGHT_ASSISTANT_DEMO_MCP_URL", f"{base_url}/mcp")
    email = os.environ.get("LYNEWRIGHT_ASSISTANT_DEMO_EMAIL", "")
    password = os.environ.get("LYNEWRIGHT_ASSISTANT_DEMO_PASSWORD", "")
    project_id = os.environ.get("LYNEWRIGHT_ASSISTANT_DEMO_PROJECT", "")
    line_id = os.environ.get("LYNEWRIGHT_ASSISTANT_DEMO_LINE", "demo-write-review-format-notify")
    if not email or not password:
        raise RuntimeError("Set LYNEWRIGHT_ASSISTANT_DEMO_EMAIL and LYNEWRIGHT_ASSISTANT_DEMO_PASSWORD for a temporary admin account.")
    login = request(base_url, "/api/auth/login", payload={"email": email, "password": password})
    token = str(login.get("token") or "")
    if not token:
        raise RuntimeError("The admin sign-in did not return a session token.")
    tenants = request(base_url, "/api/tenants", token=token)
    if not isinstance(tenants, list) or not tenants:
        raise RuntimeError("No tenant is available for the assistant demo.")
    tenant_id = str(tenants[0]["id"])
    if not project_id:
        projects = request(
            base_url, f"/api/tenants/{quote(tenant_id, safe='')}/projects",
            token=token, tenant_id=tenant_id,
        )
        if not isinstance(projects, list) or not projects:
            raise RuntimeError("Create a project in the dashboard before running the assistant demo.")
        project_id = str(projects[0]["project_id"])

    line_path = f"/api/production-lines/{quote(line_id, safe='')}"
    current = request(
        base_url, f"/api/production-lines?project_id={quote(project_id, safe='')}&include_shared=true",
        token=token, tenant_id=tenant_id, project_id=project_id,
    )
    line = next((item for item in current.get("items", []) if item.get("id") == line_id), None)
    if not line:
        raise RuntimeError(f"The demo line {line_id!r} is not available.")
    original_definition = dict(line["definition"])
    assistant: dict | None = None
    try:
        request(
            base_url, line_path, token=token, tenant_id=tenant_id, project_id=project_id,
            payload={"definition": original_definition | {"review_by": "dashboard_or_assistant"}},
            method="PATCH",
        )
        assistant = request(
            base_url, "/api/platform/mcp-clients", token=token, tenant_id=tenant_id, project_id=project_id,
            payload={"name": f"Assistant demo {int(time.time())}", "client_type": "mcp", "permission_mode": "custom",
                     "granted_capabilities": ["lines:read", "runs:read", "runs:start", "reviews:read", "reviews:decide"]},
            expected=201,
        )
        run_id, status = asyncio.run(_run_mcp_sequence(mcp_url, assistant["access_token"], project_id, line_id))
        print(f"lines.list → {line_id}\nruns.start → {run_id}\nreviews.list/get → waiting at review\nreviews.decide → approved\nruns.get → {status}")
    finally:
        try:
            if assistant is not None:
                request(
                    base_url, f"/api/platform/mcp-clients/{quote(str(assistant['id']), safe='')}",
                    token=token, tenant_id=tenant_id, expected=204, method="DELETE",
                )
        finally:
            # Restore dashboard-only review after the demo has finished.
            request(
                base_url, line_path, token=token, tenant_id=tenant_id, project_id=project_id,
                payload={"definition": original_definition}, method="PATCH",
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - keep a useful script failure in CI and local runs.
        print(f"Assistant demo failed: {exc}", file=sys.stderr)
        traceback.print_exception(exc)
        raise SystemExit(1) from exc
