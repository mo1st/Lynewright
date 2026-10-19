"""Whether each AI task can actually run, readable by anyone signed in.

`/api/ai/providers` requires admin because it carries credentials, so an
editor-role operator could neither see that a key was missing nor act on the
advice the studio gave them — every fallback notice said "connect an AI provider
in Settings", a page they cannot open. They were told to fix something they had
no way to fix, and had no way to know it needed fixing before generating.

This endpoint answers only "will this run, and with what", and deliberately
carries no key material — not even a masked one.
"""

from __future__ import annotations
from contextlib import closing

from typing import Any

from fastapi import APIRouter, Depends

from app.database import get_connection, init_db
from app.security import require_user
from app.services.ai_provider import has_configured_api_key
from app.services.ai_task_provider import (
    TASK_TYPES,
    get_ai_settings_for_task,
    get_provider_for_task,
)

router = APIRouter(
    prefix="/api/ai/readiness",
    tags=["ai-readiness"],
    dependencies=[Depends(require_user)],
)


def _readiness(db, task_type: str) -> dict[str, Any]:
    route = get_provider_for_task(db, task_type)
    provider = str(route.get("provider") or "disabled")
    enabled = bool(route.get("enabled")) and provider not in {"disabled", "mock"}
    # Resolved rather than read from the row: a task can inherit the app-wide
    # provider, and `credential_mode='env_var'` means the key lives in the
    # environment and is absent from the database entirely.
    has_key = has_configured_api_key(get_ai_settings_for_task(task_type)) if enabled else False
    return {
        "task_type": task_type,
        "provider": provider,
        "model": str(route.get("model") or ""),
        "enabled": enabled,
        "key_configured": has_key,
        # The one thing the studio actually needs to decide whether to warn.
        "will_use_fallback": not (enabled and has_key),
    }


@router.get("")
def read_ai_readiness() -> dict:
    init_db()
    with closing(get_connection()) as db, db:
        return {"tasks": [_readiness(db, task_type) for task_type in TASK_TYPES]}
