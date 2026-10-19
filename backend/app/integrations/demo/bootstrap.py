"""Seed the key-free demo through normal integration and line services."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from app.services.platform_services import PlatformNotFoundError, ProjectService
from app.services.tenant_schema import DEFAULT_TENANT_ID


DEMO_LINE_ID = "demo-write-review-format-notify"
DEMO_FOREACH_LINE_ID = "demo-list-for-each-review-notify"
DEMO_WRITER_ID = "lynewright.integration.demo-writer"
DEMO_FORMATTER_ID = "lynewright.integration.demo-formatter"
DEMO_IMAGER_ID = "lynewright.integration.demo-imager"
DEMO_INSPECTOR_ID = "lynewright.integration.demo-inspector"
DEMO_MEDIA_LINE_ID = "demo-media-review-link-upload-notify"
DEMO_MEDIA_PROJECT_ID = "lynewright-demo-media"


def demo_line_definition() -> dict[str, Any]:
    return {
        "stages": [
            {
                "id": "write",
                "kind": "operation",
                "contract": "lynewright.demo.writer",
                "operation": "write",
                "args_template": {"topic": "{{run.vars.topic}}"},
            },
            {
                "id": "review",
                "kind": "gate:review",
                "items_template": [{
                    "id": "draft",
                    "label": "Draft note",
                    "caption": "{{stages.write.output.text}}",
                }],
            },
            {
                "id": "format",
                "kind": "async_operation",
                "contract": "lynewright.demo.formatter",
                "operation": "format",
                "args_template": {
                    "topic": "{{run.vars.topic}}",
                    "text": "{{stages.write.output.text}}",
                },
                "status_operation": {
                    "contract": "lynewright.demo.formatter",
                    "operation": "status",
                },
                "status_args_template": {"job_id": "{{stages.format.output.job_id}}"},
                "async_ref_path": "job_id",
                "status_ref_field": "job_id",
            },
            {
                "id": "notify",
                "kind": "notify",
                "title": "Demo line complete",
                "message_template": "{{stages.format.output.formatted}}",
            },
        ],
    }


def demo_media_line_definition() -> dict[str, Any]:
    return {
        "stages": [
            {
                "id": "imager", "kind": "operation",
                "contract": "lynewright.demo.imager", "operation": "generate",
                "args_template": {},
            },
            {
                "id": "review", "kind": "gate:review",
                "items_template": [{
                    "id": "demo-image", "label": "Generated PNG",
                    "caption": "The next stages will inspect this file by link and by upload.",
                    "media_refs": "{{stages.imager.output.media_refs}}",
                }],
            },
            {
                "id": "inspect_link", "kind": "operation",
                "contract": "lynewright.demo.inspector", "operation": "inspect_link",
                "args_template": {"media_url": "{{stages.imager.output.media_refs.0.url}}"},
            },
            {
                "id": "inspect_upload", "kind": "operation",
                "contract": "lynewright.demo.inspector", "operation": "inspect_upload",
                "args_template": {"media_file": "{{stages.imager.output.media_refs.0}}"},
            },
            {
                "id": "notify", "kind": "notify", "title": "Demo media inspection complete",
                "message_template": "PNG SHA-256: {{stages.inspect_upload.output.sha256}}",
            },
        ],
    }


def demo_foreach_line_definition() -> dict[str, Any]:
    return {
        "stages": [
            {
                "id": "list",
                "kind": "operation",
                "contract": "lynewright.demo.writer",
                "operation": "list",
                "args_template": {},
            },
            {
                "id": "each",
                "kind": "for_each",
                "items": "{{stages.list.output.items}}",
                "contract": "lynewright.demo.writer",
                "operation": "process",
                "input": {"value": "{{item.value}}"},
                "concurrency": 1,
                "on_item_error": "continue",
            },
            {
                "id": "review",
                "kind": "gate:review",
                "items_template": "{{stages.each.items}}",
            },
            {
                "id": "notify",
                "kind": "notify",
                "message_template": "Completed {{stages.each.output.succeeded}} items.",
            },
        ],
    }


def seed_demo() -> None:
    """Enable both packages and create the line once per tenant database."""
    if os.getenv("LYNEWRIGHT_DEMO", "").strip() != "1":
        return
    from app.services.integration_host import runtime
    from app.services.line_engine import ProductionLineEngine

    projects = ProjectService(tenant_id=DEFAULT_TENANT_ID)
    try:
        projects.get(DEMO_MEDIA_PROJECT_ID)
    except PlatformNotFoundError:
        projects.create({"project_id": DEMO_MEDIA_PROJECT_ID, "name": "Lynewright media demo"})

    for integration_id in (DEMO_WRITER_ID, DEMO_FORMATTER_ID, DEMO_IMAGER_ID, DEMO_INSPECTOR_ID):
        record = runtime.integrations.get(integration_id)
        # The optional demo's image producer and inspector need only the
        # capabilities they declared in their bundled manifests.  Grant those
        # in the explicitly demo-enabled bootstrap, never in normal startup.
        if integration_id in {DEMO_IMAGER_ID, DEMO_INSPECTOR_ID}:
            requested = [str(value) for value in record.get("requested_capabilities") or []]
            if set(record.get("granted_capabilities") or []) != set(requested):
                runtime.integrations.set_authorization(
                    integration_id, permission_mode="custom", granted_capabilities=requested,
                )
        if runtime.is_active(integration_id):
            continue
        if record.get("enabled"):
            runtime.activate(integration_id)
        else:
            runtime.enable(integration_id)

    engine = ProductionLineEngine(tenant_id=DEFAULT_TENANT_ID)
    try:
        engine.db.get_line(DEMO_LINE_ID)
    except PlatformNotFoundError:
        engine.db.put_line(
            DEMO_LINE_ID,
            name="Demo: write → review → format → notify",
            project_id="",
            definition=demo_line_definition(),
            enabled=True,
            actor_id="system",
        )
    try:
        engine.db.get_line(DEMO_MEDIA_LINE_ID)
    except PlatformNotFoundError:
        engine.db.put_line(
            DEMO_MEDIA_LINE_ID,
            name="Demo: image → review → inspect link → inspect upload → notify",
            project_id=DEMO_MEDIA_PROJECT_ID,
            definition=demo_media_line_definition(),
            enabled=True,
            actor_id="system",
        )
    try:
        engine.db.get_line(DEMO_FOREACH_LINE_ID)
    except PlatformNotFoundError:
        engine.db.put_line(
            DEMO_FOREACH_LINE_ID,
            name="Demo: list → for each → review → notify",
            project_id="",
            definition=demo_foreach_line_definition(),
            enabled=True,
            actor_id="system",
        )


def register(app: Any) -> None:
    """Install this optional behavior via the generic LYNEWRIGHT_PLUGINS hook."""
    if getattr(app.state, "demo_lifecycle_registered", False):
        return
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def demo_lifespan(application: Any):
        async with original_lifespan(application):
            if os.getenv("LYNEWRIGHT_DEMO", "").strip() == "1":
                seed_demo()
            yield

    app.router.lifespan_context = demo_lifespan
    app.state.demo_lifecycle_registered = True
