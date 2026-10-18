from __future__ import annotations

import sqlite3

import pytest

from app.services.integration_contributions import ContributionRegistry
from app.services.integration_lifecycle import IntegrationLifecycleRuntime, IntegrationRuntimeDefinition
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformNotFoundError, PlatformOperationError, PlatformStore
from app.services.control_plane_host import get_control_plane_data, get_control_plane_registry, unregister_control_plane_registry
from app.services.dependency_readiness import Dependency, DependencyReadinessGraph


def manifest(integration_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0", "id": integration_id, "name": integration_id,
        "version": "0.1.0", "type": "native", "lynewright_version": "^0.1.0",
        "sdk_version": "^1.0.0", "backend": "dist/backend.mjs",
    }


@pytest.fixture()
def lifecycle() -> tuple[IntegrationRegistry, ContributionRegistry, IntegrationLifecycleRuntime, list[str]]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    integrations = IntegrationRegistry(store)
    for integration_id in ("lynewright.integration.healthy", "lynewright.integration.unavailable", "lynewright.integration.error"):
        integrations.register(manifest(integration_id))
    contributions = ContributionRegistry(integrations)
    runtime = IntegrationLifecycleRuntime(integrations, contributions)
    calls: list[str] = []

    def activate(context):
        calls.append(f"activate:{context.integration_id}")
        context.contribute(local_id="service.echo", kind="service", handler=lambda value: value)

    def deactivate(context):
        calls.append(f"deactivate:{context.integration_id}")

    runtime.register_definition(IntegrationRuntimeDefinition(
        "lynewright.integration.healthy", activate=activate, deactivate=deactivate,
        diagnose=lambda _: {"configuration": "valid"}, health=lambda _: {"state": "HEALTHY"},
    ))
    runtime.register_definition(IntegrationRuntimeDefinition(
        "lynewright.integration.unavailable", health=lambda _: {"state": "UNAVAILABLE"},
    ))
    runtime.register_definition(IntegrationRuntimeDefinition(
        "lynewright.integration.error", activate=lambda _: (_ for _ in ()).throw(RuntimeError("activation boom")),
    ))
    yield integrations, contributions, runtime, calls
    connection.close()


def test_enable_activate_reload_and_disable_clean_up_contributions(lifecycle: tuple[IntegrationRegistry, ContributionRegistry, IntegrationLifecycleRuntime, list[str]]) -> None:
    integrations, contributions, runtime, calls = lifecycle
    active = runtime.enable("lynewright.integration.healthy")
    assert active["status"] == "active"
    contribution_id = "lynewright.integration.healthy:service.echo"
    assert contributions.invoke(contribution_id, "ok") == "ok"
    assert runtime.diagnose("lynewright.integration.healthy")["result"] == {"configuration": "valid"}
    health = runtime.health("lynewright.integration.healthy")
    assert health["runtime"] == {"state": "HEALTHY"}
    assert integrations.get("lynewright.integration.healthy")["last_health_check_at"] is not None

    runtime.reload("lynewright.integration.healthy")
    assert calls == [
        "activate:lynewright.integration.healthy", "deactivate:lynewright.integration.healthy", "activate:lynewright.integration.healthy",
    ]
    disabled = runtime.disable("lynewright.integration.healthy")
    assert disabled["status"] == "disabled"
    assert contributions.list(integration_id="lynewright.integration.healthy") == []


def test_enable_without_a_registered_definition_does_not_persist_enabled_state() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    integration_id = "lynewright.integration.missing-definition"
    integrations = IntegrationRegistry(store)
    integrations.register(manifest(integration_id))
    runtime = IntegrationLifecycleRuntime(integrations, ContributionRegistry(integrations))

    with pytest.raises(PlatformNotFoundError, match="not registered"):
        runtime.enable(integration_id)

    assert integrations.get(integration_id)["enabled"] is False
    connection.close()


def test_ten_reloads_leave_one_live_generation_without_duplicate_handlers(lifecycle: tuple[IntegrationRegistry, ContributionRegistry, IntegrationLifecycleRuntime, list[str]]) -> None:
    integrations, contributions, runtime, calls = lifecycle
    integration_id = "lynewright.integration.healthy"
    runtime.enable(integration_id)
    contribution_id = f"{integration_id}:service.echo"
    for _ in range(10):
        runtime.reload(integration_id)
        assert [item.id for item in contributions.list(integration_id=integration_id)] == [contribution_id]
        assert contributions.invoke(contribution_id, "still-live") == "still-live"
    assert calls.count(f"activate:{integration_id}") == 11
    assert calls.count(f"deactivate:{integration_id}") == 10
    runtime.disable(integration_id)
    assert contributions.list(integration_id=integration_id) == []


def test_direct_registry_disable_deactivates_runtime_and_removes_handles(lifecycle: tuple[IntegrationRegistry, ContributionRegistry, IntegrationLifecycleRuntime, list[str]]) -> None:
    integrations, contributions, runtime, calls = lifecycle
    runtime.enable("lynewright.integration.healthy")
    integrations.disable("lynewright.integration.healthy")

    assert contributions.list(integration_id="lynewright.integration.healthy") == []
    assert calls[-1] == "deactivate:lynewright.integration.healthy"


def test_unavailable_and_activation_error_integrations_are_isolated(lifecycle: tuple[IntegrationRegistry, ContributionRegistry, IntegrationLifecycleRuntime, list[str]]) -> None:
    integrations, contributions, runtime, _ = lifecycle
    runtime.enable("lynewright.integration.unavailable")
    assert runtime.health("lynewright.integration.unavailable")["runtime"] == {"state": "UNAVAILABLE"}

    with pytest.raises(PlatformOperationError, match="activation failed"):
        runtime.enable("lynewright.integration.error")
    errored = integrations.get("lynewright.integration.error")
    assert errored["enabled"] is True
    assert errored["status"] == "error"
    assert "RuntimeError" in errored["last_error"]
    assert contributions.list(integration_id="lynewright.integration.error") == []

    # A failing integration does not affect another active integration.
    runtime.enable("lynewright.integration.healthy")
    assert contributions.invoke("lynewright.integration.healthy:service.echo", "still works") == "still works"


def test_configuration_and_enabled_state_survive_runtime_reconstruction(lifecycle: tuple[IntegrationRegistry, ContributionRegistry, IntegrationLifecycleRuntime, list[str]]) -> None:
    integrations, contributions, runtime, _ = lifecycle
    runtime.configure("lynewright.integration.healthy", {"mode": "test"})
    runtime.enable("lynewright.integration.healthy")

    recovered = IntegrationLifecycleRuntime(integrations, contributions)
    recovered.register_definition(IntegrationRuntimeDefinition(
        "lynewright.integration.healthy", activate=lambda context: context.contribute(local_id="service.recovered", kind="service", handler=lambda: "ok")
    ))
    # A fresh process has no runtime handles, so it can recover the persisted enabled integration explicitly.
    recovered.activate("lynewright.integration.healthy")
    assert contributions.invoke("lynewright.integration.healthy:service.recovered") == "ok"


def test_recover_enabled_restores_available_integrations_once_and_isolates_unavailable_ones() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    initial = IntegrationRegistry(store)
    healthy = "lynewright.integration.restart-healthy"
    unavailable = "lynewright.integration.restart-unavailable"
    broken = "lynewright.integration.restart-broken"
    for integration_id in (healthy, broken):
        initial.register(manifest(integration_id))
    # This one must declare a runtime, or "no definition after restart" is no
    # longer an error: a record that declares nothing executable is reported as
    # not_executable instead, which is what stopped legacy placeholders looking
    # like broken integrations. Here the point is a genuinely missing
    # definition, so the manifest has to claim one.
    initial.register({
        **manifest(unavailable), "schema_version": "1.1", "contributions": ["services"],
        "provides": [{"contract": "lynewright.restart",
                      "implementation": "app.integrations.absent.provider:Missing"}],
    })
    initial.configure(healthy, {"mode": "persisted"})
    for integration_id in (healthy, unavailable, broken):
        initial.enable(integration_id)

    # New service instances model a process restart: only persisted registry
    # data remains; adapter code registers definitions during boot.
    integrations = IntegrationRegistry(store)
    contributions = ContributionRegistry(integrations)
    runtime = IntegrationLifecycleRuntime(integrations, contributions)
    configurations: list[dict[str, object]] = []

    def activate(context):
        configurations.append(context.configuration)
        context.contribute(local_id="service.echo", kind="service", handler=lambda: "recovered")

    runtime.register_definition(IntegrationRuntimeDefinition(healthy, activate=activate))
    runtime.register_definition(IntegrationRuntimeDefinition(
        broken,
        activate=lambda _: (_ for _ in ()).throw(RuntimeError("adapter unavailable")),
    ))

    result = runtime.recover_enabled()
    assert result == {
        "activated": [healthy],
        "already_active": [],
        "unavailable": [unavailable],
        "not_executable": [],
        "failed": [broken],
    }
    assert configurations == [{"mode": "persisted"}]
    assert contributions.invoke(f"{healthy}:service.echo") == "recovered"
    assert integrations.get(unavailable)["enabled"] is True
    assert integrations.get(unavailable)["status"] == "error"
    assert integrations.get(broken)["status"] == "error"

    # Repeated recovery is idempotent: no duplicate contribution or activation.
    assert runtime.recover_enabled()["already_active"] == [healthy]
    assert [item.id for item in contributions.list(integration_id=healthy)] == [f"{healthy}:service.echo"]
    connection.close()


def test_activation_context_can_bind_provider_actions_to_the_tenant_host() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    tenant_id = "tenant-control-plane-activation"
    integration_id = "lynewright.integration.social-activation"
    integrations = IntegrationRegistry(store, tenant_id=tenant_id)
    integrations.register(manifest(integration_id))
    contributions = ContributionRegistry(integrations)
    runtime = IntegrationLifecycleRuntime(integrations, contributions)
    bound: list[object] = []

    def activate(context):
        context.contribute(
            local_id="social.publisher.v1",
            kind="service",
            metadata={"contract": "lynewright.social.publisher", "version": "1.0.0"},
            handler=lambda _operation, _payload: {},
        )
        class VideoProvider:
            def create_informational_short(self, **_payload):
                return {"generation_id": "generation-1"}

            def get_generation_status(self, _generation_id):
                return {"state": "completed"}

        bound.append(context.register_control_plane_service_handlers(require_verified=False))
        bound.append(context.register_control_plane_video_handlers(VideoProvider()))

    runtime.register_definition(IntegrationRuntimeDefinition(integration_id, activate=activate))
    try:
        runtime.enable(integration_id)
        dispatcher = bound[0]
        assert ("social.publish.create", "create") in dispatcher._handlers
        assert ("social.publish.schedule", "schedule") in dispatcher._handlers
        assert ("media.video.render", "render") in dispatcher._handlers
        assert ("media.video.status", "status") in dispatcher._handlers
        registry = get_control_plane_registry(tenant_id)
        # E2 removed built-in social/video descriptors from the empty-by-default
        # registry; activation still binds and disposes the generic action handlers.
        runtime.disable(integration_id)
        assert ("social.publish.create", "create") not in dispatcher._handlers
        assert ("social.publish.schedule", "schedule") not in dispatcher._handlers
        assert ("media.video.render", "render") not in dispatcher._handlers
        assert ("media.video.status", "status") not in dispatcher._handlers
        assert ("resource.lifecycle.reconcile", "reconcile") in dispatcher._handlers
    finally:
        if integration_id in runtime._contexts:
            runtime.disable(integration_id)
        unregister_control_plane_registry(tenant_id)
        connection.close()


def test_activation_context_disposes_control_plane_data_adapter_on_disable() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    tenant_id = "tenant-control-plane-data-lifecycle"
    integration_id = "lynewright.integration.data-activation"
    integrations = IntegrationRegistry(store, tenant_id=tenant_id)
    integrations.register(manifest(integration_id))
    contributions = ContributionRegistry(integrations)
    runtime = IntegrationLifecycleRuntime(integrations, contributions)

    def activate(context):
        context.register_control_plane_data_adapter(
            "media.video.status",
            lambda site_id, project_id: {
                "site_id": site_id,
                "project_id": project_id,
                "state": "completed",
            },
        )

    runtime.register_definition(IntegrationRuntimeDefinition(integration_id, activate=activate))
    try:
        runtime.enable(integration_id)
        assert get_control_plane_data(
            tenant_id,
            "media.video.status",
            site_id=7,
            project_id="project-1",
        ) == {"site_id": 7, "project_id": "project-1", "state": "completed"}

        runtime.disable(integration_id)
        assert get_control_plane_data(
            tenant_id,
            "media.video.status",
            site_id=7,
            project_id="project-1",
        ) is None
    finally:
        unregister_control_plane_registry(tenant_id)
        connection.close()


def test_dependency_loss_suspends_and_recovery_reactivates_enabled_integration() -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    store = PlatformStore(lambda: connection)
    tenant_id = "tenant-dependency-lifecycle"
    integration_id = "lynewright.integration.dependency-lifecycle"
    dependency = Dependency(kind="credential", id="social-token")
    integrations = IntegrationRegistry(store, tenant_id=tenant_id)
    integrations.register(manifest(integration_id))
    contributions = ContributionRegistry(integrations)
    graph = DependencyReadinessGraph()
    runtime = IntegrationLifecycleRuntime(integrations, contributions, dependency_graph=graph)
    events: list[str] = []

    def activate(context):
        events.append("activate")
        context.contribute(local_id="service.live", kind="service", handler=lambda: "live")

    def deactivate(_context):
        events.append("deactivate")

    runtime.register_definition(IntegrationRuntimeDefinition(integration_id, activate=activate, deactivate=deactivate))
    assert runtime.register_dependencies(integration_id, (dependency,))["state"] == "WAITING"

    integrations.enable(integration_id)
    waiting = runtime.activate(integration_id)
    assert waiting["status"] == "degraded"
    assert integrations.get(integration_id)["enabled"] is True
    assert not runtime.is_active(integration_id)

    ready = runtime.set_dependency_available(dependency)
    assert ready["reactivated"] == [integration_id]
    assert runtime.is_active(integration_id)
    assert contributions.invoke(f"{integration_id}:service.live") == "live"

    lost = runtime.set_dependency_available(dependency, available=False)
    assert lost["suspended"] == [integration_id]
    assert integrations.get(integration_id)["status"] == "degraded"
    assert not runtime.is_active(integration_id)
    assert contributions.list(integration_id=integration_id) == []

    recovered = runtime.set_dependency_available(dependency)
    assert recovered["reactivated"] == [integration_id]
    assert events == ["activate", "deactivate", "activate"]
    runtime.disable(integration_id)
    connection.close()
