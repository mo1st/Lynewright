from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DIST_NAME = "lynewright-integration-example"
INTEGRATION_ID = "lynewright.integration.example-native"


def _render_line(lynewright, operation: dict, prompt: str) -> dict:
    stage_id = "render"
    return lynewright.run_stages([{
        "id": stage_id,
        "kind": "async_operation",
        "operation_id": operation["operation_id"],
        "contract": operation["contract"],
        "operation": operation["operation"],
        "args_template": {"prompt": prompt},
        "status_operation": {
            "contract": operation["contract"],
            "operation": "status",
        },
        "status_args_template": {"job_id": "{{stages.render.output.job_id}}"},
        "async_ref_path": "job_id",
        "status_ref_field": "job_id",
    }])


def test_declared_setting_schema_is_bounded() -> None:
    from example_native.settings import SETTINGS_SCHEMA

    assert SETTINGS_SCHEMA["fields"][0]["key"] == "style"
    assert SETTINGS_SCHEMA["fields"][0]["options"] == ["calm", "bright"]


@pytest.mark.lynewright
def test_native_package_is_hidden_until_listed_and_runs_async_with_settings(lynewright) -> None:
    from app.services.production_integrations import discover_shipped_manifests

    before = discover_shipped_manifests()
    assert not any(item.distribution == DIST_NAME for item in before)

    lynewright.monkeypatch.setenv("LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES", "1")
    integration_id = lynewright.install_native_package(DIST_NAME, granted_capabilities=["media:create"])
    assert integration_id == INTEGRATION_ID
    assert any(
        item["id"] == INTEGRATION_ID
        for item in lynewright.management.integrations.list()
    )
    lynewright.update_settings(integration_id, {"style": "calm"})
    operation = next(item for item in lynewright.operations(integration_id=integration_id) if item["operation"] == "render")
    first = _render_line(lynewright, operation, "small blue landscape")
    assert first["status"] == "done", first
    first_output = first["stages"][0]["output"]
    assert first_output["style"] == "calm"
    assert first_output["media_refs"][0]["content_type"] == "image/png"

    lynewright.update_settings(integration_id, {"style": "bright"})
    second = _render_line(lynewright, operation, "small orange landscape")
    assert second["status"] == "done", second
    second_output = second["stages"][0]["output"]
    assert second_output["style"] == "bright"
    assert second_output["sha256"] != first_output["sha256"]
