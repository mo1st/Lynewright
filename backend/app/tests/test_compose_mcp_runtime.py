from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_mcp_uses_api_root_entrypoint_and_waits_for_api_ownership_setup() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    api = compose["services"]["api"]
    mcp = compose["services"]["mcp"]

    assert api["build"] == mcp["build"]
    assert api["user"] == mcp["user"] == "0:0"
    assert api["entrypoint"] == mcp["entrypoint"] == ["python", "entrypoint.py"]
    assert api["environment"]["LYNEWRIGHT_UID"] == mcp["environment"]["LYNEWRIGHT_UID"]
    assert api["environment"]["LYNEWRIGHT_GID"] == mcp["environment"]["LYNEWRIGHT_GID"]
    assert mcp["environment"]["LYNEWRIGHT_SKIP_DATA_CHOWN"] == "1"
    assert mcp["depends_on"]["api"]["condition"] == "service_healthy"
    assert api["volumes"] == mcp["volumes"]


def test_services_overriding_entrypoint_also_declare_a_command() -> None:
    # Compose drops the image CMD when a service sets entrypoint, so the
    # entrypoint would start with no command and exit.
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    missing = [
        name for name, service in compose["services"].items()
        if "entrypoint" in service and not service.get("command")
    ]
    assert missing == []
