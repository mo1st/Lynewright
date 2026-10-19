#!/usr/bin/env python3
"""Sign in and run the review-gated demo through the installed HTTP API."""
from __future__ import annotations

import json
import os
import sys
import time
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(base_url: str, path: str, *, token: str = "", payload: dict | None = None, expected: int = 200, extra_headers: dict[str, str] | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(extra_headers or {})
    req = Request(f"{base_url}{path}", data=data, headers=headers)
    try:
        with urlopen(req, timeout=10) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
    if status != expected:
        raise RuntimeError(f"{path}: expected HTTP {expected}, got {status}: {body[:500].decode(errors='replace')}")
    return json.loads(body) if body else {}


def wait_for_run(base_url: str, token: str, run_id: str, wanted: set[str], label: str) -> dict:
    deadline = time.monotonic() + 60
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = request(base_url, f"/api/production-lines/runs/{run_id}", token=token)
        if latest.get("status") in wanted:
            return latest
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {label}: {latest}")


MCP_CHECK_SCRIPT = """import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client(
        "http://127.0.0.1:8001/mcp",
        headers={"Authorization": "Bearer " + ACCESS_TOKEN},
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool("lines.list", {"project_id": "demo-project"})
            if result.isError or not any(
                "demo-write-review-format-notify" in content.text
                for content in result.content
                if hasattr(content, "text")
            ):
                raise RuntimeError(f"MCP lines.list did not return the demo line: {result}")

asyncio.run(main())
"""


def compose_dir() -> str:
    """The installed stack's directory; Compose names the project after it."""
    return os.environ.get("LYNEWRIGHT_DIR") or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


def check_mcp_lines_list(access_token: str) -> None:
    """Run the MCP SDK check in its image, not in the host Python environment."""
    root = compose_dir()
    script = "ACCESS_TOKEN = " + json.dumps(access_token) + "\n" + MCP_CHECK_SCRIPT
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "mcp", "python", "-"],
        cwd=root,
        input=script,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"MCP lines.list check in the mcp container failed: {detail}")


def dump_compose_diagnostics() -> None:
    """Print container state and API/MCP logs when the installer smoke fails."""
    root = compose_dir()
    for arguments in (["ps", "-a"], ["logs", "--no-color", "mcp", "api"]):
        command = ["docker", "compose", *arguments]
        print(f"Installer smoke diagnostics: {' '.join(command)}", file=sys.stderr)
        try:
            result = subprocess.run(
                command, cwd=root, text=True, capture_output=True, timeout=30, check=False,
            )
            if result.stdout:
                print(result.stdout.rstrip(), file=sys.stderr)
            if result.stderr:
                print(result.stderr.rstrip(), file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not mask smoke failure.
            print(f"Could not collect Compose diagnostics: {exc}", file=sys.stderr)


def main() -> int:
    base_url = os.environ.get("LYNEWRIGHT_SMOKE_URL", "http://127.0.0.1:18080").rstrip("/")
    email = os.environ.get("LYNEWRIGHT_SMOKE_EMAIL", "")
    password = os.environ.get("LYNEWRIGHT_SMOKE_PASSWORD", "")
    if not email or not password:
        raise RuntimeError("installer smoke credentials were not provided")

    login = request(base_url, "/api/auth/login", payload={"email": email, "password": password})
    token = login.get("token")
    if not token:
        raise RuntimeError("login response did not include a session token")

    headers_token = str(token)
    assistant = request(
        base_url, "/api/platform/mcp-clients", token=headers_token,
        payload={"name": "Installer MCP smoke", "client_type": "mcp", "permission_mode": "custom",
                 "granted_capabilities": ["lines:read"]}, expected=201,
        extra_headers={"X-Lynewright-Project-ID": "demo-project"},
    )

    check_mcp_lines_list(str(assistant["access_token"]))
    line_id = "demo-write-review-format-notify"
    lines = request(base_url, "/api/production-lines", token=headers_token).get("items", [])
    if not any(line.get("id") == line_id for line in lines):
        raise RuntimeError("the demo line was not seeded")

    started = request(
        base_url,
        f"/api/production-lines/{line_id}/runs",
        token=headers_token,
        payload={"vars": {"topic": "reliable local workflows"}},
        expected=202,
    )
    run_id = str(started["id"])
    waiting = wait_for_run(base_url, headers_token, run_id, {"waiting", "failed", "rejected"}, "review gate")
    if waiting.get("status") != "waiting" or waiting.get("current_stage") != "review":
        raise RuntimeError(f"demo run did not reach its review gate: {waiting}")

    value = {"approved": True, "items": [{"id": "draft", "decision": "approved", "reason": ""}]}
    request(
        base_url,
        f"/api/production-lines/runs/{run_id}/continue",
        token=headers_token,
        payload={"value": value},
        expected=202,
    )
    completed = wait_for_run(base_url, headers_token, run_id, {"done", "failed", "rejected"}, "demo completion")
    if completed.get("status") != "done":
        raise RuntimeError(f"demo run did not finish as done: {completed}")
    print("Installer smoke test: default bootstrap sign-in succeeded; MCP lines.list returned the demo line; the demo line reached review, was approved, and finished done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - print a concise CI failure reason.
        print(f"Installer smoke test failed: {exc}", file=sys.stderr)
        dump_compose_diagnostics()
        raise SystemExit(1) from exc
