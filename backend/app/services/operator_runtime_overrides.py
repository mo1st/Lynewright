"""Bounded runtime overrides for operator operational knobs.

Why this module exists
----------------------

``BOUNDED_SETTINGS`` gates what an agent may change through
``lynewright.settings.set``, which writes to the generic ``platform_settings``
key/value table. Until this module existed, registering keys there would have
been theatre: **no application code reads ``platform_settings`` by name.** The
real runtime knobs live in :mod:`app.config`, which is populated purely from
environment variables (49 ``os.getenv`` reads, zero database reads), so an
agent could have received a ``verified: true`` change plan for a value that
changed nothing.

This module closes that gap for a deliberately small set of operator
*operational* knobs: a stored ``platform_settings`` value overrides the
environment default at read time, so a bounded agent change actually takes
effect and is observable.

Design constraints honoured
---------------------------

* **No hot-path cost.** ``config.get_settings()`` is untouched — it is called
  from request middleware. Only the operator subsystem, which already talks to
  the database every tick, pays for the override lookup.
* **No recursion.** ``database.get_connection()`` does not depend on
  ``get_settings()`` (verified), so reading settings here is safe.
* **Bounded twice.** ``BOUNDED_SETTINGS`` validates before the write; this
  module clamps again on read, so a value written by any other route (an admin,
  a migration, a direct SQL edit) can still never push the runtime outside the
  declared envelope.
* **Cheap.** Values are cached for ``_TTL_SECONDS`` and the cache is dropped
  whenever a settings write lands, so a bounded change is visible on the next
  tick rather than after a restart.

Deliberately NOT overridable
----------------------------

``operator_automation_enabled`` and ``operator_dispatch_enabled`` stay
environment-only. An agent must not be able to switch its own execution or
oversight on or off; that is an operator-lifecycle decision, not a bounded
tuning knob.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from app.services.operator_settings_bounds import BOUNDED_SETTINGS

__all__ = [
    "OPERATOR_OVERRIDE_KEYS",
    "effective_int",
    "effective_float",
    "invalidate_override_cache",
]


# setting key -> (config attribute, coercer)
OPERATOR_OVERRIDE_KEYS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "platform.operator.automation_tick_seconds": ("operator_automation_tick_seconds", float),
    "platform.operator.objectives_per_tick": ("operator_objectives_per_tick", int),
    "platform.operator.interactions_per_tick": ("operator_interactions_per_tick", int),
    "platform.operator.max_tenants_per_tick": ("operator_max_tenants_per_tick", int),
    "platform.operator.remediation_max_attempts": ("operator_remediation_max_attempts", int),
}

_TTL_SECONDS = 5.0
_cache: dict[str, Any] = {}
_cache_stamp: float = 0.0


def invalidate_override_cache() -> None:
    """Drop cached overrides so the next read reflects a just-written value."""
    global _cache_stamp
    _cache.clear()
    _cache_stamp = 0.0


def _stored_overrides() -> dict[str, Any]:
    global _cache_stamp
    now = time.monotonic()
    if _cache and (now - _cache_stamp) < _TTL_SECONDS:
        return _cache
    values: dict[str, Any] = {}
    try:
        from app.database import get_connection

        with get_connection() as db:
            placeholders = ",".join("?" for _ in OPERATOR_OVERRIDE_KEYS)
            rows = db.execute(
                f"SELECT key,value_json FROM platform_settings WHERE key IN ({placeholders})",
                tuple(OPERATOR_OVERRIDE_KEYS),
            ).fetchall()
        for row in rows:
            try:
                values[str(row["key"])] = json.loads(row["value_json"])
            except (TypeError, ValueError, KeyError, IndexError):
                continue
    except Exception:
        # A settings read must never break the scheduler. Fall back to env.
        return {}
    _cache.clear()
    _cache.update(values)
    _cache_stamp = now
    return _cache


def _effective(key: str, fallback: Any) -> Any:
    """Stored override if present and inside its declared bounds, else fallback."""
    if key not in OPERATOR_OVERRIDE_KEYS:
        return fallback
    raw = _stored_overrides().get(key)
    if raw is None:
        return fallback
    ok, _reason = BOUNDED_SETTINGS.validate_value(key, raw)
    if not ok:
        # Clamped on read: a value that escaped the write-time gate by any
        # other route still cannot move the runtime outside the envelope.
        return fallback
    return raw


def effective_int(key: str, fallback: int) -> int:
    try:
        return int(_effective(key, fallback))
    except (TypeError, ValueError):
        return int(fallback)


def effective_float(key: str, fallback: float) -> float:
    try:
        return float(_effective(key, fallback))
    except (TypeError, ValueError):
        return float(fallback)
