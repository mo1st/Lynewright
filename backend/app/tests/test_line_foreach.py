from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app.services.line_engine import ProductionLineEngine, validate_definition
from app.services.platform_services import PlatformStore, PlatformValidationError


PROJECT = "line-item-test"
OPERATIONS = {
    ("demo", "echo"): {
        "schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    },
}


@pytest.fixture
def engine(tmp_path: Path):
    opened: list[sqlite3.Connection] = []
    database_path = tmp_path / "line-items.sqlite3"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(database_path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        opened.append(connection)
        return connection

    result = ProductionLineEngine(store=PlatformStore(connect), tenant_id="ten_items")
    yield result
    for connection in opened:
        connection.close()


def definition(*, count: int, **options: Any) -> dict[str, Any]:
    stage = {
        "id": "batch",
        "kind": "for_each",
        "items": "{{run.vars.values}}",
        "operation": "demo.echo",
        "input": {"value": "{{item.value}}"},
        **options,
    }
    return {"stages": [stage]}


def save(engine: ProductionLineEngine, value: dict[str, Any], *, operation_directory: Any = OPERATIONS) -> None:
    engine.db.put_line(
        "items-line", name="Items line", project_id=PROJECT, definition=value,
        enabled=True, actor_id="test", operation_directory=operation_directory, create_only=True,
    )


def capture_schedules(engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def schedule(run_id: str, stage: dict[str, Any], **kwargs: Any) -> dict[str, str]:
        item_index = int(kwargs["item_index"])
        phase = str(kwargs.get("phase") or "start")
        payload = kwargs.get("payload") or {"value": str(engine._stage_item_rows(run_id, str(stage["id"]))[item_index]["input"].get("value"))}
        calls.append({"run_id": run_id, "stage": stage, "item_index": item_index, "phase": phase, "payload": payload, "retry": bool(kwargs.get("retry"))})
        engine._stage_item_update(
            run_id, str(stage["id"]), item_index, status="running", keep_output=True,
            runner_key=f"runner-{item_index}-{phase}", runner_phase=phase, runner_payload=payload,
        )
        return {"run_key": f"runner-{item_index}-{phase}", "status": "queued"}

    monkeypatch.setattr(engine, "_schedule_stage", schedule)
    return calls


def start(
    engine: ProductionLineEngine, values: list[Any], *, stage: dict[str, Any] | None = None,
    operation_directory: Any = OPERATIONS,
) -> dict[str, Any]:
    save(engine, {"stages": [stage or definition(count=len(values))["stages"][0]]}, operation_directory=operation_directory)
    return engine.start("items-line", variables={"values": values}, project_id=PROJECT, actor={"user_id": "test"})


def update(
    engine: ProductionLineEngine, run_id: str, item_index: int, *,
    status: str = "succeeded", output: Any = None, phase: str = "start", error: str = "",
) -> None:
    stage = engine._stage(run_id, "batch")
    engine._handle_for_each_update(run_id, stage, {
        "status": status,
        "result": output if isinstance(output, dict) else {"value": output} if output is not None else {},
        "error": error,
        "metadata": {"item_index": item_index, "phase": phase},
    })


def test_for_each_dispatches_within_concurrency_and_resumes_from_persisted_items(
    engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_schedules(engine, monkeypatch)
    run = start(engine, [{"value": "a"}, {"value": "b"}, {"value": "c"}], stage=definition(count=3, concurrency=2)["stages"][0])
    assert [call["item_index"] for call in calls] == [0, 1]

    update(engine, run["id"], 0, output={"value": "A"})
    assert [call["item_index"] for call in calls] == [0, 1, 2]

    restarted = ProductionLineEngine(store=engine.store, tenant_id=engine.tenant_id)
    assert restarted.get_run(run["id"])["status"] == "running"
    assert [item["status"] for item in restarted._stage_item_rows(run["id"], "batch")] == ["succeeded", "running", "running"]
    assert restarted._stage_item_rows(run["id"], "batch")[0]["output"] == {"value": "A"}


def test_continue_records_partial_failure_and_retry_runs_only_failed_items(
    engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_schedules(engine, monkeypatch)
    run = start(engine, [{"value": "a"}, {"value": "b"}, {"value": "c"}], stage=definition(count=3, concurrency=2)["stages"][0])
    update(engine, run["id"], 0, output={"value": "A"})
    update(engine, run["id"], 1, status="failed", error="Temporary failure")
    update(engine, run["id"], 2, output={"value": "C"})

    completed = engine.get_run(run["id"])
    assert completed["status"] == "done_with_errors"
    assert completed["stages"][0]["status"] == "done_with_errors"
    assert completed["stages"][0]["output"] == {
        "total": 3, "succeeded": 2, "failed": 1, "skipped": 0,
        "results_ref": f"{run['id']}:batch",
    }
    failed = engine.stage_items(run["id"], "batch", status_filter="failed")
    assert [item["item_index"] for item in failed["items"]] == [1]
    assert failed["items"][0]["error"] == "Temporary failure"

    retried = engine.retry_failed_items(run["id"], "batch", actor_id="test")
    assert retried["status"] == "running"
    assert [call["item_index"] for call in calls] == [0, 1, 2, 1]
    assert calls[-1]["retry"] is True
    update(engine, run["id"], 1, output={"value": "B"})
    assert engine.get_run(run["id"])["status"] == "done"
    assert engine._stage_item_rows(run["id"], "batch")[1]["attempts"] == 2


def test_stop_skips_queued_items_after_a_failure(engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_schedules(engine, monkeypatch)
    run = start(engine, [{"value": str(index)} for index in range(4)], stage=definition(count=4, concurrency=2, on_item_error="stop")["stages"][0])
    assert [call["item_index"] for call in calls] == [0, 1]
    update(engine, run["id"], 0, status="failed", error="Stop here")
    assert [item["status"] for item in engine._stage_item_rows(run["id"], "batch")] == ["failed", "running", "skipped", "skipped"]
    update(engine, run["id"], 1, output={"value": "ok"})
    assert engine.get_run(run["id"])["status"] == "failed"
    assert engine.get_run(run["id"])["stages"][0]["status"] == "failed"


def test_continue_fails_the_stage_when_every_item_fails(engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_schedules(engine, monkeypatch)
    run = start(engine, [{"value": "a"}])
    update(engine, run["id"], 0, status="failed", error="Unavailable")
    completed = engine.get_run(run["id"])
    assert completed["status"] == "failed"
    assert completed["stages"][0]["status"] == "failed"
    assert completed["stages"][0]["output"]["failed"] == 1
    retried = engine.retry_failed_items(run["id"], "batch", actor_id="test")
    assert retried["status"] == "running"
    assert [call["item_index"] for call in calls] == [0, 0]
    update(engine, run["id"], 0, output={"value": "recovered"})
    assert engine.get_run(run["id"])["status"] == "done"


def test_partial_continue_advances_to_review_before_retrying_failed_items(
    engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_schedules(engine, monkeypatch)
    stages = [
        definition(count=2, concurrency=2)["stages"][0],
        {"id": "review", "kind": "gate:review", "items_template": "{{stages.batch.items}}"},
    ]
    save(engine, {"stages": stages})
    run = engine.start(
        "items-line", variables={"values": [{"value": "a"}, {"value": "b"}]},
        project_id=PROJECT, actor={"user_id": "test"},
    )
    update(engine, run["id"], 0, output={"value": "A"})
    update(engine, run["id"], 1, status="failed", error="Retry this item")
    waiting = engine.get_run(run["id"])
    assert waiting["status"] == "waiting"
    assert waiting["current_stage"] == "review"
    assert [stage["stage_id"] for stage in waiting["stages"]] == ["batch", "review"]
    assert waiting["stages"][0]["status"] == "done_with_errors"
    assert [item["value"] for item in waiting["stages"][1]["output"]["items"]] == ["A"]

    completed = engine.continue_run(run["id"], value={"items": [{"id": "0", "decision": "approved"}]})
    assert completed["status"] == "done_with_errors"
    assert completed["stages"][-1]["status"] == "done"

    engine.retry_failed_items(run["id"], "batch", actor_id="test")
    assert calls[-1]["item_index"] == 1 and calls[-1]["retry"] is True
    update(engine, run["id"], 1, output={"value": "B"})
    repaired = engine.get_run(run["id"])
    assert repaired["status"] == "done"
    assert repaired["stages"][0]["status"] == "done"
    assert [item["value"] for item in repaired["stages"][1]["output"]["approved"]] == ["A"]
    assert repaired["stages"][1]["output"]["rejected"] == []


def test_max_items_is_refused_before_any_item_is_scheduled(engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_schedules(engine, monkeypatch)
    run = start(engine, [{"value": str(index)} for index in range(3)], stage=definition(count=3, max_items=2)["stages"][0])
    assert run["status"] == "failed"
    assert calls == []
    assert engine._stage_item_rows(run["id"], "batch") == []


def test_async_operation_is_polled_for_each_item(engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_schedules(engine, monkeypatch)
    ops = {
        ("demo", "start"): {
            "schema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            "async": {"status_contract": "demo", "status_operation": "status", "status_argument": "job_id"},
        },
        ("demo", "status"): {
            "schema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
        },
    }
    stage = {
        "id": "batch", "kind": "for_each", "items": "{{run.vars.values}}",
        "operation": "demo.start", "input": {"value": "{{item.value}}"},
        "status_operation": {"contract": "demo", "operation": "status"},
        "status_args_template": {"job_id": "{{stages.batch.output.job_id}}"},
    }
    run = start(engine, [{"value": "a"}], stage=stage, operation_directory=ops)
    assert calls[0]["stage"]["kind"] == "async_operation"
    update(engine, run["id"], 0, output={"job_id": "job-0"})
    assert [(call["item_index"], call["phase"], call["payload"]) for call in calls] == [
        (0, "start", {"value": "a"}), (0, "poll", {"job_id": "job-0"}),
    ]
    update(engine, run["id"], 0, output={"status": "pending"}, phase="poll")
    assert calls[-1]["phase"] == "poll-1"
    update(engine, run["id"], 0, output={"status": "done", "value": "finished"}, phase="poll-1")
    assert engine.get_run(run["id"])["status"] == "done"
    assert engine._stage_item_rows(run["id"], "batch")[0]["output"] == {"status": "done", "value": "finished"}


def test_item_output_enforces_the_individual_limit(engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    capture_schedules(engine, monkeypatch)
    run = start(engine, [{"value": "a"}])
    update(engine, run["id"], 0, output={"value": "x" * (64 * 1024 + 1)})
    completed = engine.get_run(run["id"])
    assert completed["status"] == "failed"
    assert completed["stages"][0]["status"] == "failed"
    assert "64 KiB" in engine._stage_item_rows(run["id"], "batch")[0]["error"]


def test_per_item_review_splits_approved_and_rejected_outputs(engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_schedules(engine, monkeypatch)
    stages = [
        definition(count=2)["stages"][0],
        {"id": "review", "kind": "gate:review", "items_template": "{{stages.batch.items}}"},
    ]
    save(engine, {"stages": stages})
    run = engine.start(
        "items-line", variables={"values": [{"value": "a"}, {"value": "b"}]},
        project_id=PROJECT, actor={"user_id": "test"},
    )
    update(engine, run["id"], 0, output={"value": "A"})
    assert [call["item_index"] for call in calls] == [0, 1]
    update(engine, run["id"], 1, output={"value": "B"})
    waiting = engine.get_run(run["id"])
    assert waiting["status"] == "waiting"
    review_items = waiting["stages"][1]["output"]["items"]
    assert [item["id"] for item in review_items] == ["0", "1"]

    continued = engine.continue_run(run["id"], value={"items": [
        {"id": "0", "decision": "approved"},
        {"id": "1", "decision": "refused", "reason": "Needs another pass"},
    ]})
    assert continued["status"] == "done"
    review_output = continued["context"]["stages"]["review"]["output"]
    assert [item["value"] for item in review_output["approved"]] == ["A"]
    assert [item["value"] for item in review_output["rejected"]] == ["B"]


def test_template_context_resolves_item_outputs_and_failures(engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    capture_schedules(engine, monkeypatch)
    run = start(engine, [{"value": "a"}, {"value": "b"}], stage=definition(count=2, concurrency=2)["stages"][0])
    update(engine, run["id"], 0, output={"value": "A"})
    update(engine, run["id"], 1, status="failed", error="Unavailable")
    context = engine._template_context(run["id"], engine.get_run(run["id"])["context"], {
        "ok": "{{stages.batch.items}}", "bad": "{{stages.batch.failed}}",
    })
    from app.services.line_engine import _resolve
    resolved = _resolve({"ok": "{{stages.batch.items}}", "bad": "{{stages.batch.failed}}"}, context)
    assert resolved["ok"] == [{"value": "A"}]
    assert resolved["bad"] == [{"index": 1, "input": {"value": "b"}, "error": "Unavailable"}]


@pytest.mark.parametrize("definition_value", [
    {"stages": [{"id": "batch", "kind": "for_each", "items": "{{run.vars.values}}", "operation": "demo.echo", "input": {"value": "{{item.value}}"}, "unknown": True}]},
    {"metadata": {}, "stages": [{"id": "batch", "kind": "for_each", "items": "{{run.vars.values}}", "operation": "demo.echo", "input": {"value": "{{item.value}}"}}]},
    {"stages": [{"id": "batch", "kind": "for_each", "items": "{{item.children}}", "operation": "demo.echo", "input": {"value": "{{item.value}}"}}]},
    {"stages": [{"id": "batch", "kind": "for_each", "items": "{{run.vars.values}}", "operation": "demo.echo", "input": {"value": "{{item.value}}"}, "title": "{{item.value}}"}]},
])
def test_for_each_rejects_unknown_fields_and_out_of_scope_item_templates(definition_value: dict[str, Any]) -> None:
    with pytest.raises(PlatformValidationError):
        validate_definition(definition_value, operation_directory=OPERATIONS)


def test_cancel_and_archive_clear_per_item_rows(engine: ProductionLineEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = capture_schedules(engine, monkeypatch)
    run = start(engine, [{"value": "a"}, {"value": "b"}])
    import app.services.line_runner as line_runner
    monkeypatch.setattr(line_runner, "cancel_run", lambda *_args, **_kwargs: True)
    cancelled = engine.cancel(run["id"])
    assert cancelled["status"] == "cancelled"
    assert [item["status"] for item in engine._stage_item_rows(run["id"], "batch")] == ["cancelled", "cancelled"]

    calls.clear()
    next_run = engine.start("items-line", variables={"values": [{"value": "c"}]}, project_id=PROJECT, actor={"user_id": "test"})
    update(engine, next_run["id"], 0, output={"value": "C"})
    assert engine.get_run(next_run["id"])["status"] == "done"
    engine.archive_run(next_run["id"], actor_id="test")
    assert engine._stage_item_rows(next_run["id"], "batch") == []
