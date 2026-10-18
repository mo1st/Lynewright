from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_every_compose_service_network_is_declared(tmp_path: Path) -> None:
    assert tmp_path.is_dir()
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    declared = set((compose.get("networks") or {}).keys())

    for service_name, service in compose["services"].items():
        configured = service.get("networks")
        if configured is None:
            referenced = {"default"}
        elif isinstance(configured, dict):
            referenced = set(configured)
        else:
            referenced = set(configured)

        assert referenced <= declared, (
            f"service {service_name!r} references undeclared networks: "
            f"{sorted(referenced - declared)}"
        )
