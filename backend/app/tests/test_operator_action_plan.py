from __future__ import annotations

import sqlite3

import pytest

from app.services.operator_runtime.action_plan import materialize_step, validate_action_plan
from app.services.operator_runtime.loop import (
    _looks_compound_objective,
    _select_prompt_tools,
)
from app.services.operator_runtime.schema import _migrate_action_plan_step_type


def _plan() -> dict:
    return {
        "type": "action_plan",
        "message": "complete the workflow",
        "steps": [
            {
                "step_id": "discover",
                "tool": "integration.search_operations",
                "arguments": {"query": "camera"},
                "bindings": [],
            },
            {
                "step_id": "prepare",
                "tool": "media.prepare",
                "arguments": {"product_id": ""},
                "bindings": [
                    {
                        "target_pointer": "/arguments/product_id",
                        "source": {"kind": "step_output", "step_id": "discover", "pointer": "/items/0/id"},
                    }
                ],
            },
        ],
    }


def test_action_plan_validates_and_materializes_previous_output() -> None:
    plan = validate_action_plan(_plan())
    assert len(plan["steps"]) == 2
    decision = materialize_step(plan["steps"][1], {"discover": {"items": [{"id": "product_42"}]}})
    assert decision["tool"] == "media.prepare"
    assert decision["arguments"]["product_id"] == "product_42"
    assert decision["_plan_step_id"] == "prepare"


def test_action_plan_rejects_forward_binding() -> None:
    plan = _plan()
    plan["steps"][0]["bindings"] = [
        {
            "target_pointer": "/arguments/product_id",
            "source": {"kind": "step_output", "step_id": "prepare", "pointer": "/id"},
        }
    ]
    with pytest.raises(ValueError, match="earlier step"):
        validate_action_plan(plan)


def test_action_plan_rejects_secret_shaped_binding() -> None:
    plan = _plan()
    plan["steps"][1]["bindings"][0]["source"]["pointer"] = "/secret/token"
    with pytest.raises(ValueError, match="secret"):
        validate_action_plan(plan)


def test_action_plan_rejects_secret_shaped_arguments() -> None:
    plan = _plan()
    plan["steps"][0]["arguments"] = {"api_key": "should-not-be-in-a-plan"}
    with pytest.raises(ValueError, match="credential"):
        validate_action_plan(plan)


def test_compound_gate_is_conservative() -> None:
    assert _looks_compound_objective("create a video then publish it")
    assert not _looks_compound_objective("show the latest video")
    assert not _looks_compound_objective(
        "Find the first available operation in the integration registry, then report its name and image URL."
    )
    assert _looks_compound_objective("find the product then fetch its availability")


def test_legacy_step_check_constraint_is_migrated() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE operator_runs (id TEXT PRIMARY KEY)")
    db.execute(
        """CREATE TABLE operator_run_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            step_type TEXT NOT NULL CHECK (step_type IN ('model_decision','tool_call','wait','resume','failure','finish','approval_request')),
            status TEXT NOT NULL DEFAULT 'succeeded', tool_name TEXT NOT NULL DEFAULT '',
            decision_json TEXT NOT NULL DEFAULT '{}', input_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}', job_id TEXT, approval_id TEXT,
            provider TEXT NOT NULL DEFAULT '', protocol TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            usage_exact INTEGER NOT NULL DEFAULT 0, provider_attempts INTEGER NOT NULL DEFAULT 0,
            prompt_chars INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0,
            error_code TEXT NOT NULL DEFAULT '', error_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(run_id, sequence)
        )"""
    )
    db.execute("CREATE INDEX idx_operator_run_steps_run ON operator_run_steps(run_id, sequence)")
    db.execute("INSERT INTO operator_run_steps(run_id,tenant_id,project_id,sequence,step_type) VALUES ('r','t','p',1,'finish')")
    _migrate_action_plan_step_type(db)
    db.execute("INSERT INTO operator_run_steps(run_id,tenant_id,project_id,sequence,step_type) VALUES ('r','t','p',2,'action_plan')")
    assert db.execute("SELECT COUNT(*) FROM operator_run_steps").fetchone()[0] == 2
