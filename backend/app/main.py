"""Lynewright API composition root."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.database import init_db, purge_expired_sessions
from app.middleware.request_logging import RequestLoggingMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.routers import (
    ai_provider_settings, ai_readiness, application_logs, auth, catalogue, control_plane,
    health, integration_gateway, integration_management, interactions,
    integration_runner, mcp_oauth, media_library, media_links, operator, platform_mcp_clients,
    production_lines, review_images, runtime_jobs, settings, telegram_webhooks, tenants,
)
from app.security import _auth_enforced
from app.services.application_logging import log_event
from app.services.auth_bootstrap import bootstrap_admin_user, validate_production_admin_access
from app.services.integration_activation import activate_integrations


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    from app.services.catalogue import validate_catalogue_key_configuration
    validate_catalogue_key_configuration()
    settings = get_settings()
    init_db()
    purge_expired_sessions()
    bootstrap_admin_user()
    validate_production_admin_access()
    from app.services.catalogue import CatalogueService
    CatalogueService().startup_refresh()
    activation = activate_integrations(source="lynewright.startup", recover_enabled=True)
    from app.services.integration_provenance import backfill_pre_k52_provenance
    backfilled_provenance = backfill_pre_k52_provenance()
    if backfilled_provenance:
        log_event(
            level="info", category="integration", source="lynewright.startup",
            message="Backfilled provenance for pre-K5.2 integrations.",
            details={"integrations": backfilled_provenance},
        )
    if activation.get("recovery", {}).get("failed"):
        log_event(
            level="warning", category="integration", source="lynewright.startup",
            message="Some enabled integrations could not be restored.",
            details={"failed": activation["recovery"]["failed"]},
        )

    worker_stop = threading.Event()
    worker_task: asyncio.Task[None] | None = None
    automation_task: asyncio.Task[None] | None = None
    if settings.runtime_worker_enabled and settings.runtime_worker_mode != "disabled":
        from app.workers.runtime_job_worker import run as run_job_worker
        worker_task = asyncio.create_task(asyncio.to_thread(run_job_worker, worker_stop))
    if settings.operator_automation_enabled and not settings.disable_background_schedulers:
        from app.services.operator_automation import run_operator_automation_scheduler
        automation_task = asyncio.create_task(run_operator_automation_scheduler())
    try:
        yield
    finally:
        worker_stop.set()
        if automation_task is not None:
            automation_task.cancel()
            try:
                await automation_task
            except asyncio.CancelledError:
                pass
        if worker_task is not None:
            try:
                await asyncio.wait_for(worker_task, timeout=settings.runtime_worker_shutdown_timeout_seconds)
            except asyncio.TimeoutError:
                log_event(level="warning", category="runtime_worker", source="lynewright.shutdown",
                          message="The job worker did not stop before the shutdown timeout.")


settings_at_import = get_settings()
app = FastAPI(title="Lynewright API", version=settings_at_import.application_version, lifespan=lifespan)
use_wildcard_cors = "*" in settings_at_import.cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[] if use_wildcard_cors else settings_at_import.cors_origins,
    allow_origin_regex=".*" if use_wildcard_cors else None,
    allow_credentials=not use_wildcard_cors,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(RequestValidationError)
async def log_validation_error(request: Request, exc: RequestValidationError):
    request.state.failure_details = {
        "error_type": "RequestValidationError",
        "errors": json.loads(json.dumps(exc.errors(), default=str))[:20],
    }
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(StarletteHTTPException)
async def log_http_error(request: Request, exc: StarletteHTTPException):
    request.state.failure_details = {"status_code": exc.status_code, "detail": str(exc.detail)[:1000]}
    return await http_exception_handler(request, exc)


CORE_ROUTERS = (
    health, auth, tenants, operator, production_lines, integration_gateway,
    integration_management, interactions, mcp_oauth,
    platform_mcp_clients, application_logs, telegram_webhooks, control_plane,
    ai_provider_settings, ai_readiness, settings, runtime_jobs, review_images,
    media_library, media_links, catalogue, integration_runner,
)
for router_module in CORE_ROUTERS:
    app.include_router(router_module.router)


def load_plugins() -> list[str]:
    """Load optional modules from LYNEWRIGHT_PLUGINS and call register(app)."""
    modules = [item.strip() for item in os.getenv("LYNEWRIGHT_PLUGINS", "").split(",") if item.strip()]
    loaded: list[str] = []
    for module_name in modules:
        module = importlib.import_module(module_name)
        register = getattr(module, "register", None)
        if not callable(register):
            raise TypeError(f"Lynewright plugin {module_name!r} must define register(app)")
        register(app)
        loaded.append(module_name)
    return loaded


loaded_plugins = load_plugins()

__all__ = ["app", "load_plugins", "CORE_ROUTERS"]
