"""Authenticated, generic integration management API."""
from __future__ import annotations

import json
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.security import require_tenant_context, require_tenant_role
from app.services import integration_host
from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_management import IntegrationManagementService
from app.services.integration_file import IntegrationFileService, IntegrationFileValidationError
from app.services.integration_drafts import IntegrationDraftService
from app.services.integration_operations import IntegrationOperationCatalog
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_runner import IntegrationRunnerService
from app.services.integration_install_policy import (
    InstallConfirmationRequired,
    InstallPolicyForbidden,
    current_install_policy,
)
from app.services.runner_runtime_config import generate_runtime_config
from app.services.resource_lifecycle_host import get_resource_lifecycle_coordinator
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformServiceError,
    PlatformValidationError,
)
from app.services.platform_capabilities import CapabilityActor
from app.services.tenant_schema import DEFAULT_TENANT_ID


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualIntegrationCreate(_Request):
    name: str = Field(min_length=1, max_length=120)
    runtime_kind: Literal["mcp", "openapi", "webhook"]
    endpoint_url: str = Field(min_length=1, max_length=2_000)
    document: dict[str, Any] | str | None = None
    credential_refs: list[str] = Field(default_factory=list, max_length=20)
    verification: dict[str, Any] | None = None
    event_type: str | None = Field(default=None, max_length=120)
    payload_mapping: dict[str, str] = Field(default_factory=dict, max_length=50)


class IntegrationDraftCreate(_Request):
    source_kind: Literal["openapi_url", "mcp_endpoint", "github_repo", "pasted_docs"]
    project_id: str = Field(min_length=1, max_length=100)
    url: str = Field(default="", max_length=2_000)
    text: str = Field(default="", max_length=200_000)
    credential_refs: list[str] = Field(default_factory=list, max_length=20)
    internal_endpoint_ref: str = Field(default="", max_length=64)


class IntegrationDraftUpdate(_Request):
    project_id: str = Field(min_length=1, max_length=100)
    draft_json: dict[str, Any]


class InstallConfirmation(_Request):
    integration_id: str = Field(min_length=1, max_length=160)
    summary_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class IntegrationDraftProject(_Request):
    project_id: str = Field(min_length=1, max_length=100)
    confirm: InstallConfirmation | None = None


class PackageInstallConfirmation(_Request):
    confirm: InstallConfirmation | None = None


class IntegrationRemovalRequest(_Request):
    confirm_name: str = Field(min_length=1, max_length=160)
    disable_using_lines: bool = False
    delete_exclusive_credentials: bool = False


class IntegrationDraftProbe(IntegrationDraftProject):
    operation_id: str = Field(min_length=1, max_length=200)
    args: dict[str, Any] = Field(default_factory=dict)


class ConnectionCreate(_Request):
    name: str = Field(min_length=1, max_length=120)
    endpoint_url: str = Field(default="", max_length=2_000)
    credential_refs: list[str] = Field(default_factory=list, max_length=20)
    project_id: str | None = Field(default=None, max_length=100)
    configuration: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = Field(default=None, max_length=100)
    enabled: bool = True
    origin_kind: Literal["public", "internal_endpoint"] = "public"
    internal_endpoint_ref: str = Field(default="", max_length=256)


class ConnectionPatch(_Request):
    endpoint_url: str | None = Field(default=None, max_length=2_000)
    credential_refs: list[str] | None = Field(default=None, max_length=20)
    project_id: str | None = Field(default=None, max_length=100)
    configuration: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class ToolApproval(_Request):
    tool_names: list[str] = Field(max_length=500)


class MCPToolProbe(_Request):
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=20)


class MCPContractMapping(_Request):
    mapping_id: str | None = Field(default=None, max_length=80)
    tool_name: str = Field(min_length=1, max_length=200)
    target_contract: str = Field(min_length=3, max_length=160)
    target_operation: str = Field(min_length=1, max_length=120)
    argument_mapping: dict[str, Any] = Field(default_factory=dict, max_length=20)
    output_mapping: dict[str, Any] = Field(default_factory=dict, max_length=20)
    enabled: bool = True
    write_enabled: bool = False


class MCPContractExecute(_Request):
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    confirm_destructive: bool = False


class OpenApiContractMapping(_Request):
    mapping_id: str | None = Field(default=None, max_length=80)
    operation_id: str = Field(min_length=1, max_length=120)
    target_contract: str = Field(min_length=3, max_length=160)
    target_operation: str = Field(min_length=1, max_length=120)
    argument_mapping: dict[str, Any] = Field(default_factory=dict, max_length=20)
    output_mapping: dict[str, Any] = Field(default_factory=dict, max_length=20)
    enabled: bool = True
    write_enabled: bool = False


class OpenApiContractExecute(_Request):
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class SettingsUpdate(_Request):
    values: dict[str, Any] = Field(default_factory=dict, max_length=50)


class OperationApproval(_Request):
    operation_ids: list[str] = Field(max_length=500)


class GenericOperationPrepare(_Request):
    project_id: str = Field(min_length=1, max_length=256)
    operation_id: str = Field(min_length=1, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)


class GenericOperationInvoke(_Request):
    project_id: str = Field(min_length=1, max_length=256)
    operation_id: str = Field(min_length=1, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)
    prepared_invocation_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class GenericExecutionRequest(_Request):
    project_id: str = Field(min_length=1, max_length=256)


class OpenApiResourceMapping(_Request):
    mapping_id: str | None = Field(default=None, max_length=80)
    resource_type: str = Field(min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=120)
    plural_label: str | None = Field(default=None, max_length=120)
    list_operation_id: str = Field(min_length=1, max_length=120)
    get_operation_id: str | None = Field(default=None, max_length=120)
    create_operation_id: str | None = Field(default=None, max_length=120)
    update_operation_id: str | None = Field(default=None, max_length=120)
    delete_operation_id: str | None = Field(default=None, max_length=120)
    get_external_id_target: str | None = Field(default=None, max_length=800)
    update_external_id_target: str | None = Field(default=None, max_length=800)
    delete_external_id_target: str | None = Field(default=None, max_length=800)
    items_path: str = Field(min_length=1, max_length=800)
    id_path: str = Field(min_length=1, max_length=800)
    name_path: str = Field(min_length=1, max_length=800)
    parent_id_path: str | None = Field(default=None, max_length=800)
    url_path: str | None = Field(default=None, max_length=800)
    status_path: str | None = Field(default=None, max_length=800)
    display_fields: list[str] = Field(default_factory=list, max_length=20)
    metadata_paths: dict[str, str] = Field(default_factory=dict, max_length=20)
    pagination: dict[str, Any] | None = None
    enabled: bool = True


class MCPResourceMapping(_Request):
    mapping_id: str | None = Field(default=None, max_length=80)
    resource_type: str = Field(min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=120)
    plural_label: str | None = Field(default=None, max_length=120)
    list_tool_name: str = Field(min_length=1, max_length=200)
    list_arguments: dict[str, Any] = Field(default_factory=dict, max_length=20)
    get_tool_name: str | None = Field(default=None, max_length=200)
    create_tool_name: str | None = Field(default=None, max_length=200)
    update_tool_name: str | None = Field(default=None, max_length=200)
    delete_tool_name: str | None = Field(default=None, max_length=200)
    get_external_id_target: str | None = Field(default=None, max_length=800)
    update_external_id_target: str | None = Field(default=None, max_length=800)
    delete_external_id_target: str | None = Field(default=None, max_length=800)
    items_path: str = Field(min_length=1, max_length=800)
    id_path: str = Field(min_length=1, max_length=800)
    name_path: str = Field(min_length=1, max_length=800)
    parent_id_path: str | None = Field(default=None, max_length=800)
    url_path: str | None = Field(default=None, max_length=800)
    status_path: str | None = Field(default=None, max_length=800)
    display_fields: list[str] = Field(default_factory=list, max_length=20)
    metadata_paths: dict[str, str] = Field(default_factory=dict, max_length=20)
    enabled: bool = True


class AuthorizationUpdate(_Request):
    permission_mode: Literal["restricted", "custom", "full"]
    granted_capabilities: list[str] = Field(default_factory=list, max_length=100)
    requested_capabilities: list[str] | None = Field(default=None, max_length=100)


class OpenApiExecute(_Request):
    operation_id: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    confirm_destructive: bool = False


class ResourceAction(_Request):
    action: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    confirm_destructive: bool = False
    expected_revision: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class BindingCreate(_Request):
    project_id: str | None = Field(default=None, max_length=256)
    resource_type: str = Field(min_length=1, max_length=256)
    resource_id: str = Field(min_length=1, max_length=256)
    role: str = Field(min_length=1, max_length=256)
    ownership_mode: Literal["managed", "linked", "observed"] = "linked"


class UnbindRequest(_Request):
    expected_revision: int = Field(ge=1)


class BindingRepair(_Request):
    reason: str = Field(min_length=1, max_length=2_000)


class SetupVerification(_Request):
    evidence: dict[str, Any] = Field(default_factory=dict)


class OAuthBegin(_Request):
    redirect_uri: str = Field(min_length=1, max_length=2_000)


class OAuthCallback(_Request):
    state: str = Field(min_length=1, max_length=512)
    code: str = Field(min_length=1, max_length=4_000)
    access_credential_ref: str = Field(min_length=1, max_length=256)
    refresh_credential_ref: str = Field(default="", max_length=256)


router = APIRouter(prefix="/api/integration-management", tags=["integration-management"])
_READ = require_tenant_context
_WRITE = require_tenant_role("editor")
_ADMIN = require_tenant_role("admin")


def _service(context: dict[str, Any]) -> IntegrationManagementService:
    tenant_id = str(context["tenant_id"])
    integrations = IntegrationRegistry(integration_host.store, tenant_id=tenant_id)
    connections = ConnectionProfileService(integration_host.store, tenant_id=tenant_id, registry=integrations)
    if tenant_id == DEFAULT_TENANT_ID:
        contributions = integration_host.contributions
        lifecycle = integration_host.runtime
    else:
        contributions = ContributionRegistry(integrations, connections=connections)
        from app.services.integration_worker_invocations import DatabaseContainerInvocationBridge
        contributions.container_bridge = DatabaseContainerInvocationBridge(integration_host.store)
        lifecycle = None
    return IntegrationManagementService(
        integrations,
        connections,
        contributions,
        lifecycle=lifecycle,
        resource_lifecycle=get_resource_lifecycle_coordinator(tenant_id),
        actor_id=str(context.get("user_id") or "admin"),
    )


def _operation_catalog(context: dict[str, Any]) -> IntegrationOperationCatalog:
    return IntegrationOperationCatalog(_service(context))


def _operation_actor(context: dict[str, Any]) -> CapabilityActor:
    role = str(context.get("role") or "viewer")
    if role in {"owner", "admin", "editor"}:
        return CapabilityActor(
            client_id=f"human:{context.get('user_id', 0)}",
            name="Lynewright dashboard/API user",
            client_type="human",
            permission_mode="full",
            granted_capabilities=frozenset(),
        )
    return CapabilityActor(
        client_id=f"human:{context.get('user_id', 0)}",
        name="Lynewright dashboard/API viewer",
        client_type="human",
        permission_mode="custom",
        granted_capabilities=frozenset({"system:read", "network:use"}),
    )


def _error(exc: PlatformServiceError) -> HTTPException:
    if isinstance(exc, InstallConfirmationRequired):
        return HTTPException(status_code=409, detail=exc.response())
    if isinstance(exc, InstallPolicyForbidden):
        return HTTPException(status_code=403, detail=exc.message)
    if isinstance(exc, PlatformNotFoundError):
        return HTTPException(status_code=404, detail=exc.message)
    if isinstance(exc, PlatformConflictError):
        return HTTPException(status_code=409, detail=exc.message)
    if isinstance(exc, PlatformOperationError):
        return HTTPException(status_code=409, detail=exc.message)
    if isinstance(exc, PlatformValidationError):
        return HTTPException(status_code=400, detail=exc.message)
    return HTTPException(status_code=500, detail="Integration management request failed.")


def _confirmation_header(request: Request) -> dict[str, Any] | None:
    raw = request.headers.get("x-lynewright-install-confirm", "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return InstallConfirmation.model_validate(parsed).model_dump()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="X-Lynewright-Install-Confirm must contain integration_id and a SHA-256 summary_hash.") from exc


@router.get("")
def list_integrations(context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        service = _service(context)
        return {"items": service.list_integrations(), "package_statuses": service.package_statuses(), "install_policy": current_install_policy()}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/manual", status_code=status.HTTP_201_CREATED)
async def create_manual(payload: ManualIntegrationCreate, request: Request, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        result = IntegrationFileService(_service(context)).import_manual(
            payload.model_dump(), artifact_bytes=await request.body(),
            confirm=_confirmation_header(request),
        )
        return result["integration"]
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_integration_file(request: Request, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        if content_type == "multipart/form-data":
            form = await request.form()
            try:
                upload = form.get("file")
                if upload is None or not hasattr(upload, "read"):
                    raise IntegrationFileValidationError(["file: multipart upload must include a file field."])
                raw = await upload.read(1_000_001)
            finally:
                await form.close()
        elif content_type in {"application/yaml", "application/x-yaml", "application/json", "text/plain", "text/yaml"} or content_type.startswith("text/"):
            raw = await request.body()
        else:
            raise HTTPException(status_code=415, detail="Use application/yaml, application/json, text, or multipart/form-data.")
        importer = IntegrationFileService(_service(context))
        return importer.import_value(importer.parse(raw), artifact_bytes=raw, confirm=_confirmation_header(request))
    except IntegrationFileValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc
    except PlatformValidationError as exc:
        raise HTTPException(status_code=422, detail={"errors": [exc.message]}) from exc
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/export")
def export_integration_file(
    integration_id: str,
    format: Literal["yaml", "json"] = Query(default="yaml"),
    context: dict[str, Any] = Depends(_ADMIN),
) -> Response:
    try:
        value = IntegrationFileService(_service(context)).export_value(integration_id)
        if format == "json":
            body = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
            media_type = "application/json"
            extension = "json"
        else:
            body = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
            media_type = "application/yaml"
            extension = "yaml"
        return Response(
            content=body,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{integration_id}.{extension}"'},
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


def _draft_service(context: dict[str, Any], project_id: str) -> IntegrationDraftService:
    return IntegrationDraftService(
        _service(context), project_id=project_id,
        actor_id=str(context.get("user_id") or "admin"),
    )


def _draft_error(exc: PlatformServiceError) -> HTTPException:
    if isinstance(exc, IntegrationFileValidationError):
        return HTTPException(status_code=422, detail={"errors": exc.errors})
    return _error(exc)


@router.post("/drafts", status_code=status.HTTP_201_CREATED)
def create_integration_draft(payload: IntegrationDraftCreate, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        return _draft_service(context, payload.project_id).create(
            source_kind=payload.source_kind, url=payload.url, text=payload.text,
            credential_refs=payload.credential_refs,
            internal_endpoint_ref=payload.internal_endpoint_ref,
        )
    except PlatformServiceError as exc:
        raise _draft_error(exc) from exc


@router.get("/drafts")
def list_integration_drafts(project_id: str = Query(min_length=1, max_length=100), context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        return {"items": _draft_service(context, project_id).list()}
    except PlatformServiceError as exc:
        raise _draft_error(exc) from exc


@router.get("/drafts/{draft_id}")
def get_integration_draft(draft_id: str, project_id: str = Query(min_length=1, max_length=100), context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        return _draft_service(context, project_id).get(draft_id)
    except PlatformServiceError as exc:
        raise _draft_error(exc) from exc


@router.put("/drafts/{draft_id}")
def update_integration_draft(draft_id: str, payload: IntegrationDraftUpdate, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        return _draft_service(context, payload.project_id).update(draft_id, payload.draft_json)
    except PlatformServiceError as exc:
        raise _draft_error(exc) from exc


@router.post("/drafts/{draft_id}/probe")
def probe_integration_draft(draft_id: str, payload: IntegrationDraftProbe, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        return _draft_service(context, payload.project_id).probe(
            draft_id, operation_id=payload.operation_id, arguments=payload.args,
        )
    except PlatformServiceError as exc:
        raise _draft_error(exc) from exc


@router.post("/drafts/{draft_id}/enable")
def enable_integration_draft(draft_id: str, payload: IntegrationDraftProject, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        return _draft_service(context, payload.project_id).enable(draft_id, confirm=payload.confirm.model_dump() if payload.confirm else None)
    except PlatformServiceError as exc:
        raise _draft_error(exc) from exc


@router.post("/drafts/{draft_id}/discard")
def discard_integration_draft(draft_id: str, payload: IntegrationDraftProject, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        return _draft_service(context, payload.project_id).discard(draft_id)
    except PlatformServiceError as exc:
        raise _draft_error(exc) from exc


@router.get("/drafts/{draft_id}/download")
def download_integration_draft(draft_id: str, project_id: str = Query(min_length=1, max_length=100), context: dict[str, Any] = Depends(_ADMIN)) -> Response:
    try:
        draft = _draft_service(context, project_id).get(draft_id)
        if draft["draft_kind"] != "cli_command":
            raise PlatformValidationError("Only CLI command drafts are downloaded as command.yaml.")
        return Response(
            content=yaml.safe_dump(draft["draft_json"], sort_keys=False, allow_unicode=True),
            media_type="application/yaml",
            headers={"Content-Disposition": f'attachment; filename="command-{draft_id}.yaml"'},
        )
    except PlatformServiceError as exc:
        raise _draft_error(exc) from exc


@router.get("/manual-capabilities")
def manual_capabilities(context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    return {"items": _service(context).manual_capability_options()}


@router.get("/operations")
def list_generic_operations(
    project_id: str = Query(min_length=1, max_length=256),
    query: str = Query(default="", max_length=200),
    context: dict[str, Any] = Depends(_READ),
) -> dict[str, Any]:
    try:
        actor = _operation_actor(context)
        return {"items": _operation_catalog(context).search(actor, project_id=project_id, query=query)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/operations/{operation_id}")
def describe_generic_operation(operation_id: str, project_id: str = Query(min_length=1, max_length=256), context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return _operation_catalog(context).describe(
            _operation_actor(context), project_id=project_id, operation_id=operation_id,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/operations/prepare", status_code=status.HTTP_201_CREATED)
def prepare_generic_operation(payload: GenericOperationPrepare, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _operation_catalog(context).prepare(
            _operation_actor(context), project_id=payload.project_id,
            operation_id=payload.operation_id, arguments=payload.arguments,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/operations/invoke")
def invoke_generic_operation(payload: GenericOperationInvoke, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        arguments = dict(payload.arguments)
        if payload.idempotency_key:
            arguments.setdefault("idempotency_key", payload.idempotency_key)
        return _operation_catalog(context).invoke(
            _operation_actor(context), project_id=payload.project_id,
            operation_id=payload.operation_id, arguments=arguments,
            prepared_invocation_id=payload.prepared_invocation_id,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/operations/executions/{execution_id}")
def get_generic_execution(execution_id: str, project_id: str = Query(min_length=1, max_length=256), context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return _operation_catalog(context).get_execution(
            _operation_actor(context), project_id=project_id, execution_id=execution_id,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/operations/executions/{execution_id}/cancel")
def cancel_generic_execution(execution_id: str, payload: GenericExecutionRequest, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _operation_catalog(context).cancel_execution(
            _operation_actor(context), project_id=payload.project_id, execution_id=execution_id,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}")
def get_integration(integration_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return _service(context).detail(integration_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/resources")
def list_resources(
    integration_id: str,
    connection_id: str | None = Query(default=None, max_length=256),
    resource_type: str | None = Query(default=None, max_length=120),
    search: str | None = Query(default=None, max_length=200),
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    context: dict[str, Any] = Depends(_READ),
) -> dict[str, Any]:
    try:
        return _service(context).resources(integration_id, connection_id=connection_id, resource_type=resource_type, search=search, cursor=cursor, limit=limit)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/resources/{inventory_id}/action")
def resource_action(
    integration_id: str,
    inventory_id: str,
    payload: ResourceAction,
    context: dict[str, Any] = Depends(_WRITE),
) -> dict[str, Any]:
    try:
        return _service(context).resource_action(
            integration_id,
            inventory_id,
            **payload.model_dump(),
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/connections")
def list_connections(integration_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return {"items": _service(context).connections_list(integration_id)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/bindings")
def list_bindings(integration_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return {"items": _service(context).bindings_list(integration_id)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/attention")
def list_attention(
    integration_id: str,
    limit: int = Query(default=200, ge=1, le=1_000),
    context: dict[str, Any] = Depends(_READ),
) -> dict[str, Any]:
    try:
        return {"items": _service(context).attention(integration_id, limit=limit)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/setup-requirements")
def list_setup_requirements(integration_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return {"items": _service(context).setup_requirements(integration_id)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/webhooks")
def list_webhooks(integration_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return {"items": _service(context).inbound_handlers(integration_id)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/connections/{connection_id}/oauth")
def oauth_status(integration_id: str, connection_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return _service(context).oauth_status(integration_id, connection_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/oauth/begin")
def oauth_begin(integration_id: str, connection_id: str, payload: OAuthBegin, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).oauth_begin(integration_id, connection_id, redirect_uri=payload.redirect_uri)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/oauth/callback")
def oauth_callback(integration_id: str, connection_id: str, payload: OAuthCallback, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).oauth_callback(integration_id, connection_id, **payload.model_dump())
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/oauth/refresh")
def oauth_refresh(integration_id: str, connection_id: str, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).oauth_refresh(integration_id, connection_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/reconcile")
def reconcile_integration(
    integration_id: str,
    max_items: int = Query(default=5, ge=1, le=100),
    context: dict[str, Any] = Depends(_ADMIN),
) -> dict[str, Any]:
    try:
        return _service(context).reconcile(integration_id, max_items=max_items)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections")
def create_connection(integration_id: str, payload: ConnectionCreate, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).create_connection(integration_id, **payload.model_dump())
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.patch("/{integration_id}/connections/{connection_id}")
def configure_connection(integration_id: str, connection_id: str, payload: ConnectionPatch, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).configure_connection(integration_id, connection_id, **{key: value for key, value in payload.model_dump().items() if value is not None})
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/enable")
def enable_connection(integration_id: str, connection_id: str, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).set_connection_enabled(integration_id, connection_id, enabled=True)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/disable")
def disable_connection(integration_id: str, connection_id: str, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).set_connection_enabled(integration_id, connection_id, enabled=False)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.delete("/{integration_id}/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_connection(integration_id: str, connection_id: str, context: dict[str, Any] = Depends(_WRITE)) -> None:
    try:
        _service(context).remove_connection(integration_id, connection_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/enable")
def enable_integration(integration_id: str, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).set_integration_enabled(integration_id, enabled=True)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/disable")
def disable_integration(integration_id: str, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).set_integration_enabled(integration_id, enabled=False)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/removal-preview")
def integration_removal_preview(integration_id: str, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    from app.services.integration_removal import IntegrationRemovalService

    try:
        return IntegrationRemovalService(_service(context)).preview(integration_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.delete("/{integration_id}")
def remove_integration(
    integration_id: str,
    payload: IntegrationRemovalRequest,
    context: dict[str, Any] = Depends(_ADMIN),
) -> dict[str, Any]:
    from app.services.integration_removal import IntegrationRemovalService

    try:
        return IntegrationRemovalService(_service(context)).remove(
            integration_id,
            confirm_name=payload.confirm_name,
            disable_using_lines=payload.disable_using_lines,
            delete_exclusive_credentials=payload.delete_exclusive_credentials,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/authorization")
def update_authorization(integration_id: str, payload: AuthorizationUpdate, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        return _service(context).set_authorization(integration_id, **payload.model_dump())
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/discover")
def discover(integration_id: str, connection_id: str, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).discover(integration_id, connection_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/resources/{resource_type}/action")
def create_openapi_resource(
    integration_id: str,
    connection_id: str,
    resource_type: str,
    payload: ResourceAction,
    context: dict[str, Any] = Depends(_WRITE),
) -> dict[str, Any]:
    try:
        if payload.action != "create":
            raise PlatformValidationError("This endpoint only supports the create resource action.")
        service = _service(context)
        integration = service.integrations.get(integration_id)
        if service._manual_kind(integration) == "openapi":
            return service.create_openapi_resource(
                integration_id, connection_id, resource_type, arguments=payload.arguments,
            )
        return service.create_resource(
            integration_id, connection_id, resource_type, arguments=payload.arguments,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/test")
def test_connection(integration_id: str, connection_id: str, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).test_connection(integration_id, connection_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/bindings/{binding_id}/repair")
def repair_binding(
    integration_id: str,
    binding_id: str,
    payload: BindingRepair,
    context: dict[str, Any] = Depends(_ADMIN),
) -> dict[str, Any]:
    try:
        return _service(context).repair_binding(
            integration_id,
            binding_id,
            operator_id=str(context.get("user_id") or "synthetic-admin"),
            reason=payload.reason,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/bindings/{binding_id}/setup/{requirement_id}/verify")
def verify_setup(
    integration_id: str,
    binding_id: str,
    requirement_id: str,
    payload: SetupVerification,
    context: dict[str, Any] = Depends(_ADMIN),
) -> dict[str, Any]:
    try:
        return _service(context).verify_setup(
            integration_id,
            binding_id,
            requirement_id,
            evidence=payload.evidence,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/mcp/approve")
def approve_mcp(integration_id: str, connection_id: str, payload: ToolApproval, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).approve_mcp_tools(integration_id, connection_id, payload.tool_names)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/mcp/tools/{tool_name}/probe")
def probe_mcp_tool(
    integration_id: str,
    connection_id: str,
    tool_name: str,
    payload: MCPToolProbe,
    context: dict[str, Any] = Depends(_WRITE),
) -> dict[str, Any]:
    try:
        return _service(context).probe_mcp_tool(integration_id, connection_id, tool_name, payload.arguments)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/connections/{connection_id}/mcp/tools/{tool_name}/schema")
def mcp_tool_input_schema(
    integration_id: str,
    connection_id: str,
    tool_name: str,
    context: dict[str, Any] = Depends(_WRITE),
) -> dict[str, Any]:
    try:
        return _service(context).mcp_tool_input_schema(integration_id, connection_id, tool_name)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/mcp/contract-targets")
def mcp_contract_targets(integration_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        _service(context).detail(integration_id)
        return {"items": _service(context).mcp_contract_targets()}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/connections/{connection_id}/mcp/contract-mappings")
def list_mcp_contract_mappings(integration_id: str, connection_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return {"items": _service(context).mcp_contract_mappings(integration_id, connection_id)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/mcp/contract-mappings", status_code=status.HTTP_201_CREATED)
def create_mcp_contract_mapping(integration_id: str, connection_id: str, payload: MCPContractMapping, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).create_mcp_contract_mapping(integration_id, connection_id, payload.model_dump(exclude_none=True))
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.patch("/{integration_id}/connections/{connection_id}/mcp/contract-mappings/{mapping_id}")
def update_mcp_contract_mapping(integration_id: str, connection_id: str, mapping_id: str, payload: MCPContractMapping, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).update_mcp_contract_mapping(integration_id, connection_id, mapping_id, payload.model_dump(exclude_none=True))
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.delete("/{integration_id}/connections/{connection_id}/mcp/contract-mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_contract_mapping(integration_id: str, connection_id: str, mapping_id: str, context: dict[str, Any] = Depends(_WRITE)) -> None:
    try:
        _service(context).delete_mcp_contract_mapping(integration_id, connection_id, mapping_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/mcp/contract-mappings/{mapping_id}/execute")
def execute_mcp_contract_mapping(integration_id: str, connection_id: str, mapping_id: str, payload: MCPContractExecute, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).execute_mcp_contract_mapping(
            integration_id,
            connection_id,
            mapping_id,
            payload.arguments,
            idempotency_key=payload.idempotency_key,
            confirm_destructive=payload.confirm_destructive,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/mcp/resource-mappings")
def list_mcp_resource_mappings(
    integration_id: str,
    connection_id: str = Query(..., max_length=100),
    context: dict[str, Any] = Depends(_READ),
) -> dict[str, Any]:
    try:
        return {"items": _service(context).mcp_resource_mappings(integration_id, connection_id)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/mcp/resource-mappings/preview")
def preview_mcp_resource_mapping(
    integration_id: str,
    payload: MCPResourceMapping,
    connection_id: str = Query(..., max_length=100),
    invoke: bool = Query(default=False),
    context: dict[str, Any] = Depends(_WRITE),
) -> dict[str, Any]:
    try:
        return _service(context).validate_mcp_resource_mapping(
            integration_id, connection_id, payload.model_dump(exclude_none=True), invoke=invoke,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/mcp/resource-mappings", status_code=status.HTTP_201_CREATED)
def create_mcp_resource_mapping(
    integration_id: str,
    payload: MCPResourceMapping,
    connection_id: str = Query(..., max_length=100),
    context: dict[str, Any] = Depends(_WRITE),
) -> dict[str, Any]:
    try:
        return _service(context).create_mcp_resource_mapping(
            integration_id, connection_id, payload.model_dump(exclude_none=True),
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.patch("/{integration_id}/mcp/resource-mappings/{mapping_id}")
def update_mcp_resource_mapping(
    integration_id: str,
    mapping_id: str,
    payload: MCPResourceMapping,
    connection_id: str = Query(..., max_length=100),
    context: dict[str, Any] = Depends(_WRITE),
) -> dict[str, Any]:
    try:
        return _service(context).update_mcp_resource_mapping(
            integration_id, connection_id, mapping_id, payload.model_dump(exclude_none=True),
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.delete("/{integration_id}/mcp/resource-mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_resource_mapping(
    integration_id: str,
    mapping_id: str,
    connection_id: str = Query(..., max_length=100),
    context: dict[str, Any] = Depends(_WRITE),
) -> None:
    try:
        _service(context).delete_mcp_resource_mapping(integration_id, connection_id, mapping_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/openapi/resource-mappings")
def list_openapi_resource_mappings(integration_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return {"items": _service(context).openapi_resource_mappings(integration_id)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/openapi/resource-mappings/preview")
def preview_openapi_resource_mapping(
    integration_id: str,
    payload: OpenApiResourceMapping,
    connection_id: str | None = Query(default=None, max_length=100),
    invoke: bool = Query(default=False),
    context: dict[str, Any] = Depends(_WRITE),
) -> dict[str, Any]:
    try:
        return _service(context).validate_openapi_resource_mapping(
            integration_id,
            payload.model_dump(exclude_none=True),
            connection_id=connection_id,
            invoke=invoke,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/openapi/resource-mappings", status_code=status.HTTP_201_CREATED)
def create_openapi_resource_mapping(integration_id: str, payload: OpenApiResourceMapping, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).create_openapi_resource_mapping(integration_id, payload.model_dump(exclude_none=True))
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.patch("/{integration_id}/openapi/resource-mappings/{mapping_id}")
def update_openapi_resource_mapping(integration_id: str, mapping_id: str, payload: OpenApiResourceMapping, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).update_openapi_resource_mapping(integration_id, mapping_id, payload.model_dump(exclude_none=True))
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.delete("/{integration_id}/openapi/resource-mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_openapi_resource_mapping(integration_id: str, mapping_id: str, context: dict[str, Any] = Depends(_WRITE)) -> None:
    try:
        _service(context).delete_openapi_resource_mapping(integration_id, mapping_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/connections/{connection_id}/openapi/contract-mappings")
def list_openapi_contract_mappings(integration_id: str, connection_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return {"items": _service(context).openapi_contract_mappings(integration_id, connection_id)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/openapi/contract-mappings", status_code=status.HTTP_201_CREATED)
def create_openapi_contract_mapping(integration_id: str, connection_id: str, payload: OpenApiContractMapping, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).create_openapi_contract_mapping(integration_id, connection_id, payload.model_dump(exclude_none=True))
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.patch("/{integration_id}/connections/{connection_id}/openapi/contract-mappings/{mapping_id}")
def update_openapi_contract_mapping(integration_id: str, connection_id: str, mapping_id: str, payload: OpenApiContractMapping, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).update_openapi_contract_mapping(integration_id, connection_id, mapping_id, payload.model_dump(exclude_none=True))
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.delete("/{integration_id}/connections/{connection_id}/openapi/contract-mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_openapi_contract_mapping(integration_id: str, connection_id: str, mapping_id: str, context: dict[str, Any] = Depends(_WRITE)) -> None:
    try:
        _service(context).delete_openapi_contract_mapping(integration_id, connection_id, mapping_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/openapi/contract-mappings/{mapping_id}/execute")
def execute_openapi_contract_mapping(integration_id: str, connection_id: str, mapping_id: str, payload: OpenApiContractExecute, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).execute_openapi_contract_mapping(integration_id, connection_id, mapping_id, payload.arguments, idempotency_key=payload.idempotency_key)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/openapi/operations")
def openapi_operations(integration_id: str, connection_id: str | None = Query(default=None, max_length=256), context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return {"items": _service(context).openapi_operations(integration_id, connection_id)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/openapi/approve")
def approve_openapi(integration_id: str, connection_id: str, payload: OperationApproval, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).approve_openapi_operations(integration_id, connection_id, payload.operation_ids)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/connections/{connection_id}/openapi/execute")
def execute_openapi(integration_id: str, connection_id: str, payload: OpenApiExecute, context: dict[str, Any] = Depends(_WRITE)) -> Any:
    try:
        return _service(context).execute_openapi(integration_id, connection_id, **payload.model_dump())
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/resources/{inventory_id}/bind", status_code=status.HTTP_201_CREATED)
def bind_resource(inventory_id: str, payload: BindingCreate, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).bind(inventory_id, **payload.model_dump())
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/bindings/{binding_id}/unbind")
def unbind_resource(binding_id: str, payload: UnbindRequest, context: dict[str, Any] = Depends(_WRITE)) -> dict[str, Any]:
    try:
        return _service(context).unbind(binding_id, expected_revision=payload.expected_revision)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/health")
def integration_health(integration_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return _service(context).health(integration_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/runner-token")
def create_runner_token(
    integration_id: str, response: Response, context: dict[str, Any] = Depends(_ADMIN),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        service = _service(context)
        integration = service.integrations.get(integration_id)
        if str(integration.get("source") or "") != "package":
            raise PlatformValidationError("Runner tokens are available only for native package integrations.")
        token = IntegrationRunnerService(service.store, tenant_id=service.tenant_id).create_or_rotate(integration_id)
        return {
            **token,
            "token_env_file_line": f"LYNEWRIGHT_RUNNER_TOKEN={token['token']}",
            "message": "Store this token once in the referenced runner env file; it cannot be shown again.",
        }
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.delete("/{integration_id}/runner-token")
def revoke_runner_token(integration_id: str, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        service = _service(context)
        revoked = IntegrationRunnerService(service.store, tenant_id=service.tenant_id).revoke(integration_id)
        return {"integration_id": integration_id, "revoked": revoked}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/confirm-install")
def confirm_package_install(
    integration_id: str,
    payload: PackageInstallConfirmation,
    context: dict[str, Any] = Depends(_ADMIN),
) -> dict[str, Any]:
    try:
        service = _service(context)
        return service.confirm_package_install(
            integration_id,
            confirm=payload.confirm.model_dump() if payload.confirm else None,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/runtime-config")
def integration_runtime_config(
    integration_id: str,
    format: Literal["compose", "quadlet"] = Query(default="compose"),
    context: dict[str, Any] = Depends(_ADMIN),
) -> dict[str, Any]:
    try:
        service = _service(context)
        integration = service.integrations.get(integration_id)
        from app.services.integration_provenance import get_provenance
        provenance = get_provenance(service.store, service.tenant_id, integration_id)
        if provenance and provenance.get("status") == "awaiting_confirmation":
            raise PlatformOperationError("This outside package is awaiting admin confirmation; runtime configuration is unavailable.")
        integration["tenant_id"] = service.tenant_id
        return generate_runtime_config(
            service.store, integration, format=format, tenant_id=service.tenant_id,
        )
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/diagnostics")
def integration_diagnostics(integration_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return _service(context).diagnostics(integration_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.get("/{integration_id}/settings")
def integration_settings(integration_id: str, context: dict[str, Any] = Depends(_READ)) -> dict[str, Any]:
    try:
        return _service(context).settings(integration_id)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.put("/{integration_id}/settings")
def update_integration_settings(integration_id: str, payload: SettingsUpdate, context: dict[str, Any] = Depends(_ADMIN)) -> dict[str, Any]:
    try:
        return _service(context).update_settings(integration_id, payload.values)
    except PlatformServiceError as exc:
        raise _error(exc) from exc


class DefinitionUpgradeApply(_Request):
    """Confirmation that the applied upgrade is the reviewed one.

    Both fields come from a preview. Requiring them means an apply cannot
    silently move the activation lock to a definition nobody looked at --
    the deployment could have changed between the two calls.
    """

    expected_definition_version: str = Field(min_length=1, max_length=64)
    expected_manifest_fingerprint: str = Field(min_length=8, max_length=128)


def _definition_upgrade_service(context: dict[str, Any]) -> Any:
    """Upgrades act on the host runtime, which is the only one holding definitions."""
    from app.services.integration_definition_upgrades import (
        IntegrationDefinitionUpgradeService,
    )

    if str(context["tenant_id"]) != DEFAULT_TENANT_ID:
        raise PlatformOperationError(
            "Definition upgrades are available only on the host runtime tenant."
        )
    return IntegrationDefinitionUpgradeService(integration_host.runtime)


def _candidate_definition(integration_id: str) -> Any:
    """The definition this deployment's code compiles for that integration.

    The candidate is never taken from the request. It is what the running
    release actually builds, so an upgrade can only ever move the lock to the
    code that is already deployed -- not to something a caller describes.
    """
    from app.services.production_integrations import compile_installed_definitions

    compiled = compile_installed_definitions(integration_host.runtime.integrations)
    for definition in compiled["definitions"]:
        if definition.integration_id == integration_id:
            return definition
    reason = compiled["diagnostics"].get(integration_id, "not_installed")
    raise PlatformNotFoundError(
        f"No runtime definition is available for {integration_id} ({reason})."
    )


@router.post("/{integration_id}/definition-upgrade/preview")
def preview_definition_upgrade(
    integration_id: str, context: dict[str, Any] = Depends(_ADMIN),
) -> dict[str, Any]:
    """Report what applying the deployed definition would change. Writes nothing.

    Two modes, chosen by what the process actually holds. When the activated
    definition is still registered the reversible upgrade applies. After an
    immutable release it is not, and the honest answer is a lock reconcile
    whose rollback is redeploying the previous release.
    """
    from dataclasses import asdict

    try:
        service = _definition_upgrade_service(context)
        candidate = _candidate_definition(integration_id)
        try:
            preview = service.preview(integration_id, candidate)
        except PlatformOperationError:
            return service.deployed_reconcile_preview(integration_id, candidate)
        return {"mode": "reversible_upgrade", **asdict(preview)}
    except PlatformServiceError as exc:
        raise _error(exc) from exc


@router.post("/{integration_id}/definition-upgrade/apply")
def apply_definition_upgrade(
    integration_id: str,
    payload: DefinitionUpgradeApply,
    context: dict[str, Any] = Depends(_ADMIN),
) -> dict[str, Any]:
    """Move the activation lock to the deployed definition, reversibly.

    The service deactivates, replaces the lock, reactivates, and restores the
    previous lock and activation if the new definition fails to activate.
    """
    try:
        service = _definition_upgrade_service(context)
        candidate = _candidate_definition(integration_id)
        # Derived the way the preview derives it. ``definition_identity`` only
        # knows definitions already registered, and after an immutable release
        # the freshly compiled candidate is not -- so it raised KeyError on
        # exactly the apply this endpoint exists for (seen on 0.5.2 -> 0.5.3).
        identity = integration_host.runtime.identities.identity_for(
            candidate.integration_id,
            definition_version=candidate.definition_version,
            manifest_fingerprint=candidate.manifest_fingerprint,
            contract_versions=candidate.contract_versions,
        )
        if (
            identity.definition_version != payload.expected_definition_version
            or identity.manifest_fingerprint != payload.expected_manifest_fingerprint
        ):
            raise PlatformConflictError(
                "The deployed definition no longer matches the reviewed preview."
            )
        try:
            return {"mode": "reversible_upgrade", **service.apply(integration_id, candidate)}
        except PlatformOperationError:
            return service.reconcile_to_deployed(integration_id, candidate)
    except PlatformServiceError as exc:
        raise _error(exc) from exc
