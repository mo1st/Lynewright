"""Recovery must tell "nothing to run" apart from "it failed to run".

Two very different situations produced the same red status. A record that
declares nothing executable was reported as "Integration runtime definition is
unavailable after restart", rewritten on every restart, which is what made a
legacy placeholder look like a broken integration and invited someone to go
build a runtime definition for it.

An error means Lynewright expected this to execute and something failed.
`not_executable` means the record declares no runtime under the current
architecture. Collapsing them costs the diagnostic its meaning.
"""
from __future__ import annotations

import sqlite3

from app.services.extension_registry import EXTENSION_STATUS_NOT_EXECUTABLE, ExtensionRegistry
from app.services.integration_contributions import ContributionRegistry
from app.services.integration_lifecycle import (
    IntegrationLifecycleRuntime,
    IntegrationRuntimeDefinition,
)
from app.services.platform_services import PlatformStore


def _runtime() -> tuple[IntegrationLifecycleRuntime, ExtensionRegistry]:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    integrations = ExtensionRegistry(PlatformStore(lambda: db))
    return IntegrationLifecycleRuntime(integrations, ContributionRegistry(integrations)), integrations


def _manifest(integration_id: str, *, declares: bool) -> dict:
    body: dict = {
        "schema_version": "1.1" if declares else "1.0",
        "id": integration_id, "name": "Subject", "version": "1.0.0", "type": "native",
        "lynewright_version": "0.1.0", "sdk_version": "1.0.0",
        "contributions": ["services"] if declares else [],
    }
    if declares:
        body["provides"] = [{
            "contract": "lynewright.subject",
            "implementation": "app.integrations.absent.provider:Missing",
        }]
    else:
        # The shape the legacy placeholders actually carry.
        body["backend"] = "dist/backend.mjs"
    return body


_PLACEHOLDER = "lynewright.integration.legacy-placeholder"
_EXPECTED_TO_RUN = "lynewright.integration.declares-runtime"


# --------------------------------------------------------------------------
# A placeholder stops being reported as an error
# --------------------------------------------------------------------------

def test_a_placeholder_is_not_executable_rather_than_errored() -> None:
    runtime, integrations = _runtime()
    integrations.register(_manifest(_PLACEHOLDER, declares=False))
    integrations.enable(_PLACEHOLDER)
    # The state production was actually in, rewritten on every restart.
    integrations.set_runtime_status(
        _PLACEHOLDER, status="error",
        error="Integration runtime definition is unavailable after restart.",
    )

    result = runtime.recover_enabled()
    record = integrations.get(_PLACEHOLDER)

    assert _PLACEHOLDER in result["not_executable"]
    assert _PLACEHOLDER not in result["unavailable"]
    assert _PLACEHOLDER not in result["failed"]
    assert record["status"] == EXTENSION_STATUS_NOT_EXECUTABLE
    assert record["last_error"] == ""
    # Enabled is an operator decision and recovery does not touch it.
    assert record["enabled"] is True


def test_no_activation_is_attempted_for_a_placeholder() -> None:
    runtime, integrations = _runtime()
    integrations.register(_manifest(_PLACEHOLDER, declares=False))
    integrations.enable(_PLACEHOLDER)

    runtime.recover_enabled()

    assert not runtime.is_active(_PLACEHOLDER)
    assert runtime.contributions.list() == []


# --------------------------------------------------------------------------
# A genuine failure is still a genuine failure
# --------------------------------------------------------------------------

def test_a_declared_runtime_with_no_definition_is_still_an_error() -> None:
    """The branch must not become a way to hide real activation failures."""
    runtime, integrations = _runtime()
    integrations.register(_manifest(_EXPECTED_TO_RUN, declares=True))
    integrations.enable(_EXPECTED_TO_RUN)

    result = runtime.recover_enabled()
    record = integrations.get(_EXPECTED_TO_RUN)

    assert _EXPECTED_TO_RUN in result["unavailable"]
    assert _EXPECTED_TO_RUN not in result["not_executable"]
    assert record["status"] == "error"
    assert "unavailable after restart" in record["last_error"]


def test_an_integration_with_a_registered_definition_still_activates() -> None:
    """Persisted sources declare no contributions yet activate perfectly well.

    Their definitions come from durable configuration rather than a manifest,
    so the discriminator has to be registration and not the declaration alone.
    """
    runtime, integrations = _runtime()
    source_id = "lynewright.integration.source.example"
    integrations.register(_manifest(source_id, declares=False))
    integrations.enable(source_id)
    activated: list[str] = []
    runtime.register_definition(IntegrationRuntimeDefinition(
        integration_id=source_id, definition_version="1.0.0",
        activate=lambda _context: activated.append(source_id),
    ))

    result = runtime.recover_enabled()

    assert source_id in result["activated"]
    assert source_id not in result["not_executable"]
    assert activated == [source_id]


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------

def test_repeated_restarts_stop_rewriting_the_row() -> None:
    """The original defect rewrote the same misleading error on every restart."""
    runtime, integrations = _runtime()
    integrations.register(_manifest(_PLACEHOLDER, declares=False))
    integrations.enable(_PLACEHOLDER)
    integrations.set_runtime_status(_PLACEHOLDER, status="error", error="stale")

    runtime.recover_enabled()
    settled = integrations.get(_PLACEHOLDER)["updated_at"]

    for _ in range(3):
        result = runtime.recover_enabled()
        record = integrations.get(_PLACEHOLDER)
        assert _PLACEHOLDER in result["not_executable"]
        assert record["status"] == EXTENSION_STATUS_NOT_EXECUTABLE
        assert record["last_error"] == ""
        # Already correct, so recovery has nothing to write.
        assert record["updated_at"] == settled
