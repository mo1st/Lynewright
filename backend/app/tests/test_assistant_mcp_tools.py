from __future__ import annotations

import sqlite3

import pytest
from starlette.testclient import TestClient

from app.services.assistant_tools import register_assistant_tools
from app.services.core_tools import CoreToolRuntime
from app.services.line_engine import ProductionLineEngine
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformNotFoundError, PlatformOperationError, PlatformStore, PlatformValidationError


TENANT = "ten_assistant_test"
PROJECT = "assistant-project"


def _actor(*, projects: tuple[str, ...] = (PROJECT,), email: str = "admin@example.test") -> CapabilityActor:
    return CapabilityActor(
        client_id="client-assistant", name="Review Assistant", client_type="mcp",
        permission_mode="custom", granted_capabilities=frozenset({
            "lines:read", "runs:read", "runs:start", "runs:cancel", "runs:retry",
            "reviews:read", "reviews:decide",
        }), tenant_id=TENANT, project_ids=projects, on_behalf_of=email,
    )


def _runtime(monkeypatch) -> tuple[CoreToolRuntime, PlatformStore, ProductionLineEngine]:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    def require_project(*, tenant_id: str, project_id: str) -> dict[str, str]:
        if tenant_id != TENANT or project_id != PROJECT:
            raise ValueError("project is outside the test tenant")
        return {"tenant_id": tenant_id, "project_id": project_id}

    monkeypatch.setattr("app.services.assistant_tools.require_project_in_tenant", require_project)
    runtime = CoreToolRuntime(store=store)
    register_assistant_tools(runtime)
    engine = ProductionLineEngine(store=store, tenant_id=TENANT)
    engine.db.put_line(
        "assistant-review-line", name="Assistant review", project_id=PROJECT,
        definition={"stages": [{"id": "review", "kind": "gate:review", "items_template": [{"id": "draft-1", "title": "Draft"}]}]},
        enabled=True, actor_id="admin", operation_directory=set(),
    )
    return runtime, store, engine


def test_assistant_tools_are_registered_with_expected_annotations() -> None:
    runtime = CoreToolRuntime()
    register_assistant_tools(runtime)
    definitions = {item.name: item for item in runtime.definitions()}
    assert {"lines.list", "lines.get", "runs.list", "runs.get", "runs.start", "runs.cancel", "runs.retry_stage", "reviews.list", "reviews.get", "reviews.decide"} <= definitions.keys()
    assert definitions["lines.list"].read_only is True
    assert definitions["reviews.get"].read_only is True
    assert definitions["runs.cancel"].destructive is True
    assert definitions["reviews.decide"].destructive is True
    assert len(definitions) == 10
    assert not any("user" in item.name or "credential" in item.name or "integration" in item.name for item in definitions.values())


def test_mcp_wire_schema_exposes_named_arguments(monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_PLUGINS", "")
    monkeypatch.setenv("LYNEWRIGHT_MCP_PLUGINS", "")
    monkeypatch.setenv("LYNEWRIGHT_MCP_OAUTH_ENABLED", "0")
    from app.mcp_server import create_mcp_server

    mcp, _registry = create_mcp_server()
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert "line_id" in tools["runs.start"].parameters["properties"]
    assert "arguments" not in tools["runs.start"].parameters["properties"]
    assert tools["reviews.decide"].parameters["properties"]["decision"]["enum"] == ["approve", "reject"]


def test_mcp_tools_list_hides_actions_outside_the_clients_grants(monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_PLUGINS", "")
    monkeypatch.setenv("LYNEWRIGHT_MCP_PLUGINS", "")
    monkeypatch.setenv("LYNEWRIGHT_MCP_OAUTH_ENABLED", "0")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    from app.mcp_server import create_mcp_server

    mcp, registry = create_mcp_server(store)
    provisioned = registry.clients.create_client(
        {"name": "Read-only assistant", "permission_mode": "custom", "granted_capabilities": ["lines:read", "runs:read"]},
        actor=CapabilityActor.platform_manager(), tenant_id=TENANT, project_ids=[PROJECT],
    )
    headers = {
        "Authorization": f"Bearer {provisioned['access_token']}",
        "Accept": "application/json, text/event-stream",
    }
    initialize = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    }
    listed = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    with TestClient(mcp.streamable_http_app(), base_url="http://127.0.0.1:8000") as http:
        assert http.post("/mcp", headers=headers, json=initialize).status_code == 200
        response = http.post("/mcp", headers=headers, json=listed)
        assert response.status_code == 200, response.text
        visible = {item["name"] for item in response.json()["result"]["tools"]}
    assert {"lines.list", "lines.get", "runs.list", "runs.get"} <= visible
    assert not {"runs.start", "runs.cancel", "runs.retry_stage", "reviews.list", "reviews.get", "reviews.decide"} & visible
    db.close()


def test_mcp_server_does_not_run_api_plugins_as_mcp_plugins(monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_PLUGINS", "app.integrations.demo.bootstrap")
    monkeypatch.setenv("LYNEWRIGHT_MCP_PLUGINS", "")
    monkeypatch.setenv("LYNEWRIGHT_MCP_OAUTH_ENABLED", "0")
    from app.mcp_server import create_mcp_server

    mcp, _registry = create_mcp_server()
    assert {tool.name for tool in mcp._tool_manager.list_tools()} >= {"lines.list", "runs.start", "reviews.decide"}


def test_mcp_service_uses_shared_secondary_process_activation(monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_MCP_OAUTH_ENABLED", "0")
    calls = []
    monkeypatch.setattr(
        "app.services.integration_activation.activate_integrations",
        lambda **kwargs: calls.append(kwargs) or {},
    )
    from app.mcp_server import create_mcp_http_app, create_mcp_server
    from app.services.mcp_oauth import MCPAuthorizationProvider

    mcp, _registry = create_mcp_server()
    app = create_mcp_http_app(
        mcp, oauth_provider=MCPAuthorizationProvider(_registry.oauth_service, _registry.clients),
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert calls == [{"source": "lynewright.mcp.startup", "recover_enabled": False}]


def test_project_scope_applies_to_list_and_run_start(monkeypatch) -> None:
    runtime, store, _engine = _runtime(monkeypatch)
    actor = _actor()
    listed = runtime.invoke("lines.list", actor, {"project_id": PROJECT})
    assert [item["id"] for item in listed["items"]] == ["assistant-review-line"]
    with pytest.raises(PlatformNotFoundError, match="authorized scope"):
        runtime.invoke("lines.list", actor, {"project_id": "other-project"})
    with pytest.raises(PlatformOperationError, match="not authorized for this capability"):
        runtime.invoke("lines.list", CapabilityActor(
            client_id="limited", name="Limited", client_type="mcp", permission_mode="custom",
            granted_capabilities=frozenset({"runs:read"}), tenant_id=TENANT,
            project_ids=(PROJECT,), on_behalf_of="admin@example.test",
        ), {"project_id": PROJECT})
    other_tenant = CapabilityActor(
        client_id="other-tenant", name="Other tenant", client_type="mcp", permission_mode="custom",
        granted_capabilities=frozenset({"lines:read"}), tenant_id="ten_other",
        project_ids=(PROJECT,), on_behalf_of="admin@example.test",
    )
    with pytest.raises(PlatformNotFoundError, match="authorized scope"):
        runtime.invoke("lines.list", other_tenant, {"project_id": PROJECT})
    started = runtime.invoke("runs.start", actor, {"line_id": "assistant-review-line", "project_id": PROJECT, "vars": {}})
    assert started["status"] == "waiting"
    assert started["timeline"][0]["actor_id"] == "mcp_client:Review Assistant on behalf of admin@example.test"
    db = store._connection_factory()
    db.close()


@pytest.mark.parametrize(("tool_name", "arguments"), [
    ("lines.list", {"project_id": "outside-project"}),
    ("lines.get", {"line_id": "assistant-review-line", "project_id": "outside-project"}),
    ("runs.list", {"project_id": "outside-project"}),
    ("runs.get", {"run_id": "not-found", "project_id": "outside-project"}),
    ("runs.start", {"line_id": "assistant-review-line", "project_id": "outside-project", "vars": {}}),
    ("runs.cancel", {"run_id": "not-found", "project_id": "outside-project"}),
    ("runs.retry_stage", {"run_id": "not-found", "stage_id": "review", "project_id": "outside-project"}),
    ("reviews.list", {"project_id": "outside-project"}),
    ("reviews.get", {"run_id": "not-found", "project_id": "outside-project"}),
    ("reviews.decide", {"run_id": "not-found", "stage_id": "review", "decision": "approve", "project_id": "outside-project"}),
])
def test_every_assistant_tool_rejects_a_project_outside_the_client_scope(monkeypatch, tool_name, arguments) -> None:
    runtime, store, _engine = _runtime(monkeypatch)
    with pytest.raises(PlatformNotFoundError, match="authorized scope"):
        runtime.invoke(tool_name, _actor(), arguments)
    store._connection_factory().close()


def test_every_assistant_tool_requires_its_declared_capability(monkeypatch) -> None:
    runtime, store, _engine = _runtime(monkeypatch)
    actor = _actor()
    cases = {
        "lines.list": "lines:read", "lines.get": "lines:read",
        "runs.list": "runs:read", "runs.get": "runs:read", "runs.start": "runs:start",
        "runs.cancel": "runs:cancel", "runs.retry_stage": "runs:retry",
        "reviews.list": "reviews:read", "reviews.get": "reviews:read", "reviews.decide": "reviews:decide",
    }
    base_arguments = {
        "lines.list": {"project_id": PROJECT}, "lines.get": {"line_id": "assistant-review-line", "project_id": PROJECT},
        "runs.list": {"project_id": PROJECT}, "runs.get": {"run_id": "any", "project_id": PROJECT},
        "runs.start": {"line_id": "assistant-review-line", "project_id": PROJECT, "vars": {}},
        "runs.cancel": {"run_id": "any", "project_id": PROJECT},
        "runs.retry_stage": {"run_id": "any", "stage_id": "review", "project_id": PROJECT},
        "reviews.list": {"project_id": PROJECT}, "reviews.get": {"run_id": "any", "project_id": PROJECT},
        "reviews.decide": {"run_id": "any", "stage_id": "review", "decision": "approve", "project_id": PROJECT},
    }
    for tool_name, capability in cases.items():
        limited = CapabilityActor(
            client_id=actor.client_id, name=actor.name, client_type=actor.client_type,
            permission_mode="custom", granted_capabilities=frozenset(set(actor.granted_capabilities) - {capability}),
            tenant_id=TENANT, project_ids=(PROJECT,), on_behalf_of=actor.on_behalf_of,
        )
        with pytest.raises(PlatformOperationError, match="not authorized for this capability"):
            runtime.invoke(tool_name, limited, base_arguments[tool_name])
    store._connection_factory().close()


def test_cancel_and_retry_use_existing_engine_and_require_valid_state(monkeypatch) -> None:
    runtime, store, engine = _runtime(monkeypatch)
    actor = _actor()
    waiting = engine.start("assistant-review-line", variables={}, project_id=PROJECT)
    cancelled = runtime.invoke("runs.cancel", actor, {"run_id": waiting["id"], "project_id": PROJECT})
    assert cancelled["status"] == "cancelled"

    failed = engine.start("assistant-review-line", variables={}, project_id=PROJECT)
    engine._stage_update(failed["id"], "review", status="failed", error="fixture failure")
    engine._set_run(failed["id"], status="failed")
    # Retry dispatches through the same engine. Keep the runner queue inert in this unit test.
    monkeypatch.setattr(ProductionLineEngine, "_schedule_stage", lambda *_args, **_kwargs: None)
    retried = runtime.invoke("runs.retry_stage", actor, {
        "run_id": failed["id"], "stage_id": "review", "project_id": PROJECT,
    })
    assert retried["status"] == "running"
    assert any(event["type"] == "stage.retry_requested" and event["actor_id"].startswith("mcp_client:") for event in retried["timeline"])
    store._connection_factory().close()


def test_review_policy_blocks_dashboard_only_then_allows_assistant_and_logs_actor(monkeypatch) -> None:
    runtime, store, engine = _runtime(monkeypatch)
    actor = _actor()
    run = engine.start("assistant-review-line", variables={}, project_id=PROJECT)
    assert run["status"] == "waiting"
    with pytest.raises(PlatformOperationError, match="only in the dashboard"):
        runtime.invoke("reviews.decide", actor, {
            "run_id": run["id"], "stage_id": "review", "decision": "approve", "project_id": PROJECT,
        })
    details = runtime.invoke("reviews.get", actor, {"run_id": run["id"], "stage_id": "review", "project_id": PROJECT})
    assert details["items"][0]["id"] == "draft-1"
    assert "bytes" not in details["items"][0]

    line = engine.db.get_line("assistant-review-line")
    engine.db.put_line(
        line["id"], name=line["name"], project_id=PROJECT,
        definition={**line["definition"], "review_by": "dashboard_or_assistant"},
        enabled=True, actor_id="admin", operation_directory=set(),
    )
    result = runtime.invoke("reviews.decide", actor, {
        "run_id": run["id"], "stage_id": "review", "decision": "approve", "project_id": PROJECT,
    })
    assert result["run"]["status"] == "done"
    assert any(event["actor_id"] == "mcp_client:Review Assistant on behalf of admin@example.test" for event in result["run"]["timeline"])
    with pytest.raises(PlatformValidationError, match="reason is required"):
        # A second run exercises the reject guard before any review mutation.
        next_run = engine.start("assistant-review-line", variables={}, project_id=PROJECT)
        runtime.invoke("reviews.decide", actor, {
            "run_id": next_run["id"], "stage_id": "review", "decision": "reject", "project_id": PROJECT,
        })
    store._connection_factory().close()


def test_review_by_validation_rejects_unknown_values(monkeypatch) -> None:
    runtime, _store, engine = _runtime(monkeypatch)
    with pytest.raises(Exception, match="review_by must be dashboard"):
        engine.db.put_line(
            "bad-review-policy", name="Bad policy", project_id=PROJECT,
            definition={"review_by": "anyone", "stages": [{"id": "review", "kind": "gate:review", "items_template": []}]},
            enabled=True, actor_id="admin", operation_directory=set(),
        )
    assert runtime.get_definition("reviews.decide") is not None


def test_waiting_runs_older_than_the_newest_page_are_still_listed(monkeypatch) -> None:
    runtime, store, engine = _runtime(monkeypatch)
    actor = _actor()
    waiting = engine.start("assistant-review-line", variables={}, project_id=PROJECT)
    assert waiting["status"] == "waiting"
    # Newer runs that are no longer waiting fill the first page.
    for _ in range(3):
        newer = engine.start("assistant-review-line", variables={}, project_id=PROJECT)
        engine.cancel(newer["id"], actor_id="admin")

    listed = runtime.invoke("runs.list", actor, {"project_id": PROJECT, "status": "waiting", "limit": 1})
    assert [run["id"] for run in listed["items"]] == [waiting["id"]]
    reviews = runtime.invoke("reviews.list", actor, {"project_id": PROJECT})
    assert [item["run_id"] for item in reviews["items"]] == [waiting["id"]]
    store._connection_factory().close()
