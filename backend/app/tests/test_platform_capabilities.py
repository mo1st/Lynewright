from __future__ import annotations

import sqlite3

import pytest

from app.services.platform_capabilities import (
    FULL_ACCESS_CAPABILITIES,
    CapabilityActor,
    ClientAuthorizationService,
    PlatformOperationError,
    PlatformValidationError,
    allowed_capabilities,
    require_capability,
)
from app.services.platform_services import PlatformStore


@pytest.fixture()
def clients() -> ClientAuthorizationService:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    service = ClientAuthorizationService(PlatformStore(lambda: connection))
    yield service
    connection.close()


def test_unauthenticated_and_ungranted_clients_are_rejected(clients: ClientAuthorizationService) -> None:
    manager = CapabilityActor.platform_manager()
    with pytest.raises(PlatformOperationError):
        clients.authenticate("not-a-client-token")
    client = clients.create_client({"name": "Reader", "permission_mode": "restricted"}, actor=manager)
    actor = clients.authenticate(client["access_token"])
    with pytest.raises(PlatformOperationError):
        require_capability(actor, "projects:read")


def test_custom_grant_and_revoke_persist(clients: ClientAuthorizationService) -> None:
    manager = CapabilityActor.platform_manager()
    client = clients.create_client({"name": "Codex", "permission_mode": "custom", "granted_capabilities": ["projects:read", "jobs:create"]}, actor=manager)
    actor = clients.authenticate(client["access_token"])
    require_capability(actor, "projects:read")
    assert "jobs:create" in allowed_capabilities(actor)

    updated = clients.update_client(client["id"], {"permission_mode": "custom", "granted_capabilities": ["jobs:create"]}, actor=manager)
    assert updated["granted_capabilities"] == ["jobs:create"]
    actor_after_restart = clients.authenticate(client["access_token"])
    with pytest.raises(PlatformOperationError):
        require_capability(actor_after_restart, "projects:read")
    require_capability(actor_after_restart, "jobs:create")


def test_full_access_allows_public_and_denies_reserved(clients: ClientAuthorizationService) -> None:
    manager = CapabilityActor.platform_manager()
    client = clients.create_client({"name": "Full", "permission_mode": "full"}, actor=manager)
    actor = clients.authenticate(client["access_token"])
    assert allowed_capabilities(actor) == FULL_ACCESS_CAPABILITIES
    require_capability(actor, "credentials:use")
    with pytest.raises(PlatformOperationError):
        require_capability(actor, "core-db:read")
    with pytest.raises(PlatformValidationError):
        clients.create_client({"name": "Bad Full", "permission_mode": "full", "granted_capabilities": ["projects:read"]}, actor=manager)


def test_clients_cannot_self_grant_or_manage_other_clients(clients: ClientAuthorizationService) -> None:
    manager = CapabilityActor.platform_manager()
    first = clients.create_client({"name": "First", "permission_mode": "custom", "granted_capabilities": ["projects:read"]}, actor=manager)
    second = clients.create_client({"name": "Second", "permission_mode": "restricted"}, actor=manager)
    first_actor = clients.authenticate(first["access_token"])
    with pytest.raises(PlatformOperationError):
        clients.update_client(first["id"], {"permission_mode": "full"}, actor=first_actor)
    with pytest.raises(PlatformOperationError):
        clients.update_client(second["id"], {"permission_mode": "full"}, actor=first_actor)


def test_client_records_never_return_token_hash_or_secret(clients: ClientAuthorizationService) -> None:
    manager = CapabilityActor.platform_manager()
    client = clients.create_client({"name": "No Leak", "permission_mode": "restricted"}, actor=manager)
    stored = clients.get_client(client["id"], actor=manager)
    assert "access_token" not in stored
    assert "token_hash" not in stored
    assert client["access_token"].startswith("qxc_")


def test_disabled_client_token_cannot_be_reused(clients: ClientAuthorizationService) -> None:
    manager = CapabilityActor.platform_manager()
    client = clients.create_client(
        {"name": "Revoke me", "permission_mode": "restricted", "granted_capabilities": ["system:read"]},
        actor=manager,
    )
    actor = clients.authenticate(client["access_token"])
    clients.update_client(client["id"], {"enabled": False}, actor=manager)

    with pytest.raises(PlatformOperationError):
        clients.authenticate(client["access_token"])
    with pytest.raises(PlatformOperationError):
        clients.resolve_actor(actor.client_id)
