"""Tenant-authenticated API for data-defined production lines."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from app.security import require_tenant_context, require_tenant_role
from app.services.line_engine import ProductionLineEngine, _resolve, validate_definition
from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformValidationError
from app.services.tenancy import require_project_in_tenant


router = APIRouter(prefix="/api/production-lines", tags=["production-lines"])
_READ = require_tenant_context
_WRITE = require_tenant_role("editor")
_ADMIN = require_tenant_role("admin")


def _engine(context: dict[str, Any]) -> ProductionLineEngine:
    return ProductionLineEngine(tenant_id=str(context["tenant_id"]))


def _line_catalogue(context: dict[str, Any]) -> Any:
    from app.services.integration_operations import build_tenant_operation_catalog

    return build_tenant_operation_catalog(str(context["tenant_id"]))


def _line_authority() -> Any:
    from app.services.platform_capabilities import CapabilityActor

    return CapabilityActor(
        client_id="production-line-definition", name="Production line definition",
        client_type="automation", permission_mode="full", granted_capabilities=frozenset(),
    )


def _project(context: dict[str, Any], project_id: str) -> str:
    value = str(project_id or "").strip()
    if value:
        try:
            require_project_in_tenant(tenant_id=str(context["tenant_id"]), project_id=value)
        except ValueError as exc:
            raise PlatformNotFoundError("Project was not found.") from exc
    return value


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, PlatformNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PlatformConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, PlatformValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Production line request failed.")


@router.get("")
def list_lines(
    project_id: str | None = Query(default=None, max_length=160),
    include_shared: bool = Query(default=False),
    context: dict[str, Any] = Depends(_READ),
) -> dict[str, Any]:
    try:
        project_id = _project(context, project_id or "") or None
        store = _engine(context).db
        items = store.list_lines(project_id=project_id)
        if include_shared and project_id:
            known = {str(item["id"]) for item in items}
            items.extend(item for item in store.list_lines(project_id="") if str(item["id"]) not in known)
        return {"items": items}
    except Exception as exc:
        raise _errors(exc) from exc


@router.get("/operations")
def list_operations(
    project_id: str = Query(..., max_length=160),
    context: dict[str, Any] = Depends(_READ),
) -> dict[str, Any]:
    """Return declared operation schemas for the generic line editor.

    Provider authority metadata is deliberately omitted. The dashboard needs
    only the input shape and description to build a line; execution still
    goes through the existing capability checked runner.
    """
    try:
        catalogue = _line_catalogue(context)
        project = _project(context, project_id)
        items = catalogue.operations_for_lines(_line_authority(), project_id=project)
        return {"items": items}
    except Exception as exc:
        raise _errors(exc) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
def create_line(payload: dict[str, Any] = Body(...), context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        project_id = _project(context, str(payload.get("project_id") or ""))
        catalogue, authority = _line_catalogue(context), _line_authority()
        definition = validate_definition(payload.get("definition"), operation_directory=catalogue,
                                        actor=authority, project_id=project_id)
        line_id = str(payload.get("id") or "").strip()
        name = str(payload.get("name") or "").strip()
        if not line_id or len(line_id) > 160 or not name or len(name) > 200:
            raise PlatformValidationError("A line id and name are required.")
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise PlatformValidationError("Line enabled must be a boolean.")
        return _engine(context).db.put_line(line_id, name=name, project_id=project_id,
            definition=definition, enabled=enabled, actor_id=str(context.get("user_id", "")),
            operation_directory=catalogue, operation_actor=authority, create_only=True)
    except Exception as exc:
        raise _errors(exc) from exc


@router.get("/runs")
def list_runs(
    line_id: str | None = Query(default=None, max_length=160),
    project_id: str | None = Query(default=None, max_length=160),
    limit: int = Query(default=100, ge=1, le=500),
    context: dict[str, Any] = Depends(_READ),
) -> dict[str, Any]:
    try:
        project_id = _project(context, project_id or "") or None
        return {"items": _engine(context).list_runs(line_id=line_id, project_id=project_id, limit=limit)}
    except Exception as exc:
        raise _errors(exc) from exc


@router.get("/runs/{run_id}")
def get_run(run_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return _engine(context).get_run(run_id)
    except Exception as exc:
        raise _errors(exc) from exc


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_run(run_id: str, context: dict[str, Any] = Depends(_ADMIN)) -> None:
    try:
        _engine(context).db.archive_run(run_id, actor_id=str(context.get("user_id", "")))
    except Exception as exc:
        raise _errors(exc) from exc


def _review_contract(engine: ProductionLineEngine, run_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]:
    from app.services.integration_host import contributions
    from app.services.operator_dispatch import discover_contract_operations

    run = engine.get_run(run_id)
    if run["status"] != "waiting" or not run["current_stage"]:
        raise PlatformConflictError("The operation review was already submitted or the run is no longer waiting.")
    stage_run = next((item for item in run["stages"] if item["stage_id"] == run["current_stage"]), {})
    waiting = stage_run.get("output", {}).get("waiting", {}) if isinstance(stage_run.get("output"), dict) else {}
    if waiting.get("kind") != "review":
        raise PlatformConflictError("Run is not waiting for an operation review.")
    stage = engine._stage(run_id, run["current_stage"])
    key = (str(stage.get("contract") or ""), str(stage.get("operation") or ""))
    declared = next((entry for contract, operation, entry in discover_contract_operations(contributions)
                     if (contract, operation) == key), {})
    review_items = declared.get("review_items")
    review_submit = declared.get("review_submit")
    if not isinstance(review_items, dict) or not isinstance(review_submit, dict):
        raise PlatformConflictError("This waiting operation has no dashboard review contract.")
    return run, stage_run, waiting, str(review_items.get("operation") or ""), str(review_submit.get("operation") or "")


@router.get("/runs/{run_id}/review/items")
def operation_review_items(run_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        from app.services.operator_dispatch import _contract_registry

        engine = _engine(context)
        run, stage_run, waiting, items_operation, _submit_operation = _review_contract(engine, run_id)
        stage = engine._stage(run_id, run["current_stage"])
        contract = str(stage["contract"])
        actor = engine._operation_actor(contract, items_operation, run_id)
        result = _contract_registry().invoke(contract, items_operation, {"run_id": str(waiting["ref"])},
                                             project_id=_project(context, run["project_id"]), actor=actor)
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            raise PlatformValidationError("Review operation returned an invalid items list.")
        from app.services.review_media import attach_review_media

        project_id = _project(context, run["project_id"])
        with_media = attach_review_media(
            items, tenant_id=str(context["tenant_id"]), project_id=project_id,
            actor=context, run_id=run_id,
        )
        sanitized = [
            {
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or item.get("name") or item.get("title") or "Review item"),
                "caption": str(item.get("caption") or item.get("description") or ""),
                "media_previews": item.get("media_previews") or [],
            }
            for item in with_media
        ]
        return {"items": sanitized}
    except Exception as exc:
        raise _errors(exc) from exc


@router.post("/runs/{run_id}/review/submit", status_code=status.HTTP_202_ACCEPTED)
def submit_operation_review(run_id: str, payload: dict[str, Any] = Body(...), context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        from app.services.operator_dispatch import _contract_registry

        engine = _engine(context)
        run, _stage_run, waiting, _items_operation, submit_operation = _review_contract(engine, run_id)
        stage = engine._stage(run_id, run["current_stage"])
        contract = str(stage["contract"])
        items = payload.get("items")
        if not isinstance(items, list):
            raise PlatformValidationError("Review decisions must be an items list.")
        actor = engine._operation_actor(contract, submit_operation, run_id)
        result = _contract_registry().invoke(contract, submit_operation,
            {"run_id": str(waiting["ref"]), "items": items},
            project_id=_project(context, run["project_id"]), actor=actor)
        continued = engine.continue_run(run_id, value={"review": result}, actor=context,
                                        actor_id=str(context.get("user_id") or ""))
        return {"result": result, "run": continued}
    except Exception as exc:
        raise _errors(exc) from exc


@router.post("/runs/{run_id}/actions", status_code=status.HTTP_202_ACCEPTED)
def run_action(run_id: str, payload: dict[str, Any] = Body(...), context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    """Invoke only the selected operation action declared by a run stage."""
    try:
        from app.services.integration_host import contributions
        from app.services.operator_dispatch import _contract_registry, discover_contract_operations

        engine = _engine(context)
        run = engine.get_run(run_id)
        project_id = _project(context, run["project_id"])
        stage_id = str(payload.get("stage_id") or "")
        stage = engine._stage(run_id, stage_id)
        if stage.get("kind") not in {"operation", "stepped_operation", "async_operation"}:
            raise PlatformValidationError("Run actions must belong to an operation stage.")
        source_contract, source_operation = str(stage.get("contract") or ""), str(stage.get("operation") or "")
        declarations = {(contract, operation): declared for contract, operation, declared in discover_contract_operations(contributions)}
        source = declarations.get((source_contract, source_operation))
        actions = source.get("run_actions", []) if isinstance(source, dict) else []
        index = payload.get("action_index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(actions):
            raise PlatformNotFoundError("Declared run action was not found.")
        action = actions[index]
        if not isinstance(action, dict):
            raise PlatformValidationError("Declared run action is invalid.")
        inputs = payload.get("inputs", {})
        if not isinstance(inputs, dict):
            raise PlatformValidationError("Run action inputs must be an object.")
        fields = action.get("inputs") if isinstance(action.get("inputs"), list) else []
        allowed = {str(field.get("name")) for field in fields if isinstance(field, dict) and field.get("name")}
        if set(inputs) - allowed:
            raise PlatformValidationError("Run action contains undeclared inputs.")
        missing = [str(field["name"]) for field in fields if isinstance(field, dict) and field.get("required")
                   and not str(inputs.get(str(field["name"])) or "").strip()]
        if missing:
            raise PlatformValidationError(f"Run action is missing required inputs: {', '.join(missing)}.")
        if action.get("needs_reason") and not str(inputs.get("reason") or "").strip():
            raise PlatformValidationError("A reason is required for this action.")
        target_operation = str(action.get("operation") or "").strip()
        target = next((declared for (contract, operation), declared in declarations.items()
                       if contract == source_contract and operation == target_operation), None)
        if target is None:
            raise PlatformNotFoundError("Run action operation is not declared.")
        template = action.get("args_template", {})
        if not isinstance(template, (dict, str)):
            raise PlatformValidationError("Run action args_template must be an object or a context reference.")
        resolved = _resolve(template, {**run["context"], "action": inputs})
        if not isinstance(resolved, dict):
            raise PlatformValidationError("Resolved run action arguments must be an object.")
        operation_actor = engine._operation_actor(source_contract, target_operation, run_id)
        result = _contract_registry().invoke(source_contract, target_operation, resolved,
                                             project_id=project_id, actor=operation_actor)
        engine._event(run_id, "run.action", stage_id=stage_id,
                      detail={"label": str(action.get("label") or ""), "operation": target_operation},
                      actor_id=str(context.get("user_id") or ""))
        return {"result": result}
    except Exception as exc:
        raise _errors(exc) from exc


@router.post("/{line_id}/runs", status_code=status.HTTP_202_ACCEPTED)
def start_run(line_id: str, payload: dict[str, Any] = Body(default={}), context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        variables = payload.get("vars", payload)
        return _engine(context).start(line_id, variables=variables, project_id=_project(context, str(payload.get("project_id") or "")), actor=context)
    except Exception as exc:
        raise _errors(exc) from exc


@router.post("/runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_stage(run_id: str, payload: dict[str, Any] = Body(...), context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _engine(context).retry_stage(run_id, stage_id=str(payload.get("stage_id") or ""), actor_id=str(context.get("user_id", "")))
    except Exception as exc:
        raise _errors(exc) from exc


@router.post("/runs/{run_id}/continue", status_code=status.HTTP_202_ACCEPTED)
def continue_run(run_id: str, payload: dict[str, Any] = Body(default={}), context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _engine(context).continue_run(run_id, value=payload.get("value"), actor=context, actor_id=str(context.get("user_id", "")))
    except Exception as exc:
        raise _errors(exc) from exc


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _engine(context).cancel(run_id, actor_id=str(context.get("user_id", "")))
    except Exception as exc:
        raise _errors(exc) from exc


@router.post("/runs/{run_id}/stages/{stage_id}/skip", status_code=status.HTTP_202_ACCEPTED)
def skip_stage(run_id: str, stage_id: str, payload: dict[str, Any] = Body(...), context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _engine(context).skip_stage(run_id, stage_id, reason=str(payload.get("reason") or ""), actor=context, actor_id=str(context.get("user_id", "")))
    except Exception as exc:
        raise _errors(exc) from exc


@router.patch("/{line_id}")
def update_line(line_id: str, payload: dict[str, Any] = Body(...), context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        current = _engine(context).db.get_line(line_id)
        project_id = _project(context, str(payload.get("project_id", current["project_id"])))
        catalogue, authority = _line_catalogue(context), _line_authority()
        definition = validate_definition(payload.get("definition", current["definition"]),
            operation_directory=catalogue, actor=authority, project_id=project_id)
        enabled = payload.get("enabled", current["enabled"])
        if not isinstance(enabled, bool):
            raise PlatformValidationError("Line enabled must be a boolean.")
        return _engine(context).db.put_line(line_id, name=str(payload.get("name", current["name"])),
            project_id=project_id, definition=definition,
            enabled=enabled, actor_id=str(context.get("user_id", "")),
            operation_directory=catalogue, operation_actor=authority)
    except Exception as exc:
        raise _errors(exc) from exc


@router.delete("/{line_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_line(line_id: str, context: dict[str, Any] = Depends(_ADMIN)) -> None:
    try:
        _engine(context).db.delete_line(line_id, actor_id=str(context.get("user_id", "")))
    except Exception as exc:
        raise _errors(exc) from exc


__all__ = ["router"]
