from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.services.line_engine import ProductionLineEngine, validate_definition
from app.services.platform_services import PlatformStore, PlatformValidationError


PROJECT = "map-test"
OPERATIONS: dict[tuple[str, str], dict[str, Any]] = {}


@pytest.fixture
def engine(tmp_path: Path):
    connections: list[sqlite3.Connection] = []
    database_path = tmp_path / "line-map.sqlite3"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(database_path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connections.append(connection)
        return connection

    result = ProductionLineEngine(store=PlatformStore(connect), tenant_id="ten_map")
    yield result
    for connection in connections:
        connection.close()


def map_stage(**options: Any) -> dict[str, Any]:
    return {
        "id": "shape",
        "kind": "map",
        "items": "{{run.vars.values}}",
        "fields": {"value": "{{item.value}}"},
        **options,
    }


def run_map(engine: ProductionLineEngine, items: list[Any], stage: dict[str, Any]) -> dict[str, Any]:
    line_id = f"map-line-{uuid.uuid4().hex}"
    engine.db.put_line(
        line_id, name="Map line", project_id=PROJECT, definition={"stages": [stage]},
        enabled=True, actor_id="test", operation_directory=OPERATIONS, create_only=True,
    )
    return engine.start(line_id, variables={"values": items}, project_id=PROJECT, actor={"user_id": "test"})


def test_map_renames_picks_constants_filters_sorts_and_limits(engine: ProductionLineEngine) -> None:
    stage = map_stage(
        fields={
            "source_url": "{{item.url}}", "title": "{{item.name}}", "score": "{{item.score}}",
            "origin": "fixture",
        },
        where={"path": "score", "op": ">=", "value": 70},
        sort={"path": "score", "order": "desc"},
        limit=2,
    )
    run = run_map(engine, [
        {"url": "u1", "name": "first", "score": 75},
        {"url": "u2", "name": "second", "score": 95},
        {"url": "u3", "score": 80},
        {"url": "u4", "name": "last", "score": 65},
    ], stage)
    assert run["status"] == "done"
    assert run["stages"][0]["output"] == {"items": [
        {"source_url": "u2", "title": "second", "score": 95, "origin": "fixture"},
        {"source_url": "u3", "title": None, "score": 80, "origin": "fixture"},
    ]}
    assert run["context"]["stages"]["shape"]["output"] == run["stages"][0]["output"]


@pytest.mark.parametrize(("op", "expected", "indexes"), [
    ("=", 2, [1]), ("!=", 2, [0, 2]), (">", 2, [2]), (">=", 2, [1, 2]),
    ("<", 2, [0]), ("<=", 2, [0, 1]), ("exists", None, [0, 1, 2]),
])
def test_map_filter_operators(op: str, expected: Any, indexes: list[int], engine: ProductionLineEngine) -> None:
    condition = {"path": "index", "op": op}
    if op != "exists":
        condition["value"] = expected
    run = run_map(engine, [{"index": 1}, {"index": 2}, {"index": 3}], map_stage(
        fields={"index": "{{item.index}}"}, where=condition,
    ))
    assert run["status"] == "done"
    assert [item["index"] for item in run["stages"][0]["output"]["items"]] == [i + 1 for i in indexes]


def test_map_missing_paths_resolve_to_null_and_exists_rejects_them(engine: ProductionLineEngine) -> None:
    run = run_map(engine, [{"other": 1}], map_stage(fields={"picked": "{{item.value}}", "constant": 5}))
    assert run["stages"][0]["output"] == {"items": [{"picked": None, "constant": 5}]}
    filtered = run_map(engine, [{"other": 1}], map_stage(
        fields={"constant": 5}, where={"path": "picked", "op": "exists"},
    ))
    assert filtered["stages"][0]["output"] == {"items": []}


def test_map_is_data_only_and_refuses_oversized_output(engine: ProductionLineEngine) -> None:
    marker = "__import__('os').system('should remain plain text')"
    run = run_map(engine, [{"value": "x"}], map_stage(fields={"text": marker}))
    assert run["status"] == "done"
    assert run["stages"][0]["output"] == {"items": [{"text": marker}]}

    second = ProductionLineEngine(store=engine.store, tenant_id=engine.tenant_id)
    oversized = run_map(second, [{"value": 1}, {"value": 2}], map_stage(fields={"value": "x" * 40000}))
    assert oversized["status"] == "failed"
    assert "64 KiB" in oversized["stages"][0]["error"]


@pytest.mark.parametrize("stage", [
    {"id": "shape", "kind": "map", "items": "{{run.vars.values}}", "fields": {"value": "prefix {{item.value}}"}},
    {"id": "shape", "kind": "map", "items": "{{run.vars.values}}", "fields": {"value": "{{stages.later.output.value}}"}},
    {"id": "shape", "kind": "map", "items": "{{run.vars.values}}", "fields": {"value": "{{item.value}}"}, "where": {"path": "value", "op": "call", "value": 1}},
    {"id": "shape", "kind": "map", "items": "{{run.vars.values}}", "fields": {"value": "{{item.value}}"}, "limit": 0},
])
def test_map_rejects_expressions_invalid_operators_and_bad_bounds(stage: dict[str, Any]) -> None:
    with pytest.raises(PlatformValidationError):
        validate_definition({"stages": [stage]}, operation_directory=OPERATIONS)


def test_item_references_are_scoped_to_map_and_for_each() -> None:
    with pytest.raises(PlatformValidationError, match="outside the item scope"):
        validate_definition({"stages": [{
            "id": "shape", "kind": "notify", "message_template": "{{item.value}}",
        }]}, operation_directory=OPERATIONS)


@pytest.mark.parametrize("stage", [
    {"id": "shape", "kind": "map", "items": "{{run.vars.values}}", "fields": {"value": "{{item.value}}"}, "script": "return item"},
    {"id": "shape", "kind": "map", "items": "{{item.children}}", "fields": {"value": "{{item.value}}"}},
    {"id": "shape", "kind": "map", "items": "{{run.vars.values}}", "fields": {"value": "{{item.value}}"}, "title": "{{item.value}}"},
])
def test_map_rejects_unknown_fields_and_out_of_scope_item_templates(stage: dict[str, Any]) -> None:
    with pytest.raises(PlatformValidationError):
        validate_definition({"stages": [stage]}, operation_directory=OPERATIONS)
