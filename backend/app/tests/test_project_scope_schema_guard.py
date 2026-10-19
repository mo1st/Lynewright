from __future__ import annotations

from contextlib import closing
from pathlib import Path


SCOPED_CORE_TABLES = {
    "production_lines": {"tenant_id", "project_id"},
    "line_runs": {"tenant_id", "project_id"},
    "operator_runs": {"tenant_id", "project_id"},
    "operator_run_steps": {"tenant_id", "project_id"},
    "platform_integration_connections": {"tenant_id", "project_id"},
}


def test_core_project_tables_have_explicit_tenant_and_project_columns(
    tmp_path: Path, monkeypatch,
) -> None:
    from app import database

    path = tmp_path / "scoped-core.sqlite3"
    monkeypatch.setattr(database, "get_db_path", lambda: path)
    database.init_db()

    with closing(database.get_connection(path)) as db:
        names = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(SCOPED_CORE_TABLES) <= names
        for table, required in SCOPED_CORE_TABLES.items():
            columns = {
                str(row["name"]): row["dflt_value"]
                for row in db.execute(f'PRAGMA table_info("{table}")')
            }
            assert required <= columns.keys(), f"{table} lost tenant/project scope columns"
            assert all(value != "'demo-project'" for value in columns.values()), (
                f"{table} assigns rows to a demo project by default"
            )
