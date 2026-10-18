from __future__ import annotations

import sqlite3

from fastapi import FastAPI
from starlette.testclient import TestClient

import app.routers.settings as settings_router
from app.security import require_admin_token
from app.services.platform_services import CredentialService, PlatformStore


def test_dashboard_credential_routes_are_write_only(monkeypatch) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    service = CredentialService(PlatformStore(lambda: connection))
    monkeypatch.setattr(settings_router, "CredentialService", lambda: service)

    application = FastAPI()
    application.include_router(settings_router.router)
    application.dependency_overrides[require_admin_token] = lambda: {"role": "admin"}

    with TestClient(application) as client:
        saved = client.put("/api/settings/credentials", json={
            "name": "disposable.token", "kind": "token", "secret": "raw-secret-value",
        })
        assert saved.status_code == 200
        assert saved.json() == {
            "name": "disposable.token", "kind": "token", "configured": True,
            "created_at": saved.json()["created_at"], "updated_at": saved.json()["updated_at"],
        }
        assert "secret" not in saved.json()

        status = client.get("/api/settings/credentials/disposable.token")
        assert status.status_code == 200
        assert status.json()["configured"] is True
        assert "raw-secret-value" not in status.text

        listed = client.get("/api/settings/credentials")
        assert listed.status_code == 200
        assert listed.json()[0]["name"] == "disposable.token"
        assert "raw-secret-value" not in listed.text
        assert "secret" not in listed.json()[0]

        rejected = client.request(
            "DELETE",
            "/api/settings/credentials/disposable.token",
            json={"confirm_name": "different.token"},
        )
        assert rejected.status_code == 400

        removed = client.request(
            "DELETE",
            "/api/settings/credentials/disposable.token",
            json={"confirm_name": "disposable.token"},
        )
        assert removed.status_code == 200
        assert removed.json() == {"name": "disposable.token", "deleted": True}

    connection.close()
