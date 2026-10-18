from __future__ import annotations

import sqlite3

import pytest

from app.services.extension_capabilities import ExtensionAuthorizationService
from app.services.extension_registry import ExtensionRegistry
from app.services.platform_capabilities import (
    CapabilityActor,
    ClientAuthorizationService,
    allowed_capabilities,
    require_capability,
)
from app.services.platform_services import PlatformOperationError, PlatformStore, PlatformValidationError


def manifest(extension_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "id": extension_id,
        "name": extension_id,
        "version": "0.1.0",
        "type": "native",
        "lynewright_version": "^0.1.0",
        "sdk_version": "^1.0.0",
        "requested_capabilities": ["jobs:create", "jobs:read"],
        "backend": "dist/backend.mjs",
    }


@pytest.fixture()
def services() -> tuple[ExtensionRegistry, ExtensionAuthorizationService, ClientAuthorizationService]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    registry = ExtensionRegistry(store)
    registry.register(manifest("lynewright.alpha"))
    registry.register(manifest("lynewright.beta"))
    registry.enable("lynewright.alpha")
    registry.enable("lynewright.beta")
    yield registry, ExtensionAuthorizationService(registry), ClientAuthorizationService(store)
    connection.close()


def client(service: ClientAuthorizationService, *, name: str, mode: str, grants: list[str] | None = None) -> CapabilityActor:
    provisioned = service.create_client(
        {"name": name, "permission_mode": mode, "granted_capabilities": grants or []},
        actor=CapabilityActor.platform_manager(),
    )
    return service.authenticate(provisioned["access_token"])


def test_requested_capabilities_are_not_granted_until_owner_approves(services: tuple[ExtensionRegistry, ExtensionAuthorizationService, ClientAuthorizationService]) -> None:
    _, authorization, clients = services
    caller = client(clients, name="Codex", mode="full")

    with pytest.raises(PlatformOperationError):
        authorization.require_effective_capability(caller, "lynewright.alpha", "jobs:create")

    approved = authorization.set_permissions(
        "lynewright.alpha",
        permission_mode="custom",
        granted_capabilities=["jobs:create"],
        actor=CapabilityActor.platform_manager(),
    )
    assert approved["granted_capabilities"] == ["jobs:create"]
    authorization.require_effective_capability(caller, "lynewright.alpha", "jobs:create")

    authorization.set_permissions(
        "lynewright.alpha",
        permission_mode="custom",
        granted_capabilities=[],
        actor=CapabilityActor.platform_manager(),
    )
    with pytest.raises(PlatformOperationError):
        authorization.require_effective_capability(caller, "lynewright.alpha", "jobs:create")


def test_full_extension_access_includes_public_capabilities_but_not_reserved(services: tuple[ExtensionRegistry, ExtensionAuthorizationService, ClientAuthorizationService]) -> None:
    _, authorization, clients = services
    authorization.set_permissions(
        "lynewright.alpha", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager()
    )
    extension = authorization.resolve_actor("lynewright.alpha")
    assert "jobs:create" in allowed_capabilities(extension)  # type: ignore[arg-type]
    require_capability(extension, "jobs:create")  # type: ignore[arg-type]
    with pytest.raises(PlatformOperationError):
        require_capability(extension, "core-db:read")  # type: ignore[arg-type]

    caller = client(clients, name="Full caller", mode="full")
    with pytest.raises(PlatformOperationError):
        authorization.require_effective_capability(caller, "lynewright.alpha", "core-db:read")


def test_extensions_cannot_self_grant_or_change_another_extension(services: tuple[ExtensionRegistry, ExtensionAuthorizationService, ClientAuthorizationService]) -> None:
    _, authorization, _ = services
    alpha = authorization.resolve_actor("lynewright.alpha")

    with pytest.raises(PlatformOperationError, match="platform administrator"):
        authorization.set_permissions(
            "lynewright.alpha", permission_mode="full", granted_capabilities=[], actor=alpha
        )
    with pytest.raises(PlatformOperationError, match="platform administrator"):
        authorization.set_permissions(
            "lynewright.beta", permission_mode="full", granted_capabilities=[], actor=alpha
        )


def test_grants_survive_registry_restart_and_stay_with_extension_identity(services: tuple[ExtensionRegistry, ExtensionAuthorizationService, ClientAuthorizationService]) -> None:
    registry, authorization, _ = services
    authorization.set_permissions(
        "lynewright.alpha",
        permission_mode="custom",
        granted_capabilities=["jobs:read"],
        actor=CapabilityActor.platform_manager(),
    )

    after_restart = ExtensionAuthorizationService(ExtensionRegistry(registry.store))
    extension = after_restart.resolve_actor("lynewright.alpha")
    assert extension.extension_id == "lynewright.alpha"
    assert extension.client_id == "extension:lynewright.alpha"
    assert extension.granted_capabilities == {"jobs:read"}


def test_caller_and_extension_restrictions_are_both_mandatory(services: tuple[ExtensionRegistry, ExtensionAuthorizationService, ClientAuthorizationService]) -> None:
    _, authorization, clients = services
    full_caller = client(clients, name="Full caller", mode="full")
    restricted_caller = client(clients, name="Read only", mode="custom", grants=["jobs:read"])

    # A caller with Full Access cannot bypass an extension that has no grant.
    with pytest.raises(PlatformOperationError):
        authorization.require_effective_capability(full_caller, "lynewright.alpha", "jobs:create")

    authorization.set_permissions(
        "lynewright.alpha", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager()
    )
    # An extension with Full Access cannot bypass a caller with no create grant.
    with pytest.raises(PlatformOperationError):
        authorization.require_effective_capability(restricted_caller, "lynewright.alpha", "jobs:create")


def test_only_requested_public_capabilities_can_be_persisted(services: tuple[ExtensionRegistry, ExtensionAuthorizationService, ClientAuthorizationService]) -> None:
    _, authorization, _ = services
    with pytest.raises(PlatformValidationError, match="requested"):
        authorization.set_permissions(
            "lynewright.alpha",
            permission_mode="custom",
            granted_capabilities=["network:use"],
            actor=CapabilityActor.platform_manager(),
        )


def test_project_scope_and_platform_policy_are_explicit_effective_checks(services: tuple[ExtensionRegistry, ExtensionAuthorizationService, ClientAuthorizationService]) -> None:
    _, authorization, clients = services
    caller = client(clients, name="Scoped caller", mode="full")
    authorization.set_permissions(
        "lynewright.alpha", permission_mode="full", granted_capabilities=[], actor=CapabilityActor.platform_manager()
    )

    with pytest.raises(PlatformOperationError, match="require a project-scope policy"):
        authorization.require_effective_capability(
            caller, "lynewright.alpha", "jobs:create", project_id="project-a"
        )
    with pytest.raises(PlatformOperationError, match="Project scope denied"):
        authorization.require_effective_capability(
            caller,
            "lynewright.alpha",
            "jobs:create",
            project_id="project-a",
            project_scope=lambda *_: False,
        )
    with pytest.raises(PlatformOperationError, match="Platform policy denied"):
        authorization.require_effective_capability(
            caller,
            "lynewright.alpha",
            "jobs:create",
            project_id="project-a",
            project_scope=lambda *_: True,
            platform_policy=lambda *_: False,
        )
    with pytest.raises(PlatformValidationError, match="known public"):
        authorization.set_permissions(
            "lynewright.alpha",
            permission_mode="custom",
            granted_capabilities=["core-db:read"],
            actor=CapabilityActor.platform_manager(),
        )
