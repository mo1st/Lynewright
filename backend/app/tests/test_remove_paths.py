from __future__ import annotations

import re
from pathlib import Path

from app.main import app


ROOT = Path(__file__).resolve().parents[3]
AUDIT = ROOT / "docs" / "remove-paths.md"


def _route_pattern(path: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]+\})", path)
    return re.compile("^" + "".join(r"[^/]+" if part.startswith("{") else re.escape(part) for part in parts) + "$")


def test_every_audited_add_path_has_an_api_and_ui_removal() -> None:
    source = AUDIT.read_text(encoding="utf-8")
    rows = [line for line in source.splitlines() if line.startswith("|") and "|---" not in line]
    assert len(rows) > 15, "Removal audit should cover the real addable-item inventory."
    route_pairs = {
        (method.upper(), str(path))
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    audit_rows = rows[1:]
    for line in audit_rows:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        item, add_path, remove_path, api, ui, _notes = cells[:6]
        assert api == "✓", f"{item} has no API removal path: {remove_path}"
        assert ui == "✓", f"{item} has no UI removal path: {remove_path}"
        assert "✗" not in remove_path

        declared = re.findall(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/api/[^` ]+)", remove_path)
        assert declared, f"{item} must name a concrete API remove action. Add path: {add_path}"
        assert any(
            declared_method == registered_method and _route_pattern(registered_path).fullmatch(path)
            for declared_method, path in declared
            for registered_method, registered_path in route_pairs
        ), f"{item} removal route is not registered: {declared}"
        add_routes = re.findall(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/api/[^` ]+)", add_path)
        assert all(
            any(
                declared_method == registered_method and _route_pattern(registered_path).fullmatch(path)
                for registered_method, registered_path in route_pairs
            )
            for declared_method, path in add_routes
        ), f"{item} add route is not registered: {add_routes}"


def test_removal_audit_contains_no_unresolved_rows() -> None:
    source = AUDIT.read_text(encoding="utf-8")
    assert "| ✗ |" not in source
    assert "| API | UI |" in source
