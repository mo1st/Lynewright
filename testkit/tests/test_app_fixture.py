from __future__ import annotations


def test_lynewright_app_bootstraps_an_authenticated_admin(lynewright_app) -> None:
    assert lynewright_app.data_dir.joinpath("lynewright.sqlite3").is_file()
    response = lynewright_app.client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["email"] == lynewright_app.admin_email
    assert response.json()["role"] == "admin"
