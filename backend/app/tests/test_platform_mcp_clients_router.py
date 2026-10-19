from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from starlette.testclient import TestClient

import app.routers.platform_mcp_clients as mcp_clients_router
from app.security import require_admin_token
from app.services.platform_capabilities import ClientAuthorizationService
from app.services.platform_services import PlatformStore


def test_admin_mcp_client_provisioning_exposes_token_once(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    service = ClientAuthorizationService(PlatformStore(lambda: connection))
    monkeypatch.setattr(mcp_clients_router, "ClientAuthorizationService", lambda: service)
    monkeypatch.setattr(mcp_clients_router, "get_settings", lambda: SimpleNamespace(public_url="https://assist.example.test/"))

    app = FastAPI()
    app.include_router(mcp_clients_router.router)
    app.dependency_overrides[require_admin_token] = lambda: {"role": "admin"}

    with TestClient(app) as client:
        created = client.post("/api/platform/mcp-clients", headers={"X-Lynewright-Project-ID": "demo-project"}, json={
            "name": "Claude Desktop", "client_type": "mcp", "permission_mode": "custom",
            "granted_capabilities": ["system:read", "projects:read"],
        })
        assert created.status_code == 201
        provisioned = created.json()
        assert provisioned["access_token"].startswith("qxc_")
        actor = service.authenticate(provisioned["access_token"])
        assert actor.tenant_id == "ten_default"
        assert actor.project_ids == ("demo-project",)
        assert actor.on_behalf_of

        listed = client.get("/api/platform/mcp-clients")
        assert listed.status_code == 200
        assert listed.json()["clients"][0]["id"] == provisioned["id"]
        assert listed.json()["mcp_url"] == "https://assist.example.test/mcp"
        assert "access_token" not in listed.json()["clients"][0]

        disabled = client.patch(f"/api/platform/mcp-clients/{provisioned['id']}", json={"enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        enabled = client.patch(f"/api/platform/mcp-clients/{provisioned['id']}", json={"enabled": True})
        assert enabled.status_code == 200
        removed = client.delete(f"/api/platform/mcp-clients/{provisioned['id']}")
        assert removed.status_code == 204
        assert client.get("/api/platform/mcp-clients").json()["clients"] == []

    connection.close()
