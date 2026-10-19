"""Tenant-scoped Codex call budgets and cached quota state."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.services.capability_context import current_capability_context
from app.services.integration_state import IntegrationStateService
from app.services.integration_runtime_settings import IntegrationRuntimeSettingsResolver
from app.services.platform_services import PlatformStore
from app.services.tenant_schema import DEFAULT_TENANT_ID


INTEGRATION_ID = "lynewright.integration.publishing-product-factory"
_DEFAULT_CAPS = {"text": 60, "images": 40}


class CodexUsageLimitError(RuntimeError):
    def __init__(self, message: str, *, failure_kind: str, retry_after: str = "") -> None:
        super().__init__(message)
        self.failure_kind = failure_kind
        self.retry_after = retry_after


def _tenant_id() -> str:
    context = current_capability_context()
    return str(context.tenant_id if context is not None else DEFAULT_TENANT_ID)


def _scope() -> tuple[str, str, str, str]:
    return _tenant_id(), INTEGRATION_ID, "", ""


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _runtime_caps(provider: str = "codex") -> dict[str, int]:
    caps = dict(_DEFAULT_CAPS)
    try:
        config = IntegrationRuntimeSettingsResolver().runtime_config(INTEGRATION_ID)
    except Exception:
        config = {}
    cap_keys = (
        (("text", "gemini_daily_text_calls"),) if provider == "gemini_cli" else
        (("text", "codex_daily_text_calls"), ("images", "codex_daily_images"))
    )
    for kind, key in cap_keys:
        try:
            caps[kind] = max(0, min(int(config.get(key, caps[kind])), 10000))
        except (TypeError, ValueError):
            pass
    return caps


def _read_json(db: sqlite3.Connection, key: str, default: Any) -> Any:
    row = db.execute(
        "SELECT value_json FROM platform_integration_kv WHERE tenant_id=? AND integration_id=? AND connection_id=? AND project_id=? AND key=?",
        (*_scope(), key),
    ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value_json"])
    except (TypeError, ValueError):
        return default


def _write_json(db: sqlite3.Connection, key: str, value: Any) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    db.execute(
        """INSERT INTO platform_integration_kv
        (tenant_id,integration_id,connection_id,project_id,key,value_json)
        VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id,integration_id,connection_id,project_id,key)
        DO UPDATE SET value_json=excluded.value_json,updated_at=CURRENT_TIMESTAMP""",
        (*_scope(), key, encoded),
    )


def quota_until() -> str:
    store = PlatformStore()
    with store.connection() as db:
        IntegrationStateService.ensure_schema(db)
        value = _read_json(db, "codex:quota_until", "")
    return _active_quota_until(value)


def _active_quota_until(value: Any) -> str:
    until = str(value or "").strip()
    if not until:
        return ""
    try:
        parsed = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return until if parsed > datetime.now(timezone.utc) else ""


def clear_quota_until() -> None:
    """Forget a cached quota date. A new login may be a different plan: the
    free account's "until Oct 18" must not block the Plus account after it."""
    store = PlatformStore()
    with store.connection() as db:
        IntegrationStateService.ensure_schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            _write_json(db, "codex:quota_until", "")
            db.commit()
        except Exception:
            db.rollback()
            raise


def cache_quota_until(value: str | None) -> None:
    until = str(value or "").strip()
    if not until:
        return
    try:
        parsed = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return
    normalized = parsed.astimezone(timezone.utc).isoformat()
    store = PlatformStore()
    with store.connection() as db:
        IntegrationStateService.ensure_schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            _write_json(db, "codex:quota_until", normalized)
            db.commit()
        except Exception:
            db.rollback()
            raise


def reserve_call(kind: str, idempotency_key: str, *, provider: str = "codex") -> None:
    """Reserve one provider call before dispatch; duplicate job submissions cost once."""
    if provider not in {"codex", "gemini_cli"}:
        raise ValueError("Unsupported provider usage bucket.")
    if kind not in ({"text"} if provider == "gemini_cli" else {"text", "images"}):
        raise ValueError("Codex usage kind must be text or images.")
    today = _today()
    count_field = "text_calls" if kind == "text" else "images"
    digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()[:40]
    seen_key = f"{provider}:seen:{today}:{kind}:{digest}"
    usage_key = f"{provider}:usage:{today}"
    caps = _runtime_caps(provider)
    store = PlatformStore()
    with store.connection() as db:
        IntegrationStateService.ensure_schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            if _read_json(db, seen_key, False):
                db.commit()
                return
            cached_until = _active_quota_until(_read_json(db, "codex:quota_until", "")) if provider == "codex" else ""
            if cached_until:
                db.rollback()
                raise CodexUsageLimitError(
                    f"Codex quota is cached until {cached_until}.",
                    failure_kind="quota", retry_after=cached_until,
                )
            usage = _read_json(db, usage_key, {})
            usage = usage if isinstance(usage, dict) else {}
            count = max(0, int(usage.get(count_field) or 0))
            cap = caps[kind]
            if count >= cap:
                db.rollback()
                raise CodexUsageLimitError(
                    f"{provider.replace('_', ' ').title()} daily {kind} call limit reached ({cap} per UTC day).",
                    failure_kind="daily_cap",
                )
            usage[count_field] = count + 1
            usage.setdefault("text_calls", 0)
            usage.setdefault("images", 0)
            _write_json(db, usage_key, usage)
            _write_json(db, seen_key, True)
            db.commit()
        except CodexUsageLimitError:
            raise
        except Exception:
            db.rollback()
            raise


def usage_status(provider: str = "codex") -> dict[str, Any]:
    if provider not in {"codex", "gemini_cli"}:
        raise ValueError("Unsupported provider usage bucket.")
    today = _today()
    store = PlatformStore()
    with store.connection() as db:
        IntegrationStateService.ensure_schema(db)
        value = _read_json(db, f"{provider}:usage:{today}", {})
    usage = value if isinstance(value, dict) else {}
    return {
        "date": today,
        "text_calls": max(0, int(usage.get("text_calls") or 0)),
        "images": max(0, int(usage.get("images") or 0)),
        "text_cap": _runtime_caps(provider)["text"],
        "image_cap": _runtime_caps(provider)["images"] if provider == "codex" else 0,
        "quota_until": quota_until() if provider == "codex" else "",
    }
