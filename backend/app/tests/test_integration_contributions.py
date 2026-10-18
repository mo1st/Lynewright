from __future__ import annotations

import sqlite3

import pytest

from app.services.integration_contributions import ContributionRegistry
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformOperationError, PlatformStore


def manifest(integration_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "id": integration_id,
        "name": integration_id,
        "version": "0.1.0",
        "type": "native",
        "lynewright_version": "^0.1.0",
        "sdk_version": "^1.0.0",
        "backend": "dist/backend.mjs",
    }


@pytest.fixture()
def contributions() -> tuple[IntegrationRegistry, ContributionRegistry]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    integrations = IntegrationRegistry(store)
    integrations.register(manifest("lynewright.integration.alpha"))
    integrations.register(manifest("lynewright.integration.beta"))
    integrations.enable("lynewright.integration.alpha")
    integrations.enable("lynewright.integration.beta")
    registry = ContributionRegistry(integrations)
    yield integrations, registry
    connection.close()


def test_owned_contributions_register_discover_and_invoke(contributions: tuple[IntegrationRegistry, ContributionRegistry]) -> None:
    _, registry = contributions
    handle = registry.register(
        integration_id="lynewright.integration.alpha",
        local_id="service.echo",
        kind="service",
        handler=lambda value: {"echo": value},
        metadata={"contract": "lynewright.mock.echo", "version": "1.0.0"},
    )

    assert handle.id == "lynewright.integration.alpha:service.echo"
    assert registry.list(integration_id="lynewright.integration.alpha")[0].metadata["contract"] == "lynewright.mock.echo"
    assert registry.invoke(handle.id, "hello") == {"echo": "hello"}


def test_disable_unregisters_owned_contributions_and_stale_handles_are_safe(contributions: tuple[IntegrationRegistry, ContributionRegistry]) -> None:
    integrations, registry = contributions
    disposed: list[str] = []
    old_handle = registry.register(
        integration_id="lynewright.integration.alpha",
        local_id="health.main",
        kind="health",
        handler=lambda: {"state": "HEALTHY"},
        metadata={"dispose": lambda: disposed.append("old")},
    )
    registry.disable_integration("lynewright.integration.alpha")
    assert integrations.get("lynewright.integration.alpha")["enabled"] is False
    assert registry.list(integration_id="lynewright.integration.alpha") == []
    assert disposed == ["old"]
    with pytest.raises(PlatformNotFoundError):
        registry.invoke(old_handle.id)

    integrations.enable("lynewright.integration.alpha")
    replacement = registry.register(
        integration_id="lynewright.integration.alpha", local_id="health.main", kind="health", handler=lambda: {"state": "HEALTHY"}
    )
    assert old_handle.dispose() is False
    assert registry.invoke(replacement.id) == {"state": "HEALTHY"}


def test_duplicate_collisions_and_disabled_owners_are_rejected(contributions: tuple[IntegrationRegistry, ContributionRegistry]) -> None:
    integrations, registry = contributions
    registry.register(integration_id="lynewright.integration.alpha", local_id="ui.page", kind="ui")
    with pytest.raises(PlatformConflictError):
        registry.register(integration_id="lynewright.integration.alpha", local_id="ui.page", kind="ui")

    beta = registry.register(integration_id="lynewright.integration.beta", local_id="ui.page", kind="ui")
    integrations.disable("lynewright.integration.beta")
    assert beta.id not in {item.id for item in registry.list()}
    with pytest.raises(PlatformOperationError, match="enabled"):
        registry.register(integration_id="lynewright.integration.beta", local_id="ui.page", kind="ui")


def test_event_failures_are_isolated_and_health_reports_are_structured(contributions: tuple[IntegrationRegistry, ContributionRegistry]) -> None:
    _, registry = contributions
    received: list[str] = []
    registry.register(
        integration_id="lynewright.integration.alpha", local_id="events.fail", kind="event",
        handler=lambda _: (_ for _ in ()).throw(RuntimeError("boom")), metadata={"pattern": "project.*"},
    )
    registry.register(
        integration_id="lynewright.integration.beta", local_id="events.ok", kind="event",
        handler=lambda event: received.append(event["type"]), metadata={"pattern": "project.*"},
    )
    dispatched = registry.dispatch_events("project.updated", {"project_id": "project-a"})
    assert dispatched["delivered"] == 1
    assert dispatched["failures"] == ["lynewright.integration.alpha:events.fail"]
    assert received == ["project.updated"]

    healthy = registry.register(
        integration_id="lynewright.integration.alpha", local_id="health.ok", kind="health", handler=lambda: {"state": "HEALTHY"}
    )
    registry.register(
        integration_id="lynewright.integration.beta", local_id="health.fail", kind="health",
        handler=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    results = registry.run_health_checks()
    assert results[healthy.id] == {"status": "ok", "result": {"state": "HEALTHY"}}
    assert results["lynewright.integration.beta:health.fail"] == {"status": "error", "error": "RuntimeError"}
