from __future__ import annotations

import json
import sqlite3

from app.services.operator_runtime import store
from app.services.operator_runtime.context import (
    compact_history,
    estimate_tokens,
    project_result,
    rank_tools,
    search_terms,
    task_memory,
)
from app.services.operator_runtime.loop import OperatorLoop, _compact_input_schema
from app.services.integration_operations import _merge_missing, _resource_fields, _resource_hint, _schema_validation_error
from app.services.operator_runtime.schema import init_operator_schema
from app.services.operator_retrieval import search_project_evidence


def test_search_terms_are_unicode_aware_and_remove_request_noise() -> None:
    terms = search_terms("هل أنشئ فيديو جديداً للنشر في فيسبوك؟")
    assert "فيديو" in terms
    assert "فيسبوك" in terms
    assert "أنشئ" in terms
    assert "في" not in terms


def test_rank_tools_finds_future_operation_without_provider_alias() -> None:
    tools = [
        {"name": "lynewright.future.resource.publish", "description": "Publish a resource to a connected service.", "keywords": "resource service listing publish", "capability": "integration.operation.invoke"},
        {"name": "lynewright.future.research.search", "description": "Search research sources for a topic.", "capability": "integration.operation.invoke"},
        {"name": "lynewright.unrelated.cleanup", "description": "Remove an old temporary item.", "capability": "integration.operation.invoke"},
    ]
    ranked = rank_tools(tools, query="publish a resource listing")
    assert ranked[0]["name"] == "lynewright.future.resource.publish"


def test_project_result_preserves_refs_and_bounds_large_payload() -> None:
    value = {
        "status": "completed", "media_id": "med_123", "execution_id": "exec_123",
        "items": [{"id": str(index), "title": "x" * 100} for index in range(50)],
        "secret_like": "should not be selected",
    }
    projected = project_result(value, max_chars=300)
    encoded = json.dumps(projected, ensure_ascii=False)
    assert len(encoded) <= 360
    assert projected["status"] == "completed"
    assert projected["media_id"] == "med_123"


def test_project_result_bounds_list_results_and_keeps_newest_candidate_fields() -> None:
    value = [
        {
            "id": "med_new",
            "name": "final.mp4",
            "content_type": "video/mp4",
            "created_at": "2026-09-11 12:34:45",
            "url": "managed://med_new",
            "metadata": {"origin_url": "/tasks/new/final-1.mp4"},
        }
    ] + [
        {"id": f"med_{index}", "name": "final.mp4", "content_type": "video/mp4", "url": f"managed://med_{index}"}
        for index in range(20)
    ]
    projected = project_result(value, max_chars=520)
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 520
    assert projected["truncated"] is True
    assert projected["total"] == len(value)
    assert projected["items"][0]["id"] == "med_new"
    assert projected["items"][0]["created_at"] == "2026-09-11 12:34:45"


def test_task_memory_retains_verified_operation_and_artifact_refs() -> None:
    memory = task_memory(
        objective="create and publish a product video",
        context={},
        recent_steps=[
            {"sequence": 1, "step_type": "tool_call", "tool_name": "lynewright.integrations.search_operations", "status": "succeeded", "output": {"operation_id": "op_1"}},
            {"sequence": 2, "step_type": "tool_call", "tool_name": "lynewright.integrations.prepare_invocation", "status": "succeeded", "output": {"operation_id": "op_1"}},
            {"sequence": 3, "step_type": "tool_call", "tool_name": "lynewright.media.video.render", "status": "succeeded", "output": {"execution_id": "exec_1", "media_id": "med_1"}},
        ],
    )
    assert memory["version"] == 2
    assert memory["selected_operation"] == {
        "tool": "lynewright.integrations.prepare_invocation", "operation_id": "op_1",
    }
    assert {item["kind"] for item in memory["artifacts"]} >= {"execution_id", "media_id"}


def test_task_memory_does_not_duplicate_existing_checkpoint_facts() -> None:
    step = {"sequence": 7, "tool_name": "lynewright.media.video.render", "status": "succeeded", "output": {"media_id": "med_7"}}
    first = task_memory(objective="render", context={}, recent_steps=[step])
    second = task_memory(objective="render", context={"task_memory": first}, recent_steps=[step])
    assert second["completed_stages"] == first["completed_stages"]
    assert second["artifacts"] == first["artifacts"]


def test_task_memory_creates_a_bounded_handoff_for_any_successful_read() -> None:
    step = {
        "sequence": 2,
        "tool_name": "lynewright.media.list",
        "status": "succeeded",
        "decision": {"arguments": {}},
        "output": [
            {"id": "med_new", "name": "final.mp4", "content_type": "video/mp4", "created_at": "2026-09-11"},
            {"id": "med_old", "name": "final.mp4", "content_type": "video/mp4", "created_at": "2026-09-01"},
        ],
    }
    memory = task_memory(
        objective="get the latest video and publish it",
        context={},
        recent_steps=[step],
        read_tools={"lynewright.media.list"},
    )
    assert memory["last_read"]["tool"] == "lynewright.media.list"
    assert memory["last_read"]["result"][0]["id"] == "med_new"
    assert "do not repeat" in memory["last_read"]["next_action"]


def test_selected_schema_keeps_validation_constraints_and_definitions() -> None:
    schema = {
        "type": "object", "required": ["visibility", "target"], "additionalProperties": False,
        "properties": {
            "visibility": {"type": "string", "enum": ["draft", "public"]},
            "target": {"$ref": "#/$defs/target"},
        },
        "$defs": {"target": {"type": "object", "required": ["accountId"], "properties": {
            "accountId": {"type": "string", "pattern": "^acct_"},
        }}},
    }
    compact = _compact_input_schema(schema)
    assert compact["properties"]["visibility"]["enum"] == ["draft", "public"]
    assert compact["properties"]["target"]["$ref"] == "#/$defs/target"
    assert compact["$defs"]["target"]["required"] == ["accountId"]


def test_generic_resource_paths_support_nested_and_camel_case_identifiers() -> None:
    schema = {
        "type": "object", "required": ["options"], "properties": {
            "options": {"$ref": "#/$defs/options"},
        },
        "$defs": {"options": {"type": "object", "required": ["accountId"], "properties": {
            "accountId": {"type": "string"},
        }}},
    }
    assert _resource_hint("accountId") == "account"
    assert _resource_fields(schema) == [{
        "field": "options.accountId", "path": ["options", "accountId"],
        "name": "accountId", "resource_type": "account",
    }]
    assert _merge_missing({"options": {"title": "Hello"}}, {"options": {"accountId": "acct_1"}}) == {
        "options": {"title": "Hello", "accountId": "acct_1"},
    }


def test_generic_schema_validation_enforces_enum_pattern_and_nested_required_fields() -> None:
    schema = {
        "type": "object", "required": ["visibility", "target"], "additionalProperties": False,
        "properties": {
            "visibility": {"type": "string", "enum": ["draft", "public"]},
            "target": {"type": "object", "required": ["accountId"], "properties": {
                "accountId": {"type": "string", "pattern": "^acct_"},
            }},
        },
    }
    assert _schema_validation_error(schema, {"visibility": "draft", "target": {"accountId": "acct_1"}}) == ""
    assert "visibility" in _schema_validation_error(schema, {"visibility": "private", "target": {"accountId": "acct_1"}})
    assert "accountId" in _schema_validation_error(schema, {"visibility": "draft", "target": {"accountId": "bad"}})


def test_compact_history_prefers_relevant_turns_with_a_hard_character_bound() -> None:
    history = [
        {"role": "user", "text": "unrelated " * 100},
        {"role": "user", "text": "publish the generated video to a social network"},
        {"role": "assistant", "text": "the video artifact is ready"},
    ]
    compact = compact_history(history, query="publish video", max_chars=100)
    assert compact
    assert sum(len(item["text"]) for item in compact) <= 100
    assert any("video" in item["text"] for item in compact)


def test_multilingual_estimate_is_nonzero() -> None:
    assert estimate_tokens("أنشئ فيديو للمنتج") >= 1


def test_operator_provider_attempts_are_bounded_and_configurable(monkeypatch) -> None:
    monkeypatch.delenv("LYNEWRIGHT_OPERATOR_MAX_PROVIDER_ATTEMPTS", raising=False)
    settings = OperatorLoop._resolve_settings(OperatorLoop.__new__(OperatorLoop))
    assert settings["_max_provider_attempts"] == 1
    monkeypatch.setenv("LYNEWRIGHT_OPERATOR_MAX_PROVIDER_ATTEMPTS", "3")
    settings = OperatorLoop._resolve_settings(OperatorLoop.__new__(OperatorLoop))
    assert settings["_max_provider_attempts"] == 3


def test_provider_attempt_ledger_is_durable_and_idempotent() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init_operator_schema(db)
    db.execute(
        "INSERT INTO operator_profiles (id,tenant_id,project_id,name) VALUES (?,?,?,?)",
        ("profile_1", "tenant_1", "project_1", "Ops"),
    )
    db.execute(
        """INSERT INTO operator_runs
           (id,profile_id,tenant_id,project_id,objective)
           VALUES (?,?,?,?,?)""",
        ("run_1", "profile_1", "tenant_1", "project_1", "test"),
    )
    run_row = db.execute("SELECT * FROM operator_runs WHERE id='run_1'").fetchone()
    records = [{
        "attempt_id": "aiatt_test",
        "ordinal": 1,
        "provider": "openai",
        "profile_id": "profile_1",
        "protocol": "openai_chat",
        "model": "small",
        "network_attempt": True,
        "outcome": "succeeded",
        "usage": {"input_tokens": 12, "output_tokens": 4, "exact": 1},
    }]
    assert store.append_ai_attempts(db, run_row, decision_sequence=1, records=records) == 1
    assert store.append_ai_attempts(db, run_row, decision_sequence=1, records=records) == 0
    attempts = store.ai_attempts_for_run(db, "run_1")
    assert len(attempts) == 1
    assert attempts[0]["input_tokens"] == 12
    assert attempts[0]["network_attempt"] is True


def test_optional_profile_budget_is_checked_against_durable_attempts() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init_operator_schema(db)
    profile = store.create_profile(
        db, tenant_id="tenant_1", project_id="project_1", name="Budgeted",
        instructions="", granted_capabilities=frozenset({"system:read"}), enabled=True,
        max_steps=12, max_consecutive_tool_failures=3, max_model_failures=2,
        max_total_tokens=700, max_model_decisions=0, max_network_attempts=0,
        no_progress_limit=3,
    )
    run, _ = store.create_run(
        db, profile_row=profile, tenant_id="tenant_1", project_id="project_1",
        objective="test budget", context={}, idempotency_key="", created_by="test",
    )
    store.append_ai_attempts(db, run, decision_sequence=1, records=[{
        "attempt_id": "aiatt_budget", "ordinal": 1, "provider": "openai",
        "network_attempt": True, "outcome": "succeeded",
        "usage": {"input_tokens": 100, "output_tokens": 10, "exact": 1},
    }])
    failure = OperatorLoop._admission_failure(
        db, run_row=run, profile_row=profile,
        payload={"tool_context": {"estimated_input_tokens": 1}},
        settings={"_operator_decision_max_tokens": 768},
    )
    assert failure is not None
    assert failure[0] == "token_budget_exhausted"


def test_authoritative_result_refs_redact_without_dropping_collection_members() -> None:
    value = {"items": [{"id": str(index), "title": "item"} for index in range(240)], "api_key": "secret"}
    encoded = store.canonical_authoritative_json(value)
    parsed = json.loads(encoded)
    assert len(parsed["items"]) == 240
    assert parsed["api_key"] == "[REDACTED]"


def test_executor_lease_fences_a_second_executor_and_releases_cleanly() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init_operator_schema(db)
    db.execute(
        "INSERT INTO operator_profiles (id,tenant_id,project_id,name) VALUES (?,?,?,?)",
        ("profile_lease", "tenant_1", "project_1", "Lease"),
    )
    db.execute(
        "INSERT INTO operator_runs (id,profile_id,tenant_id,project_id,objective) VALUES (?,?,?,?,?)",
        ("run_lease", "profile_lease", "tenant_1", "project_1", "test"),
    )
    first = store.acquire_executor_lease(
        db, run_id="run_lease", owner_id="worker-a", lease_seconds=30,
    )
    assert first is not None
    assert store.acquire_executor_lease(
        db, run_id="run_lease", owner_id="worker-b", lease_seconds=30,
    ) is None
    assert store.renew_executor_lease(
        db, run_id="run_lease", fencing_token=first["fencing_token"], lease_seconds=30,
    )["owner_id"] == "worker-a"
    assert store.release_executor_lease(
        db, run_id="run_lease", fencing_token=first["fencing_token"],
    )
    assert store.acquire_executor_lease(
        db, run_id="run_lease", owner_id="worker-b", lease_seconds=30,
    ) is not None


def test_durable_hybrid_retrieval_ranks_arabic_intent_against_english_evidence() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init_operator_schema(db)
    from app.services.operator_retrieval import register_source_card_provider

    unregister = register_source_card_provider(lambda _db, **scope: [{
        "id": "resources:1", "source_kind": "resources", "source_id": "1",
        "title": "Publish video", "subtitle": "", "status": "ready",
        "destination_url": "",
    }] if scope == {"tenant_id": "tenant_1", "project_id": "project_1"} else [])
    try:
        result = search_project_evidence(
            db, tenant_id="tenant_1", project_id="project_1",
            query="انشر فيديو", requested_types=["resources"], limit=5,
        )
    finally:
        unregister()
    assert result["items"]
    assert result["items"][0]["id"] == "resources:1"
    assert result["retrieval"]["index_revision"]


def test_retrieval_canary_is_bounded_and_deterministic_per_run() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init_operator_schema(db)
    db.execute(
        "INSERT INTO operator_profiles (id,tenant_id,project_id,name,retrieval_mode,retrieval_canary_percent) VALUES (?,?,?,?,?,?)",
        ("profile_canary", "tenant_1", "project_1", "Canary", "hybrid_local", 0),
    )
    db.execute(
        "INSERT INTO operator_runs (id,profile_id,tenant_id,project_id,objective) VALUES (?,?,?,?,?)",
        ("run_canary", "profile_canary", "tenant_1", "project_1", "test"),
    )
    profile = db.execute("SELECT * FROM operator_profiles WHERE id='profile_canary'").fetchone()
    run = db.execute("SELECT * FROM operator_runs WHERE id='run_canary'").fetchone()
    assert OperatorLoop._retrieval_mode(profile_row=profile, run_row=run) == "lexical"
    db.execute("UPDATE operator_profiles SET retrieval_canary_percent=100 WHERE id='profile_canary'")
    profile = db.execute("SELECT * FROM operator_profiles WHERE id='profile_canary'").fetchone()
    assert OperatorLoop._retrieval_mode(profile_row=profile, run_row=run) == "hybrid_local"
