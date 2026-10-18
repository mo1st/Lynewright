from __future__ import annotations

import sqlite3

from app.database import init_db
from app.main import app


def test_agent_os_route_is_not_registered_and_platform_mcp_route_remains() -> None:
    paths = set(app.openapi()["paths"])

    assert not any(path.startswith("/api/agent") for path in paths)
    assert "/api/platform/mcp-clients" in paths


def test_fresh_database_does_not_initialize_agent_os_tables(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "phase7.sqlite3"
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(database_path))

    init_db()
    with sqlite3.connect(database_path) as db:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_%'")
        }

    assert tables == set()
