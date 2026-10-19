from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from app.services.integration_contributions import ContributionRegistry
from app.services.integration_execution import (
    IntegrationExecutionRunner,
    IntegrationExecutionTimeout,
    current_execution_context,
)
from app.services.integration_lifecycle import IntegrationLifecycleRuntime, IntegrationRuntimeDefinition
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformStore


def _manifest(integration_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0", "id": integration_id, "name": "Execution", "version": "0.1.0",
        "type": "service", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0",
        "service": {"kind": "http", "health_check": "/health"}, "backend": "dist/backend.mjs",
    }


def test_execution_timeout_returns_promptly_and_signals_cooperative_cancellation() -> None:
    stopped = threading.Event()

    def handler() -> None:
        context = current_execution_context()
        assert context is not None
        while not context.cancelled:
            time.sleep(0.005)
        stopped.set()

    started = time.monotonic()
    with pytest.raises(IntegrationExecutionTimeout):
        IntegrationExecutionRunner().run("test", handler, timeout_seconds=0.05)
    assert time.monotonic() - started < 0.25
    assert stopped.wait(0.25)


def test_contribution_and_diagnostic_callbacks_observe_execution_budgets() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    integration_id = "lynewright.integration.execution"
    integrations = IntegrationRegistry(store)
    integrations.register(_manifest(integration_id))
    contributions = ContributionRegistry(integrations)
    runtime = IntegrationLifecycleRuntime(integrations, contributions)

    def wait_for_cancel() -> None:
        context = current_execution_context()
        assert context is not None
        while not context.cancelled:
            time.sleep(0.005)

    runtime.register_definition(IntegrationRuntimeDefinition(
        integration_id,
        diagnose=lambda _: wait_for_cancel(),
        health=lambda _: {"state": "HEALTHY"},
        execution_timeout_seconds=0.05,
    ))
    runtime.enable(integration_id)
    contributions.register(
        integration_id=integration_id, local_id="slow", kind="service",
        metadata={"execution_timeout_seconds": 0.05}, handler=wait_for_cancel,
    )

    with pytest.raises(IntegrationExecutionTimeout):
        contributions.invoke(f"{integration_id}:slow")
    assert runtime.diagnose(integration_id)["error"] == "IntegrationExecutionTimeout"
    assert runtime.health(integration_id)["runtime"] == {"state": "HEALTHY"}
    db.close()
