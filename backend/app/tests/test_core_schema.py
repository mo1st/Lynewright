from __future__ import annotations

from app.database import get_connection, init_db


def _name(*parts: str) -> str:
    return "".join(parts)


def test_empty_database_has_core_schema_without_business_tables(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "core.sqlite3"
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(database_path))

    init_db()
    with get_connection() as db:
        tables = {
            str(row[0]) for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        journal_mode = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    assert journal_mode == "wal"
    assert {
        "tenants", "users", "sessions", "application_logs", "production_lines",
        "line_runs", "platform_integration_connections", "runtime_jobs", "runtime_workers",
    } <= tables
    assert any(name.startswith("operator_") for name in tables)

    business_tables = {
        _name("sites"), _name("landing", "_pages"), _name("lp", "_builds"),
        _name("wid", "get", "_configs"), _name("cam", "paigns"), _name("qu", "izzes"),
        _name("pro", "ducts"), _name("site", "_products"),
        _name("what", "sapp", "_messages"),
    }
    business_prefixes = (_name("runtime", "_verifier", "_"), _name("dis", "covery", "_"))
    present = sorted(
        name for name in tables
        if name in business_tables or any(name.startswith(prefix) for prefix in business_prefixes)
    )
    assert not present, f"Business tables in core schema: {present}"
