"""Auto-loaded pytest fixtures for isolated Lynewright integration tests."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
import secrets
from typing import Any

import pytest



@dataclass
class LynewrightApp:
    """An authenticated FastAPI test client backed by isolated SQLite data."""

    client: Any
    data_dir: Path
    admin_email: str
    admin_password: str


@pytest.fixture
def lynewright(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Provide a temporary SQLite-backed Lynewright integration and line runtime."""
    from .harness import LynewrightHarness

    harness = LynewrightHarness(tmp_path, monkeypatch=monkeypatch)
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture
def lynewright_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LynewrightApp]:
    """Start the API on a private schema and return a bootstrapped admin client."""
    data_dir = tmp_path / "lynewright-app"
    admin_email = f"admin-{secrets.token_hex(5)}@example.test"
    admin_password = secrets.token_urlsafe(24)
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(data_dir / "lynewright.sqlite3"))
    monkeypatch.setenv("LYNEWRIGHT_MANAGED_MEDIA_DIR", str(data_dir / "media"))
    monkeypatch.setenv("LYNEWRIGHT_BOOTSTRAP_EMAIL", admin_email)
    monkeypatch.setenv("LYNEWRIGHT_BOOTSTRAP_PASSWORD", admin_password)
    monkeypatch.setenv("LYNEWRIGHT_ENV", "development")
    monkeypatch.setenv("LYNEWRIGHT_ADMIN_TOKEN", "")
    monkeypatch.setenv("LYNEWRIGHT_RUNTIME_WORKER_ENABLED", "0")
    monkeypatch.setenv("LYNEWRIGHT_RUNTIME_WORKER_MODE", "disabled")
    monkeypatch.setenv("LYNEWRIGHT_DISABLE_BACKGROUND_SCHEDULERS", "1")
    monkeypatch.setenv("LYNEWRIGHT_OPERATOR_AUTOMATION_ENABLED", "0")
    monkeypatch.setenv("LYNEWRIGHT_OPERATOR_DISPATCH_ENABLED", "0")
    monkeypatch.setenv("LYNEWRIGHT_TELEGRAM_ENABLED", "0")
    monkeypatch.setenv("LYNEWRIGHT_PLUGINS", "")
    monkeypatch.setenv("LYNEWRIGHT_INTEGRATION_PACKAGES", "")
    monkeypatch.setenv("LYNEWRIGHT_DEMO", "0")

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"email": admin_email, "password": admin_password},
        )
        if response.status_code != 200:
            raise AssertionError(f"Bootstrap admin login failed: {response.status_code} {response.text}")
        token = response.json()["token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        yield LynewrightApp(
            client=client,
            data_dir=data_dir,
            admin_email=admin_email,
            admin_password=admin_password,
        )


@pytest.fixture
def install_integration_file(lynewright):
    """Return the K4.1 import helper bound to this isolated harness."""
    return lynewright.import_file


@pytest.fixture
def install_native_package(lynewright):
    """Return the allowlisted K4.2 native-package install helper."""
    return lynewright.install_native_package


@pytest.fixture
def run_line(lynewright):
    """Return a helper that saves and runs arbitrary stages with line variables."""
    def run(stages, vars=None, *, timeout: float = 20.0):
        return lynewright.run_stages(stages, variables=vars, timeout=timeout)

    return run


@pytest.fixture
def approve_review(lynewright):
    return lynewright.approve_review


@pytest.fixture
def poll_until(lynewright):
    return lynewright.poll_until
