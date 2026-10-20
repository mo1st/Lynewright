from __future__ import annotations

import copy
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.services.assistant_tools import register_assistant_tools
from app.services.automation_plans import AutomationPlanService
from app.services.core_tools import CoreToolRuntime
from app.services.line_engine import ProductionLineEngine
from app.services.platform_capabilities import (
    AUTOMATION_CAPABILITIES, INTEGRATION_DISCOVERY_CAPABILITIES,
    CapabilityActor, ClientAuthorizationService, allowed_capabilities,
    require_capability,
)
from app.services.platform_services import (
    PlatformConflictError, PlatformNotFoundError, PlatformOperationError,
    PlatformStore, PlatformValidationError,
)

TENANT = "ten_automation_test"
PROJECT = "automation-project"
SCOPES = ["automation:read", "automation:plan", "automation:provision", "integrations:discover", "sources:read"]


class InstalledOperations:
    def __init__(self):
        self.revision = "operation-v1"

    def resolve_operation(self, actor, *, project_id, operation_id="", contract="", operation="", **_kwargs):
        if project_id != PROJECT or actor.tenant_id != TENANT:
            raise PlatformNotFoundError("Operation not found.")
        require_capability(actor, "sources:read")
        if operation_id not in {"", "installed-research"} or contract not in {"", "example.research"} or operation not in {"", "find"}:
            raise PlatformNotFoundError("Operation not found.")
        return {"descriptor": {
            "operation_id": "installed-research", "revision": self.revision,
            "integration_id": "example.integration", "connection_id": "example-connection",
            "contract": "example.research", "operation": "find", "capability": "sources:read",
            "risk_class": "READ_ONLY", "kind": "openapi", "read_only": True,
            "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
            "output_schema": {"type": "object", "properties": {"items": {"type": "array"}}},
        }}

    def search(self, actor, *, project_id, **_kwargs):
        return [self.resolve_operation(actor, project_id=project_id)["descriptor"]]


@pytest.fixture()
def setup(monkeypatch):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    service = AutomationPlanService(store)
    clients = ClientAuthorizationService(store)
    operation_directory = InstalledOperations()

    def check_project(*, tenant_id, project_id):
        if tenant_id != TENANT or project_id != PROJECT:
            raise ValueError("Project not found.")
        return {"tenant_id": tenant_id, "project_id": project_id}

    monkeypatch.setattr("app.services.assistant_tools.require_project_in_tenant", check_project)
    monkeypatch.setattr("app.services.integration_operations.build_tenant_operation_catalog", lambda _tenant: operation_directory)
    runtime = CoreToolRuntime(store=store)
    register_assistant_tools(runtime, include_automation=True)
    client = clients.create_client(
        {"name": "Planning assistant", "permission_mode": "custom", "granted_capabilities": SCOPES},
        actor=CapabilityActor.platform_manager(), tenant_id=TENANT, project_ids=[PROJECT],
    )
    actor = clients.authenticate(client["access_token"], audience="mcp")
    yield connection, store, service, clients, operation_directory, runtime, actor
    connection.close()


def payload():
    return {"project_id": PROJECT, "objective": "Research a topic", "line_name": "Research preview",
            "definition": {"stages": [{"id": "research", "kind": "operation", "contract": "example.research",
                                        "operation": "find", "args_template": {"topic": "example"}}]}}


def create(setup):
    _db, _store, service, _clients, operation_directory, _runtime, actor = setup
    return service.create_or_revise(payload(), tenant_id=TENANT, created_by="assistant",
                                    originating_client_id=actor.client_id,
                                    operation_directory=operation_directory, operation_actor=actor)


def approve(setup, plan):
    return setup[2].authorize(plan["id"], tenant_id=TENANT, project_id=PROJECT,
                              revision=plan["revision"], plan_hash=plan["plan_hash"], approved_by="admin@example.test")


def apply(setup, plan):
    return setup[2].apply_preview(plan["id"], tenant_id=TENANT, project_id=PROJECT,
                                  revision=plan["revision"], plan_hash=plan["plan_hash"], created_by="assistant",
                                  operation_directory=setup[4], operation_actor=setup[6])


def test_plan_requires_exact_approval_and_applies_disabled_once(setup):
    plan = create(setup)
    assert not plan["authorization"]["approved"]
    assert plan["operation_snapshots"][0]["revision"] == "operation-v1"
    with pytest.raises(PlatformConflictError, match="approve"):
        apply(setup, plan)
    with pytest.raises(PlatformConflictError, match="changed"):
        setup[2].authorize(plan["id"], tenant_id=TENANT, project_id=PROJECT, revision=1,
                           plan_hash="0" * 64, approved_by="admin@example.test")
    approve(setup, plan)
    first = apply(setup, plan)
    second = apply(setup, plan)
    assert first == second
    assert first["line_preview"]["persisted"] and not first["line_preview"]["enabled"]
    engine = ProductionLineEngine(setup[1], tenant_id=TENANT)
    assert len(engine.db.list_lines(project_id=PROJECT)) == 1
    with pytest.raises(PlatformConflictError, match="disabled|enabled"):
        engine.start(first["line_preview"]["id"], project_id=PROJECT, variables={})


@pytest.mark.parametrize("table,action", [
    ("automation_plan_revisions", "UPDATE automation_plan_revisions SET payload_json='{}'"),
    ("automation_plan_revisions", "DELETE FROM automation_plan_revisions"),
    ("automation_authorizations", "UPDATE automation_authorizations SET plan_hash='changed'"),
    ("automation_authorizations", "DELETE FROM automation_authorizations"),
])
def test_approved_evidence_cannot_be_rewritten_or_deleted(setup, table, action):
    plan = create(setup)
    approve(setup, plan)
    with pytest.raises(sqlite3.IntegrityError, match="immutable|append-only"):
        setup[0].execute(action)
    assert setup[0].execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_revision_requires_fresh_approval_and_preserves_history(setup):
    plan = create(setup)
    approve(setup, plan)
    revised = payload() | {"objective": "Research another topic", "plan_id": plan["id"], "expected_revision": 1}
    current = setup[2].create_or_revise(revised, tenant_id=TENANT, created_by="assistant",
                                      originating_client_id=setup[6].client_id,
                                      operation_directory=setup[4], operation_actor=setup[6])
    assert current["revision"] == 2
    assert current["plan_hash"] != plan["plan_hash"]
    assert not current["authorization"]["approved"]
    assert setup[0].execute("SELECT COUNT(*) FROM automation_plan_revisions").fetchone()[0] == 2
    with pytest.raises(PlatformConflictError, match="current"):
        apply(setup, plan)
    with pytest.raises(PlatformConflictError, match="approve"):
        apply(setup, current)


def test_operation_drift_invalidates_approval(setup):
    plan = create(setup)
    approve(setup, plan)
    setup[4].revision = "operation-v2"
    with pytest.raises(PlatformConflictError, match="operation changed"):
        apply(setup, plan)
    assert not ProductionLineEngine(setup[1], tenant_id=TENANT).db.list_lines(project_id=PROJECT)


def test_expired_approval_cannot_create_line(setup, monkeypatch):
    plan = create(setup)
    approved = approve(setup, plan)
    monkeypatch.setattr("app.services.automation_plans.time.time", lambda: approved["authorization"]["expires_at_epoch"] + 1)
    with pytest.raises(PlatformConflictError, match="expired"):
        apply(setup, plan)


@pytest.mark.parametrize("tenant,project", [(TENANT, "another-project"), ("another-tenant", PROJECT)])
def test_plan_cannot_be_read_or_authorized_outside_its_scope(setup, tenant, project):
    plan = create(setup)
    with pytest.raises(PlatformNotFoundError):
        setup[2].get_plan(plan["id"], tenant_id=tenant, project_id=project)
    with pytest.raises(PlatformNotFoundError):
        setup[2].authorize(plan["id"], tenant_id=tenant, project_id=project, revision=1,
                           plan_hash=plan["plan_hash"], approved_by="admin@example.test")


def test_full_access_does_not_silently_gain_new_automation_rights(setup):
    client = setup[3].create_client({"name": "Legacy full", "permission_mode": "full"},
                                    actor=CapabilityActor.platform_manager(), tenant_id=TENANT, project_ids=[PROJECT])
    actor = setup[3].authenticate(client["access_token"])
    assert not allowed_capabilities(actor) & (AUTOMATION_CAPABILITIES | INTEGRATION_DISCOVERY_CAPABILITIES)
    with pytest.raises(PlatformOperationError):
        setup[5].invoke("automation.plan", actor, payload())


@pytest.mark.parametrize("audience,wrong", [("mcp", "api"), ("api", "mcp")])
def test_service_credentials_are_bound_to_their_audience(setup, audience, wrong):
    client = setup[3].create_client({"name": f"Audience {audience}", "audience": audience},
                                    actor=CapabilityActor.platform_manager(), tenant_id=TENANT, project_ids=[PROJECT])
    assert setup[3].authenticate(client["access_token"], audience=audience)
    with pytest.raises(PlatformOperationError):
        setup[3].authenticate(client["access_token"], audience=wrong)


@pytest.fixture()
def rest(setup, monkeypatch):
    from app.routers import automation_plans as router
    monkeypatch.setattr(router, "AutomationPlanService", lambda: setup[2])
    monkeypatch.setattr(router, "ClientAuthorizationService", lambda: setup[3])
    monkeypatch.setattr(router, "require_project_in_tenant", lambda **_kwargs: None)
    monkeypatch.setattr(router, "get_current_user", lambda authorization=None: {
        "Bearer admin-session": {"id": 1, "email": "admin@example.test", "role": "admin"},
        "Bearer editor-session": {"id": 2, "email": "editor@example.test", "role": "editor"},
    }.get(authorization))
    def unauthenticated(_user):
        raise HTTPException(status_code=401, detail="Authentication required")
    monkeypatch.setattr(router, "require_user", unauthenticated)
    monkeypatch.setattr(router, "require_tenant_context", lambda *, user, **_kwargs: {
        "tenant_id": TENANT, "role": user["role"], "user_id": user["id"],
    })
    class Limiter:
        def consume(self, **_kwargs):
            return True
    monkeypatch.setattr("app.services.rate_limiter.get_rate_limiter", Limiter)
    client = setup[3].create_client({"name": "REST planner", "audience": "api", "permission_mode": "custom",
                                    "granted_capabilities": SCOPES}, actor=CapabilityActor.platform_manager(),
                                   tenant_id=TENANT, project_ids=[PROJECT])
    app = FastAPI()
    app.include_router(router.router)
    with TestClient(app) as http:
        yield http, {"Authorization": f"Bearer {client['access_token']}"}


def test_rest_and_mcp_share_plan_validation_and_application(setup, rest):
    http, headers = rest
    mcp_plan = setup[5].invoke("automation.plan", setup[6], copy.deepcopy(payload()))
    response = http.post("/api/automation/plans", headers=headers, json=payload())
    assert response.status_code == 201, response.text
    plan = response.json()
    assert plan["line_preview"]["definition"] == mcp_plan["line_preview"]["definition"]
    assert plan["operation_snapshots"] == mcp_plan["operation_snapshots"]
    body = {"project_id": PROJECT, "revision": plan["revision"], "plan_hash": plan["plan_hash"]}
    assert http.post(f"/api/automation/plans/{plan['id']}/apply", headers=headers, json=body).status_code == 409
    assert http.post(f"/api/automation/plans/{plan['id']}/authorize", headers=headers, json=body).status_code == 403
    assert http.post(f"/api/automation/plans/{plan['id']}/authorize", headers={"Authorization": "Bearer editor-session"}, json=body).status_code == 403
    assert http.post(f"/api/automation/plans/{plan['id']}/authorize", headers={"Authorization": "Bearer admin-session"}, json=body).status_code == 200
    applied = http.post(f"/api/automation/plans/{plan['id']}/apply", headers=headers, json=body)
    assert applied.status_code == 200, applied.text
    assert applied.json()["line_preview"]["persisted"]
    assert not applied.json()["line_preview"]["enabled"]


def test_rest_rejects_mcp_credentials_anonymous_requests_and_wrong_projects(setup, rest):
    http, headers = rest
    assert http.get("/api/automation/plans", params={"project_id": PROJECT}).status_code == 401
    mcp = setup[3].create_client({"name": "Wrong REST audience", "granted_capabilities": SCOPES},
                                actor=CapabilityActor.platform_manager(), tenant_id=TENANT, project_ids=[PROJECT])
    assert http.get("/api/automation/plans", params={"project_id": PROJECT}, headers={"Authorization": f"Bearer {mcp['access_token']}"}).status_code == 401
    assert http.get("/api/automation/plans", params={"project_id": "another-project"}, headers=headers).status_code == 404
    result = http.get("/api/automation/operations", params={"project_id": PROJECT, "q": "research"}, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["schema"]["required"] == ["topic"]


def test_planner_without_apply_permission_cannot_apply(setup):
    client = setup[3].create_client({"name": "Draft only", "permission_mode": "custom", "granted_capabilities": ["automation:plan", "sources:read"]},
                                    actor=CapabilityActor.platform_manager(), tenant_id=TENANT, project_ids=[PROJECT])
    actor = setup[3].authenticate(client["access_token"])
    plan = setup[5].invoke("automation.plan", actor, payload())
    approve(setup, plan)
    with pytest.raises(PlatformOperationError):
        setup[5].invoke("automation.apply", actor, {"project_id": PROJECT, "plan_id": plan["id"], "revision": 1, "plan_hash": plan["plan_hash"]})
