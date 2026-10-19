"""Thin MCP adapters over the production-line and review services."""
from __future__ import annotations

from typing import Any

from app.services.core_tools import CoreToolRuntime
from app.services.line_engine import ProductionLineEngine
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformOperationError, PlatformValidationError
from app.services.tenant_schema import DEFAULT_TENANT_ID
from app.services.tenancy import require_project_in_tenant
from app.services.tool_models import CoreToolDefinition


def _project(actor: CapabilityActor, requested: str = "") -> tuple[str, str]:
    tenant_id = str(actor.tenant_id or DEFAULT_TENANT_ID)
    allowed = set(actor.project_ids)
    selected = str(requested or "").strip()
    if not selected and len(allowed) == 1:
        selected = next(iter(allowed))
    if not selected or selected not in allowed:
        raise PlatformNotFoundError("Project was not found in this assistant's authorized scope.")
    try:
        require_project_in_tenant(tenant_id=tenant_id, project_id=selected)
    except ValueError as exc:
        raise PlatformNotFoundError("Project was not found in this assistant's authorized scope.") from exc
    return tenant_id, selected


def _actor_label(actor: CapabilityActor) -> str:
    behalf = actor.on_behalf_of.strip() or "authorized user"
    return f"mcp_client:{actor.name} on behalf of {behalf}"


def _engine(actor: CapabilityActor) -> ProductionLineEngine:
    return ProductionLineEngine(tenant_id=actor.tenant_id or DEFAULT_TENANT_ID)


def _line_for_project(engine: ProductionLineEngine, line_id: str, project_id: str) -> dict[str, Any]:
    line = engine.db.get_line(line_id)
    if line["project_id"] not in {"", project_id}:
        raise PlatformNotFoundError("Production line was not found in this assistant's authorized project.")
    return line


def _run_for_project(engine: ProductionLineEngine, run_id: str, project_id: str) -> dict[str, Any]:
    run = engine.get_run(run_id)
    if run["project_id"] != project_id:
        raise PlatformNotFoundError("Production run was not found in this assistant's authorized project.")
    return run


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run["id"], "line_id": run["line_id"], "project_id": run["project_id"],
        "status": run["status"], "current_stage": run["current_stage"],
        "created_at": run["created_at"], "updated_at": run["updated_at"],
        "stages": run["stages"], "timeline": run["timeline"],
    }


def _review_item_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Return the review text and expiring media links, never arbitrary payloads."""
    fields = ("id", "label", "title", "name", "summary", "text", "description", "caption")
    summary = {key: item[key] for key in fields if isinstance(item.get(key), (str, int, float, bool))}
    previews = item.get("media_previews")
    if isinstance(previews, list):
        summary["media_previews"] = [
            {key: value for key, value in media.items() if key in {"media_id", "name", "content_type", "kind", "url"}}
            for media in previews if isinstance(media, dict)
        ]
    return summary


def _items_for_review(engine: ProductionLineEngine, run: dict[str, Any], actor: CapabilityActor) -> list[dict[str, Any]]:
    from app.routers.production_lines import operation_review_items

    context = {
        "tenant_id": actor.tenant_id or DEFAULT_TENANT_ID,
        "project_id": run["project_id"], "user_id": _actor_label(actor),
        "role": "editor", "synthetic": False,
    }
    stage_run = next((item for item in run["stages"] if item["stage_id"] == run["current_stage"]), {})
    output = stage_run.get("output") if isinstance(stage_run, dict) else {}
    waiting = output.get("waiting", {}) if isinstance(output, dict) else {}
    if isinstance(waiting, dict) and waiting.get("kind") == "review":
        try:
            return operation_review_items(str(run["id"]), context=context)["items"]
        except Exception as exc:
            detail = getattr(exc, "detail", "Review items could not be loaded.")
            raise PlatformOperationError(str(detail)) from exc

    stage = engine._stage(str(run["id"]), str(run["current_stage"]))
    raw_items = output.get("items", []) if stage.get("kind") == "gate:review" and isinstance(output, dict) else waiting.get("items", []) if isinstance(waiting, dict) else []
    if not isinstance(raw_items, list):
        return []
    from app.services.review_media import attach_review_media

    attached = attach_review_media(
        raw_items, tenant_id=context["tenant_id"], project_id=run["project_id"],
        actor=context, run_id=str(run["id"]),
    )
    return [_review_item_summary(item) for item in attached]


def _tool(
    runtime: CoreToolRuntime, name: str, capability: str, description: str,
    handler: Any, schema: dict[str, Any], *, read_only: bool = False,
    destructive: bool = False,
) -> None:
    runtime.register_tool(CoreToolDefinition(
        name=name, capability=capability, description=description,
        handler=handler, input_schema=schema, read_only=read_only,
        destructive=destructive, risk_class="READ_ONLY" if read_only else "SAFE_OPERATION",
    ))


def register_assistant_tools(runtime: CoreToolRuntime) -> None:
    """Register tool names as transport wrappers; engine/review services own behavior."""
    obj = {"type": "object", "additionalProperties": False}
    project_schema = obj | {"properties": {"project_id": {"type": "string"}}}

    def lines_list(actor: CapabilityActor, args: dict[str, Any]) -> dict[str, Any]:
        tenant_id, project_id = _project(actor, args.get("project_id", ""))
        engine = ProductionLineEngine(runtime.store, tenant_id=tenant_id)
        items = engine.db.list_lines(project_id=project_id)
        known = {item["id"] for item in items}
        items.extend(item for item in engine.db.list_lines(project_id="") if item["id"] not in known)
        return {"items": items}

    def lines_get(actor: CapabilityActor, args: dict[str, Any]) -> dict[str, Any]:
        tenant_id, project_id = _project(actor, args.get("project_id", ""))
        engine = ProductionLineEngine(runtime.store, tenant_id=tenant_id)
        return _line_for_project(engine, str(args.get("line_id") or ""), project_id)

    def runs_list(actor: CapabilityActor, args: dict[str, Any]) -> dict[str, Any]:
        tenant_id, project_id = _project(actor, args.get("project_id", ""))
        engine = ProductionLineEngine(runtime.store, tenant_id=tenant_id)
        line_id = str(args.get("line_id") or "") or None
        if line_id:
            _line_for_project(engine, line_id, project_id)
        status = str(args.get("status") or "")
        if status and status not in {"queued", "running", "waiting", "done", "failed", "rejected", "cancelled"}:
            raise PlatformValidationError("Unsupported run status filter.")
        limit = max(1, min(int(args.get("limit") or 50), 100))
        items = engine.list_runs(line_id=line_id, project_id=project_id, status=status or None, limit=limit)
        return {"items": [_run_summary(run) for run in items]}

    def runs_get(actor: CapabilityActor, args: dict[str, Any]) -> dict[str, Any]:
        tenant_id, project_id = _project(actor, args.get("project_id", ""))
        engine = ProductionLineEngine(runtime.store, tenant_id=tenant_id)
        return _run_summary(_run_for_project(engine, str(args.get("run_id") or ""), project_id))

    def runs_start(actor: CapabilityActor, args: dict[str, Any]) -> dict[str, Any]:
        tenant_id, project_id = _project(actor, args.get("project_id", ""))
        engine = ProductionLineEngine(runtime.store, tenant_id=tenant_id)
        line = _line_for_project(engine, str(args.get("line_id") or ""), project_id)
        variables = args.get("vars", {})
        if not isinstance(variables, dict):
            raise PlatformValidationError("Run vars must be an object.")
        label = _actor_label(actor)
        run = engine.start(line["id"], variables=variables, project_id=project_id,
                           actor={"user_id": label, "name": label, "client_id": actor.client_id})
        return _run_summary(run)

    def runs_cancel(actor: CapabilityActor, args: dict[str, Any]) -> dict[str, Any]:
        tenant_id, project_id = _project(actor, args.get("project_id", ""))
        engine = ProductionLineEngine(runtime.store, tenant_id=tenant_id)
        _run_for_project(engine, str(args.get("run_id") or ""), project_id)
        return _run_summary(engine.cancel(str(args["run_id"]), actor_id=_actor_label(actor)))

    def runs_retry(actor: CapabilityActor, args: dict[str, Any]) -> dict[str, Any]:
        tenant_id, project_id = _project(actor, args.get("project_id", ""))
        engine = ProductionLineEngine(runtime.store, tenant_id=tenant_id)
        _run_for_project(engine, str(args.get("run_id") or ""), project_id)
        return _run_summary(engine.retry_stage(
            str(args["run_id"]), stage_id=str(args.get("stage_id") or ""),
            actor=actor, actor_id=_actor_label(actor),
        ))

    def reviews_list(actor: CapabilityActor, args: dict[str, Any]) -> dict[str, Any]:
        tenant_id, project_id = _project(actor, args.get("project_id", ""))
        engine = ProductionLineEngine(runtime.store, tenant_id=tenant_id)
        pending = []
        for run in engine.list_runs(project_id=project_id, status="waiting", limit=500):
            if not run["current_stage"]:
                continue
            stage = engine._stage(run["id"], run["current_stage"])
            stage_run = next((item for item in run["stages"] if item["stage_id"] == run["current_stage"]), {})
            output = stage_run.get("output", {}) if isinstance(stage_run, dict) else {}
            waiting = output.get("waiting", {}) if isinstance(output, dict) else {}
            if stage.get("kind") == "gate:review" or (isinstance(waiting, dict) and waiting.get("kind") == "review"):
                raw_items = (
                    waiting.get("items", output.get("items", []))
                    if isinstance(waiting, dict) and isinstance(output, dict)
                    else output.get("items", []) if isinstance(output, dict) else []
                )
                item_count = len(raw_items) if isinstance(raw_items, list) else 0
                line = engine.db.get_line(run["line_id"])
                pending.append({
                    "run_id": run["id"], "line_id": line["id"], "line_name": line["name"],
                    "stage_id": run["current_stage"], "waiting_since": run["updated_at"],
                    "summary": waiting.get("summary") or waiting.get("title") or stage.get("label") or "Review needed",
                    "item_count": item_count,
                })
        return {"items": pending}

    def reviews_get(actor: CapabilityActor, args: dict[str, Any]) -> dict[str, Any]:
        tenant_id, project_id = _project(actor, args.get("project_id", ""))
        engine = ProductionLineEngine(runtime.store, tenant_id=tenant_id)
        run = _run_for_project(engine, str(args.get("run_id") or ""), project_id)
        if run["status"] != "waiting" or not run["current_stage"]:
            raise PlatformConflictError("This run is not waiting for a review decision.")
        if args.get("stage_id") and str(args["stage_id"]) != run["current_stage"]:
            raise PlatformNotFoundError("Review stage was not found.")
        stage = engine._stage(run["id"], run["current_stage"])
        return {
            "run": _run_summary(run), "stage": stage,
            "items": _items_for_review(engine, run, actor),
        }

    def reviews_decide(actor: CapabilityActor, args: dict[str, Any]) -> dict[str, Any]:
        tenant_id, project_id = _project(actor, args.get("project_id", ""))
        engine = ProductionLineEngine(runtime.store, tenant_id=tenant_id)
        run_id = str(args.get("run_id") or "")
        run = _run_for_project(engine, run_id, project_id)
        if str(args.get("stage_id") or "") != run["current_stage"]:
            raise PlatformConflictError("The review stage is no longer current.")
        run = engine.assert_assistant_review_allowed(run_id)
        decision = str(args.get("decision") or "")
        reason = str(args.get("reason") or "").strip()
        if decision not in {"approve", "reject"}:
            raise PlatformValidationError("Decision must be approve or reject.")
        if decision == "reject" and not reason:
            raise PlatformValidationError("A reason is required to reject a review.")
        label = _actor_label(actor)
        stage = engine._stage(run_id, run["current_stage"])
        stage_run = next((item for item in run["stages"] if item["stage_id"] == run["current_stage"]), {})
        output = stage_run.get("output", {}) if isinstance(stage_run, dict) else {}
        waiting = output.get("waiting", {}) if isinstance(output, dict) else {}
        context = {"tenant_id": tenant_id, "project_id": project_id, "user_id": label, "role": "editor", "synthetic": False}
        if isinstance(waiting, dict) and waiting.get("kind") == "review":
            items = _items_for_review(engine, run, actor)
            if not items:
                raise PlatformValidationError("No review items are available to decide.")
            decisions = [{
                "id": item["id"], "decision": "approved" if decision == "approve" else "refused",
                "reason": reason if decision == "reject" else "",
            } for item in items]
            from app.routers.production_lines import submit_operation_review

            try:
                result = submit_operation_review(run_id, {"items": decisions}, context=context)
            except Exception as exc:
                detail = getattr(exc, "detail", "Review decision failed.")
                raise PlatformOperationError(str(detail)) from exc
            return {"decision": decision, "run": _run_summary(result["run"])}
        if stage.get("kind") != "gate:review":
            raise PlatformValidationError("This run is not waiting for a review decision.")
        continued = engine.continue_run(
            run_id, value={"approved": decision == "approve", "reason": reason},
            actor={"user_id": label, "name": label, "client_id": actor.client_id}, actor_id=label,
        )
        return {"decision": decision, "run": _run_summary(continued)}

    line_id_schema = obj | {"required": ["line_id"], "properties": {"line_id": {"type": "string"}, **project_schema["properties"]}}
    run_schema = obj | {"required": ["run_id"], "properties": {"run_id": {"type": "string"}, **project_schema["properties"]}}
    review_schema = obj | {"required": ["run_id"], "properties": {"run_id": {"type": "string"}, "stage_id": {"type": "string"}, **project_schema["properties"]}}
    decision_schema = obj | {"required": ["run_id", "stage_id", "decision"], "properties": {
        "run_id": {"type": "string"}, "stage_id": {"type": "string"},
        "decision": {"type": "string", "enum": ["approve", "reject"]},
        "reason": {"type": "string"}, **project_schema["properties"],
    }}
    tools = [
        ("lines.list", "lines:read", "List production lines in the authorized project.", lines_list, project_schema, True, False),
        ("lines.get", "lines:read", "Get a line definition and its accepted inputs.", lines_get, line_id_schema, True, False),
        ("runs.list", "runs:read", "List runs with optional line/status filters.", runs_list, obj | {"properties": {
            "project_id": {"type": "string"}, "line_id": {"type": "string"},
            "status": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        }}, True, False),
        ("runs.get", "runs:read", "Get a run summary, stage outputs and timeline.", runs_get, run_schema, True, False),
        ("runs.start", "runs:start", "Start a production line with the supplied variables.", runs_start, obj | {"required": ["line_id"], "properties": {
            "line_id": {"type": "string"}, "project_id": {"type": "string"}, "vars": {"type": "object"},
        }}, False, False),
        ("runs.cancel", "runs:cancel", "Cancel an active run.", runs_cancel, run_schema, False, True),
        ("runs.retry_stage", "runs:retry", "Retry a failed stage.", runs_retry, obj | {"required": ["run_id", "stage_id"], "properties": {
            "run_id": {"type": "string"}, "stage_id": {"type": "string"}, **project_schema["properties"],
        }}, False, False),
        ("reviews.list", "reviews:read", "List waiting review gates in the authorized project.", reviews_list, project_schema, True, False),
        ("reviews.get", "reviews:read", "Get a waiting review item and short-lived media links.", reviews_get, review_schema, True, False),
        ("reviews.decide", "reviews:decide", "Approve or reject a review when its line allows assistant decisions.", reviews_decide, decision_schema, False, True),
    ]
    for name, capability, description, handler, schema, read_only, destructive in tools:
        _tool(runtime, name, capability, description, handler, schema,
              read_only=read_only, destructive=destructive)
