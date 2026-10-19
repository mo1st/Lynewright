"""Admin diagnostics and recovery controls for durable runtime work."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.security import require_admin, require_admin_token
from app.services.job_queue import DatabaseRuntimeJobQueue

router = APIRouter(prefix="/api/runtime-jobs", tags=["runtime-jobs"], dependencies=[Depends(require_admin_token)])


class SupervisorResetRequest(BaseModel):
    instance_group: str = ""
    generation_id: str = ""
    force_replacement: bool = False


@router.get("/health")
def runtime_job_health() -> dict:
    return DatabaseRuntimeJobQueue().health()


@router.post("/supervisor/reset")
def reset_runtime_supervisor(
    payload: SupervisorResetRequest | None = None,
    admin: dict = Depends(require_admin),
) -> dict:
    payload = payload or SupervisorResetRequest()
    try:
        return DatabaseRuntimeJobQueue().reset_terminal_supervisor_failure(
            instance_group=payload.instance_group,
            generation_id=payload.generation_id,
            force_replacement=payload.force_replacement,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


