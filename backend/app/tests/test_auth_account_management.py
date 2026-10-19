from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.database as database
import app.routers.auth as auth_router
from app.routers.auth import router


class _AllowLimiter:
    def consume(self, **_kwargs) -> bool:
        return True


class _DenyLimiter:
    def consume(self, **_kwargs) -> bool:
        return False


@pytest.fixture
def auth_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("LYNEWRIGHT_REDIS_URL", "")
    monkeypatch.delenv("LYNEWRIGHT_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(auth_router, "get_rate_limiter", lambda: _AllowLimiter())
    database.init_db()
    admin = database.create_user("admin@example.test", "admin-password-current", "admin")
    member = database.create_user("member@example.test", "member-password-current", "viewer")
    admin_token = database.create_session(admin["id"])
    member_token = database.create_session(member["id"])
    application = FastAPI()
    application.include_router(router)
    with TestClient(application) as client:
        yield {
            "client": client,
            "admin": admin,
            "member": member,
            "admin_token": admin_token,
            "member_token": member_token,
        }


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_password_change_checks_current_and_strength_keeps_current_session_and_logs_no_secrets(auth_setup) -> None:
    client = auth_setup["client"]
    token = auth_setup["admin_token"]
    other_token = database.create_session(auth_setup["admin"]["id"])
    wrong = client.post("/api/auth/me/password", headers=_headers(token), json={
        "current_password": "wrong-current-password", "new_password": "new-password-strong",
    })
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "Current password is incorrect."

    weak = client.post("/api/auth/me/password", headers=_headers(token), json={
        "current_password": "admin-password-current", "new_password": "short",
    })
    assert weak.status_code == 422

    changed = client.post("/api/auth/me/password", headers=_headers(token), json={
        "current_password": "admin-password-current", "new_password": "new-password-strong",
    })
    assert changed.status_code == 200
    assert changed.json() == {"changed": True}
    assert client.get("/api/auth/me", headers=_headers(token)).status_code == 200
    assert client.get("/api/auth/me", headers=_headers(other_token)).status_code == 401
    assert client.post("/api/auth/login", json={
        "email": "admin@example.test", "password": "new-password-strong",
    }).status_code == 200

    with sqlite3.connect(database.get_db_path()) as db:
        entries = db.execute("SELECT message,details_json FROM application_logs WHERE source='auth.password'").fetchall()
    encoded_logs = json.dumps(entries)
    for secret in ("admin-password-current", "new-password-strong", "wrong-current-password"):
        assert secret not in encoded_logs
    assert any("password changed" in str(row[0]).lower() for row in entries)


def test_password_change_is_rate_limited_like_login(auth_setup, monkeypatch) -> None:
    monkeypatch.setattr(auth_router, "get_rate_limiter", lambda: _DenyLimiter())
    response = auth_setup["client"].post(
        "/api/auth/me/password", headers=_headers(auth_setup["admin_token"]),
        json={"current_password": "admin-password-current", "new_password": "new-password-strong"},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "300"
    assert response.json()["detail"] == "Too many password changes. Try again in a few minutes."


def test_admin_email_change_normalizes_rejects_duplicates_and_revokes_all_sessions(auth_setup) -> None:
    client = auth_setup["client"]
    member_id = auth_setup["member"]["id"]
    member_token = auth_setup["member_token"]
    other_member_token = database.create_session(member_id)
    changed = client.patch(
        f"/api/auth/users/{member_id}", headers=_headers(auth_setup["admin_token"]),
        json={"email": "  New.Member@Example.Test  "},
    )
    assert changed.status_code == 200
    assert changed.json()["email"] == "new.member@example.test"
    assert client.get("/api/auth/me", headers=_headers(member_token)).status_code == 401
    assert client.get("/api/auth/me", headers=_headers(other_member_token)).status_code == 401

    duplicate = client.patch(
        f"/api/auth/users/{member_id}", headers=_headers(auth_setup["admin_token"]),
        json={"email": "ADMIN@example.test"},
    )
    assert duplicate.status_code == 409
    assert database.get_user_by_id(member_id)["email"] == "new.member@example.test"


def test_self_email_requires_password_and_keeps_current_session(auth_setup) -> None:
    client = auth_setup["client"]
    user_id = auth_setup["member"]["id"]
    token = auth_setup["member_token"]
    other_token = database.create_session(user_id)
    wrong = client.post("/api/auth/me/email", headers=_headers(token), json={
        "new_email": "updated@example.test", "current_password": "wrong-current-password",
    })
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "Current password is incorrect."

    changed = client.post("/api/auth/me/email", headers=_headers(token), json={
        "new_email": " Updated@Example.Test ", "current_password": "member-password-current",
    })
    assert changed.status_code == 200
    assert changed.json() == {"email": "updated@example.test"}
    assert client.get("/api/auth/me", headers=_headers(token)).json()["email"] == "updated@example.test"
    assert client.get("/api/auth/me", headers=_headers(other_token)).status_code == 401
    duplicate = client.post("/api/auth/me/email", headers=_headers(token), json={
        "new_email": "admin@example.test", "current_password": "member-password-current",
    })
    assert duplicate.status_code == 409


def test_user_management_remains_admin_only_and_last_admin_guard_holds(auth_setup) -> None:
    client = auth_setup["client"]
    member_headers = _headers(auth_setup["member_token"])
    assert client.get("/api/auth/users", headers=member_headers).status_code == 403
    assert client.post("/api/auth/users", headers=member_headers, json={
        "email": "blocked@example.test", "password": "secure-password", "role": "viewer",
    }).status_code == 403
    assert client.patch(
        f"/api/auth/users/{auth_setup['member']['id']}", headers=member_headers,
        json={"role": "editor"},
    ).status_code == 403

    admin_id = auth_setup["admin"]["id"]
    demote = client.patch(f"/api/auth/users/{admin_id}", headers=_headers(auth_setup["admin_token"]), json={"role": "viewer"})
    assert demote.status_code == 409
    deactivate = client.patch(f"/api/auth/users/{admin_id}", headers=_headers(auth_setup["admin_token"]), json={"is_active": False})
    assert deactivate.status_code == 409
    assert database.get_user_by_id(admin_id)["role"] == "admin"
    assert database.get_user_by_id(admin_id)["is_active"] == 1


def test_user_delete_guards_sessions_and_history_identity(auth_setup) -> None:
    client = auth_setup["client"]
    admin_token = auth_setup["admin_token"]
    member_id = auth_setup["member"]["id"]
    member_token = auth_setup["member_token"]

    self_delete = client.delete(f"/api/auth/users/{auth_setup['admin']['id']}", headers=_headers(admin_token))
    assert self_delete.status_code == 409
    last_admin = client.delete(f"/api/auth/users/{auth_setup['admin']['id']}", headers=_headers(admin_token))
    assert last_admin.status_code == 409

    second_admin = database.create_user("second-admin@example.test", "second-admin-password", "admin")
    deleted = client.delete(f"/api/auth/users/{second_admin['id']}", headers=_headers(admin_token))
    assert deleted.status_code == 200
    assert deleted.json()["email"] == "second-admin@example.test"
    assert database.get_user_by_id(second_admin["id"]) is None

    deleted_member = client.delete(f"/api/auth/users/{member_id}", headers=_headers(admin_token))
    assert deleted_member.status_code == 200
    assert deleted_member.json()["email"] == "member@example.test"
    assert client.get("/api/auth/me", headers=_headers(member_token)).status_code == 401
    assert client.delete(f"/api/auth/users/{member_id}", headers=_headers(admin_token)).status_code == 404

    with sqlite3.connect(database.get_db_path()) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (member_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tenant_memberships WHERE user_id=?", (member_id,)).fetchone()[0] == 0


def test_user_delete_is_admin_only(auth_setup) -> None:
    response = auth_setup["client"].delete(
        f"/api/auth/users/{auth_setup['member']['id']}",
        headers=_headers(auth_setup["member_token"]),
    )
    assert response.status_code == 403


def test_logout_all_can_keep_current_session_and_default_still_revokes_it(auth_setup) -> None:
    client = auth_setup["client"]
    token = auth_setup["admin_token"]
    other = database.create_session(auth_setup["admin"]["id"])
    kept = client.post("/api/auth/logout-all?keep_current=true", headers=_headers(token))
    assert kept.status_code == 204
    assert client.get("/api/auth/me", headers=_headers(token)).status_code == 200
    assert client.get("/api/auth/me", headers=_headers(other)).status_code == 401

    revoked = client.post("/api/auth/logout-all", headers=_headers(token))
    assert revoked.status_code == 204
    assert client.get("/api/auth/me", headers=_headers(token)).status_code == 401


def test_disabled_account_login_message_matches_account_page_copy(auth_setup) -> None:
    database.set_user_active(auth_setup["member"]["id"], False)
    response = auth_setup["client"].post("/api/auth/login", json={
        "email": "member@example.test", "password": "member-password-current",
    })
    assert response.status_code == 401
    assert response.json()["detail"] == "This account is disabled. Ask an admin."
