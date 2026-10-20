"""Exercise real API and MCP servers against a disposable database.

Uses only the bundled deterministic demo integration. No accounts, credentials,
or database files from an existing deployment are read or changed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "automation-smoke-project"
TENANT = "ten_default"
SCOPES = ["automation:read", "automation:plan", "automation:provision", "integrations:discover", "system:read"]


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(directory: Path):
    api_port = available_port()
    mcp_port = available_port()
    while mcp_port == api_port:
        mcp_port = available_port()
    api_url = f"http://127.0.0.1:{api_port}"
    mcp_url = f"http://127.0.0.1:{mcp_port}"
    password = secrets.token_urlsafe(32)
    os.environ.update({
        "LYNEWRIGHT_ENV": "production", "LYNEWRIGHT_DB_PATH": str(directory / "smoke.sqlite3"),
        "LYNEWRIGHT_MANAGED_MEDIA_DIR": str(directory / "media"),
        "LYNEWRIGHT_PACKAGES_DIR": str(directory / "packages"),
        "LYNEWRIGHT_SECRET_KEY": secrets.token_urlsafe(48), "LYNEWRIGHT_ADMIN_TOKEN": "",
        "LYNEWRIGHT_BOOTSTRAP_EMAIL": "smoke-admin@example.test", "LYNEWRIGHT_BOOTSTRAP_PASSWORD": password,
        "LYNEWRIGHT_PUBLIC_URL": mcp_url, "LYNEWRIGHT_CORS_ORIGINS": api_url,
        "LYNEWRIGHT_PLUGINS": "app.integrations.demo.bootstrap",
        "LYNEWRIGHT_MCP_PLUGINS": "", "LYNEWRIGHT_MCP_OAUTH_ENABLED": "1", "LYNEWRIGHT_DEMO": "1",
        "LYNEWRIGHT_DISABLE_BACKGROUND_SCHEDULERS": "1", "LYNEWRIGHT_RUNTIME_WORKER_ENABLED": "0",
        "LYNEWRIGHT_RUNTIME_WORKER_MODE": "disabled", "LYNEWRIGHT_TELEGRAM_ENABLED": "0",
        "LYNEWRIGHT_OPERATOR_AUTOMATION_ENABLED": "0", "PYTHONDONTWRITEBYTECODE": "1",
        "LYNEWRIGHT_CATALOGUE_URL": "", "LYNEWRIGHT_CATALOGUE_KEYS": "",
    })
    sys.path.insert(0, str(ROOT / "backend"))
    from app.database import create_user, init_db
    from app.services.platform_services import ProjectService
    init_db()
    create_user("smoke-admin@example.test", password, role="admin")
    ProjectService(tenant_id=TENANT).create({"project_id": PROJECT, "name": "Automation smoke"})
    processes = []
    logs = []
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def start(module, port, name):
        log = (directory / f"{name}.log").open("a", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT / "backend", env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT,
        )
        processes.append(process)
        return process

    def wait_ready(url, process):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Test server exited with code {process.returncode}")
            try:
                if httpx.get(url, timeout=1, trust_env=False).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise AssertionError("Test server did not become healthy")

    try:
        api_process = start("app.main:app", api_port, "api")
        wait_ready(f"{api_url}/api/health", api_process)
        mcp_process = start("app.mcp_server:app", mcp_port, "mcp")
        wait_ready(f"{mcp_url}/health", mcp_process)
        with httpx.Client(base_url=api_url, timeout=10, trust_env=False) as http:
            def request(method, path, *, expected=200, **kwargs):
                response = http.request(method, path, **kwargs)
                check(f"{method} {path}: {expected}", response.status_code == expected)
                return response.json() if response.content else None

            request("GET", f"/api/automation/plans?project_id={PROJECT}", expected=401)
            login = request("POST", "/api/auth/login", json={"email": "smoke-admin@example.test", "password": password})
            admin_headers = {"Authorization": f"Bearer {login['token']}", "X-Lynewright-Project-ID": PROJECT}

            def provision(name, audience, scopes, mode="custom"):
                return request("POST", "/api/platform/mcp-clients", expected=201, headers=admin_headers,
                               json={"name": name, "audience": audience, "permission_mode": mode, "granted_capabilities": scopes})

            rest_client = provision("REST smoke", "api", SCOPES)
            mcp_client = provision("MCP smoke", "mcp", SCOPES)
            legacy_client = provision("Legacy full smoke", "mcp", [], mode="full")
            reader = provision("Read-only smoke", "api", ["automation:read"])
            rest_headers = {"Authorization": f"Bearer {rest_client['access_token']}"}
            mcp_headers = {"Authorization": f"Bearer {mcp_client['access_token']}", "Accept": "application/json, text/event-stream"}

            cards = request("GET", f"/api/automation/operations?project_id={PROJECT}&q=writer", headers=rest_headers)["items"]
            writer = next(card for card in cards if card["contract"] == "lynewright.demo.writer" and card["operation"] == "write")
            check("Installed operation includes input schema", "topic" in writer["schema"]["properties"])
            body = {"project_id": PROJECT, "objective": "Create a deterministic note", "line_name": "Smoke preview",
                    "definition": {"stages": [{"id": "write", "kind": "operation", "contract": writer["contract"],
                                                "operation": writer["operation"], "operation_id": writer["operation_id"],
                                                "args_template": {"topic": "isolated verification"}}]}}
            plan = request("POST", "/api/automation/plans", expected=201, headers=rest_headers, json=body)
            reference = {"project_id": PROJECT, "revision": plan["revision"], "plan_hash": plan["plan_hash"]}
            request("POST", f"/api/automation/plans/{plan['id']}/apply", expected=409, headers=rest_headers, json=reference)
            request("POST", f"/api/automation/plans/{plan['id']}/authorize", expected=403, headers=rest_headers, json=reference)
            request("POST", f"/api/automation/plans/{plan['id']}/authorize", headers=admin_headers, json=reference)
            applied = request("POST", f"/api/automation/plans/{plan['id']}/apply", headers=rest_headers, json=reference)
            check("REST preview is persisted and disabled", applied["line_preview"]["persisted"] and not applied["line_preview"]["enabled"])
            replay = request("POST", f"/api/automation/plans/{plan['id']}/apply", headers=rest_headers, json=reference)
            check("REST retry reuses same line", replay["line_preview"]["id"] == applied["line_preview"]["id"])
            request("POST", f"/api/production-lines/{applied['line_preview']['id']}/runs", expected=409,
                    headers=admin_headers, json={"project_id": PROJECT, "vars": {}})
            request("POST", "/api/automation/plans", expected=403,
                    headers={"Authorization": f"Bearer {reader['access_token']}"}, json=body)
            request("GET", f"/api/automation/plans?project_id={PROJECT}", expected=401,
                    headers={"Authorization": f"Bearer {mcp_client['access_token']}"})
            request("GET", "/api/automation/plans?project_id=outside-project", expected=404, headers=rest_headers)

            rpc_id = 0
            def rpc(method, params, headers=mcp_headers, expected=200):
                nonlocal rpc_id
                rpc_id += 1
                response = httpx.post(f"{mcp_url}/mcp", headers=headers, timeout=10, trust_env=False,
                                      json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
                check(f"MCP {method}: {expected}", response.status_code == expected)
                if expected != 200:
                    return None
                result = response.json()
                check(f"MCP {method} has no protocol error", "error" not in result)
                return result["result"]

            def tool(name, arguments):
                result = rpc("tools/call", {"name": name, "arguments": arguments})
                check(f"MCP {name} succeeds", not result.get("isError"))
                if isinstance(result.get("structuredContent"), dict):
                    return result["structuredContent"]
                return json.loads(next(part["text"] for part in result["content"] if part["type"] == "text"))

            initialize = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Isolated smoke", "version": "1"}}
            rpc("initialize", initialize, headers={"Authorization": f"Bearer {rest_client['access_token']}", "Accept": "application/json, text/event-stream"}, expected=401)
            rpc("initialize", initialize)
            tools = rpc("tools/list", {})["tools"]
            check("All four planning tools are exposed", {"operations.search", "automation.plan", "automation.get_plan", "automation.apply"} <= {item["name"] for item in tools})
            legacy_headers = {"Authorization": f"Bearer {legacy_client['access_token']}", "Accept": "application/json, text/event-stream"}
            legacy_tools = rpc("tools/list", {}, headers=legacy_headers)["tools"]
            check("Legacy full client receives no new tools", not {item["name"] for item in legacy_tools} & {"operations.search", "automation.plan", "automation.get_plan", "automation.apply"})
            found = tool("operations.search", {"project_id": PROJECT, "query": "writer"})
            check("MCP discovers actual installed operations", any(item["operation_id"] == writer["operation_id"] for item in found["items"]))
            mcp_plan = tool("automation.plan", body)
            revised = tool("automation.plan", body | {"plan_id": mcp_plan["id"], "expected_revision": 1, "objective": "Updated deterministic note"})
            request("POST", f"/api/automation/plans/{mcp_plan['id']}/authorize", expected=409,
                    headers=admin_headers, json={"project_id": PROJECT, "revision": 1, "plan_hash": mcp_plan["plan_hash"]})
            request("POST", f"/api/automation/plans/{revised['id']}/authorize", headers=admin_headers,
                    json={"project_id": PROJECT, "revision": 2, "plan_hash": revised["plan_hash"]})
            mcp_applied = tool("automation.apply", {"project_id": PROJECT, "plan_id": revised["id"], "revision": 2, "plan_hash": revised["plan_hash"]})
            check("MCP preview is persisted and disabled", mcp_applied["line_preview"]["persisted"] and not mcp_applied["line_preview"]["enabled"])
            read = tool("automation.get_plan", {"project_id": PROJECT, "plan_id": revised["id"]})
            check("MCP read returns same approved hash", read["plan_hash"] == revised["plan_hash"] and read["authorization"]["approved"])

            api_process.terminate()
            api_process.wait(timeout=10)
            api_process = start("app.main:app", api_port, "api")
            wait_ready(f"{api_url}/api/health", api_process)
            restored = request("GET", f"/api/automation/plans/{plan['id']}?project_id={PROJECT}", headers=rest_headers)
            check("Preview and approval survive API restart", restored["line_preview"]["persisted"] and restored["authorization"]["approved"])
            request("DELETE", f"/api/platform/mcp-clients/{rest_client['id']}", expected=204, headers=admin_headers)
            request("GET", f"/api/automation/plans?project_id={PROJECT}", expected=401, headers=rest_headers)
        print(json.dumps({"status": "passed", "checks": len(checks), "transport": ["REST HTTP", "MCP Streamable HTTP"],
                          "installed_integration": "bundled deterministic demo writer", "preview_lines_created": 2,
                          "production_services_modified": False, "external_actions": False}, sort_keys=True))
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="lynewright-automation-smoke-") as temporary:
        temporary_path = Path(temporary).resolve()
        if temporary_path.parent != Path(tempfile.gettempdir()).resolve() or not temporary_path.name.startswith("lynewright-automation-smoke-"):
            raise RuntimeError("Unexpected temporary verification directory")
        run(temporary_path)
