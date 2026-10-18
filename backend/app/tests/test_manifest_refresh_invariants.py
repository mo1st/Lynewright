"""The seven invariants governing a manifest refresh.

A refresh rewrites what an integration *declares*. It must never rewrite what a
person *decided*. These are separated because the failure mode is quiet: a
deploy that silently widened authority, discarded a grant, or overwrote
configuration would look like a successful release.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.services.extension_registry import (
    EXTENSION_STATUS_ENABLED,
    EXTENSION_STATUS_INSTALLED,
    EXTENSION_STATUS_NEEDS_ATTENTION,
    ExtensionRegistry,
)
from app.services.platform_services import PlatformStore
from app.services.production_integrations import ensure_installed


_ID = "lynewright.integration.refresh-subject"


def _registry() -> ExtensionRegistry:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    return ExtensionRegistry(PlatformStore(lambda: db))


def _manifest(version: str, capabilities: list[str], provides: list[dict] | None = None) -> dict:
    body: dict = {
        "schema_version": "1.1" if provides else "1.0",
        "id": _ID, "name": "Refresh subject", "version": version, "type": "native",
        "lynewright_version": "0.1.0", "sdk_version": "1.0.0",
        "requested_capabilities": capabilities,
        "contributions": ["services"] if provides else [],
    }
    if provides:
        body["provides"] = provides
    return body


_WIDE = ["website:read", "media:read", "network:use"]
_NARROW = ["website:read", "network:use"]


def _installed_and_granted(capabilities: list[str] = _WIDE) -> ExtensionRegistry:
    registry = _registry()
    registry.register(_manifest("1.0.0", capabilities))
    registry.configure(_ID, {"region": "eu"})
    registry.set_authorization(_ID, permission_mode="restricted", granted_capabilities=capabilities)
    return registry


# 1. Effective grants are the intersection of stored grants and the manifest
# --------------------------------------------------------------------------

def test_effective_grants_are_stored_grants_intersected_with_the_manifest() -> None:
    registry = _installed_and_granted()
    ensure_installed(registry, _manifest("2.0.0", _NARROW))
    record = registry.get(_ID)

    assert set(record["effective_capabilities"]) == set(_NARROW)
    assert "media:read" not in record["effective_capabilities"]


# 2. A removed capability goes inactive without the grant being destroyed
# --------------------------------------------------------------------------

def test_a_dropped_capability_becomes_inactive_but_the_grant_survives() -> None:
    """Deleting the grant would make the operator re-authorize after a rollback."""
    registry = _installed_and_granted()
    ensure_installed(registry, _manifest("2.0.0", _NARROW))
    record = registry.get(_ID)

    assert record["inactive_capabilities"] == ["media:read"]
    assert "media:read" in record["granted_capabilities"]


# 3. enabled, configuration and grants are preserved
# --------------------------------------------------------------------------

def test_a_refresh_preserves_every_operator_decision() -> None:
    registry = _installed_and_granted()
    registry.enable(_ID)
    before = registry.get(_ID)

    ensure_installed(registry, _manifest("2.0.0", _WIDE))
    after = registry.get(_ID)

    assert after["enabled"] == before["enabled"] is True
    assert after["configuration"] == before["configuration"] == {"region": "eu"}
    assert after["granted_capabilities"] == before["granted_capabilities"]


# 4. Incompatible configuration becomes needs-attention, never rewritten
# --------------------------------------------------------------------------

def test_a_narrowing_refresh_asks_for_attention_rather_than_rewriting() -> None:
    registry = _installed_and_granted()
    ensure_installed(registry, _manifest("2.0.0", _NARROW))
    record = registry.get(_ID)

    assert record["status"] == EXTENSION_STATUS_NEEDS_ATTENTION
    assert "media:read" in record["last_error"]
    # Nothing was corrected on the operator's behalf.
    assert record["configuration"] == {"region": "eu"}
    assert "media:read" in record["granted_capabilities"]


# 5. Rolling a manifest back is allowed, and restores the stranded grant
# --------------------------------------------------------------------------

def test_a_manifest_can_roll_back_and_the_grant_takes_effect_again() -> None:
    """The reason inactive grants are kept rather than trimmed."""
    registry = _installed_and_granted()
    ensure_installed(registry, _manifest("2.0.0", _NARROW))
    assert registry.get(_ID)["inactive_capabilities"] == ["media:read"]

    # Older version, deliberately: refresh compares declarations, not versions.
    assert ensure_installed(registry, _manifest("1.0.0", _WIDE)) == "refreshed"
    restored = registry.get(_ID)

    assert restored["inactive_capabilities"] == []
    assert set(restored["effective_capabilities"]) == set(_WIDE)
    assert restored["status"] != EXTENSION_STATUS_NEEDS_ATTENTION


# 6. Runtime status is recomputed, not carried over
# --------------------------------------------------------------------------

def test_runtime_status_is_recomputed_from_the_new_declaration() -> None:
    """A stale "error" describes code the integration no longer claims to be."""
    registry = _installed_and_granted()
    registry.set_runtime_status(_ID, status="error", error="definition unavailable after restart")
    assert registry.get(_ID)["status"] == "error"

    ensure_installed(registry, _manifest("2.0.0", _WIDE))
    record = registry.get(_ID)

    assert record["status"] == EXTENSION_STATUS_INSTALLED
    assert record["last_error"] == ""


def test_recomputed_status_reflects_whether_the_operator_had_enabled_it() -> None:
    registry = _installed_and_granted()
    registry.enable(_ID)
    registry.set_runtime_status(_ID, status="error", error="stale")

    ensure_installed(registry, _manifest("2.0.0", _WIDE))
    assert registry.get(_ID)["status"] == EXTENSION_STATUS_ENABLED


# 7. Transactional, normalized, idempotent
# --------------------------------------------------------------------------

def test_an_equivalent_manifest_does_not_rewrite_the_row() -> None:
    """Both sides are normalized, or defaults alone would churn every restart."""
    registry = _installed_and_granted()
    assert ensure_installed(registry, _manifest("1.0.0", _WIDE)) == "present"
    assert ensure_installed(registry, _manifest("1.0.0", _WIDE)) == "present"


def test_refresh_settles_after_one_write() -> None:
    registry = _installed_and_granted()
    assert ensure_installed(registry, _manifest("2.0.0", _NARROW)) == "refreshed"
    assert ensure_installed(registry, _manifest("2.0.0", _NARROW)) == "present"
    assert ensure_installed(registry, _manifest("2.0.0", _NARROW)) == "present"


def test_an_invalid_manifest_leaves_the_record_untouched() -> None:
    """The write is one transaction; a rejected manifest changes nothing."""
    registry = _installed_and_granted()
    before = registry.get(_ID)

    with pytest.raises(Exception):
        registry.refresh_manifest({"id": _ID, "nonsense": True})

    after = registry.get(_ID)
    assert after["version"] == before["version"]
    assert after["manifest"] == before["manifest"]
    assert after["granted_capabilities"] == before["granted_capabilities"]
