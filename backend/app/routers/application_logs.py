from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.models.application_log import ApplicationLogList, FrontendLogRequest
from app.security import require_admin_token, require_tenant_context
from app.services.application_logging import clear_logs, list_logs, log_event


router = APIRouter(
    prefix="/api/logs", tags=["application-logs"],
    dependencies=[Depends(require_admin_token)],
)


@router.get("", response_model=ApplicationLogList)
def read_logs(
    level: str | None = Query(default=None), category: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0),
    project_id: str | None = Query(default=None, max_length=100),
    context: dict = Depends(require_tenant_context),
) -> dict:
    return list_logs(
        level=level, category=category, search=search, limit=limit, offset=offset,
        tenant_id=context["tenant_id"], project_id=project_id,
    )


@router.post("/frontend", status_code=202)
def receive_frontend_log(
    payload: FrontendLogRequest,
    context: dict = Depends(require_tenant_context),
) -> dict:
    log_event(
        level=payload.level, category=payload.category, source="frontend",
        message=payload.message, details=payload.details, request_path=payload.request_path,
        # The browser's own correlation id wins over this report's request id:
        # the failure being described happened on the other request.
        correlation_id=payload.correlation_id or str(payload.details.get("correlation_id") or ""),
        tenant_id=context["tenant_id"],
        # A browser report is data, not an in-flight server failure; an ambient
        # exception here would belong to whatever the log write is doing.
        capture_active_exception=False,
    )
    return {"accepted": True}


@router.delete("")
def delete_logs(context: dict = Depends(require_tenant_context)) -> dict:
    return {"deleted": clear_logs(tenant_id=context["tenant_id"])}

