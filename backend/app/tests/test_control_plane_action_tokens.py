from __future__ import annotations

import sqlite3

import pytest

import app.services.control_plane_action_tokens as action_token_module
from app.services.control_plane_action_tokens import ControlPlaneActionTokenService
from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformStore, PlatformValidationError


def _service() -> ControlPlaneActionTokenService:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    return ControlPlaneActionTokenService(PlatformStore(lambda: db))


def test_action_token_is_opaque_scoped_and_one_time() -> None:
    service = _service()
    issued = service.issue(tenant_id="tenant-a", actor_id="user-a", project_id="project-a", surface_path="website.overview.analytics", component_id="analytics.summary", action="refresh", capability_id="analytics.web.summary")
    assert issued["token"].startswith("cpat_")
    claims = service.consume(issued["token"], tenant_id="tenant-a", actor_id="user-a", context={"project_id": "project-a", "action": "refresh"})
    assert claims["tenant_id"] == "tenant-a"
    with pytest.raises(PlatformConflictError):
        service.consume(issued["token"], tenant_id="tenant-a", actor_id="user-a")


def test_action_token_rejects_cross_tenant_and_invalid_ttl() -> None:
    service = _service()
    with pytest.raises(PlatformValidationError):
        service.issue(tenant_id="tenant-a", actor_id="user-a", surface_path="s", component_id="c", action="a", capability_id="cap", ttl_seconds=601)
    issued = service.issue(tenant_id="tenant-a", actor_id="user-a", surface_path="s", component_id="c", action="a", capability_id="cap")
    with pytest.raises(PlatformNotFoundError):
        service.consume(issued["token"], tenant_id="tenant-b")


def test_action_token_bounds_optional_scope_claims() -> None:
    service = _service()
    with pytest.raises(PlatformValidationError):
        service.issue(
            tenant_id="tenant-a",
            actor_id="user-a",
            surface_path="s",
            component_id="c",
            action="a",
            capability_id="cap",
            resource_id="r" * 257,
        )


def test_action_token_rejects_actor_and_context_mismatch_without_burning_token() -> None:
    service = _service()
    issued = service.issue(
        tenant_id="tenant-a", actor_id="user-a", project_id="project-a",
        resource_type="site", resource_id="site-a", surface_path="s",
        component_id="c", action="refresh", capability_id="cap",
    )
    with pytest.raises(PlatformConflictError):
        service.consume(issued["token"], tenant_id="tenant-a", actor_id="user-b")
    with pytest.raises(PlatformConflictError):
        service.consume(issued["token"], tenant_id="tenant-a", actor_id="user-a", context={"project_id": "project-b"})
    claims = service.consume(
        issued["token"], tenant_id="tenant-a", actor_id="user-a",
        context={"project_id": "project-a", "resource_id": "site-a"},
    )
    assert claims["resource_id"] == "site-a"


def test_action_token_expiry_is_enforced_on_peek_and_consume(monkeypatch) -> None:
    service = _service()
    issued = service.issue(tenant_id="tenant-a", actor_id="user-a", surface_path="s", component_id="c", action="a", capability_id="cap", ttl_seconds=1)
    monkeypatch.setattr(action_token_module.time, "time", lambda: issued["expires_at_epoch"] + 1)
    with pytest.raises(PlatformConflictError):
        service.peek(issued["token"], tenant_id="tenant-a")
    with pytest.raises(PlatformConflictError):
        service.consume(issued["token"], tenant_id="tenant-a", actor_id="user-a")


def test_purge_expired_tokens_is_bounded_and_tenant_scoped(monkeypatch) -> None:
    service = _service()
    issued_a = service.issue(tenant_id="tenant-a", actor_id="user-a", surface_path="s", component_id="c", action="a", capability_id="cap", ttl_seconds=1)
    issued_b = service.issue(tenant_id="tenant-b", actor_id="user-b", surface_path="s", component_id="c", action="a", capability_id="cap", ttl_seconds=1)
    monkeypatch.setattr(action_token_module.time, "time", lambda: max(issued_a["expires_at_epoch"], issued_b["expires_at_epoch"]) + 1)
    assert service.purge_expired(tenant_id="tenant-a", limit=1) == 1
    with pytest.raises(PlatformNotFoundError):
        service.peek(issued_a["token"], tenant_id="tenant-a")
    with pytest.raises(PlatformConflictError):
        service.peek(issued_b["token"], tenant_id="tenant-b")
    assert service.purge_expired(tenant_id="tenant-a", limit=1) == 0
    assert service.purge_expired(tenant_id="tenant-b", limit=1) == 1


def test_purge_expired_tokens_rejects_unbounded_limits() -> None:
    service = _service()
    with pytest.raises(PlatformValidationError):
        service.purge_expired(limit=5_001)
