"""Generate hardened Compose and Quadlet snippets; never starts containers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

import yaml

from app.services.integration_provenance import get_provenance
from app.services.platform_services import PlatformStore


DEFAULT_LIMITS = {"cpus": 1.0, "memory": "512m", "pids": 128}


def _safe_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")[:48]
    return slug or "integration"


def _catalogue_entry(store: PlatformStore, integration: dict[str, Any], tenant_id: str) -> dict[str, Any] | None:
    provenance = get_provenance(store, tenant_id, str(integration["id"]))
    if not provenance or provenance.get("kind") != "catalogue":
        return None
    with store.connection() as db:
        row = db.execute("SELECT index_bytes FROM integration_catalogue_cache WHERE id=1").fetchone()
    if row is None or not row["index_bytes"]:
        return None
    try:
        index = json.loads(bytes(row["index_bytes"]).decode("utf-8"))
    except (ValueError, UnicodeError, TypeError):
        return None
    return next((
        item for item in index.get("entries", [])
        if isinstance(item, dict)
        and item.get("id") == provenance.get("catalogue_entry_id")
        and item.get("version") == provenance.get("entry_version")
    ), None)


def _systemd_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"' if any(char.isspace() for char in value) else escaped


def generate_runtime_config(
    store: PlatformStore, integration: dict[str, Any], *, format: str, tenant_id: str = "ten_default",
) -> dict[str, Any]:
    if format not in {"compose", "quadlet"}:
        raise ValueError("Runtime configuration format must be compose or quadlet.")
    name = _safe_name(str(integration.get("id") or "integration"))
    version = _safe_name(str(integration.get("version") or "latest"))
    service_name = f"lynewright-runner-{name}"
    provenance = get_provenance(store, tenant_id, str(integration["id"])) or {}
    catalogue_entry = _catalogue_entry(store, integration, tenant_id)
    warnings: list[str] = []
    limits = dict(DEFAULT_LIMITS)
    egress = "none"
    if catalogue_entry:
        entry_limits = catalogue_entry.get("limits") if isinstance(catalogue_entry.get("limits"), dict) else {}
        if "cpus" in entry_limits:
            limits["cpus"] = entry_limits["cpus"]
        if "memory_mb" in entry_limits:
            limits["memory"] = f"{int(entry_limits['memory_mb'])}m"
        elif "memory" in entry_limits:
            limits["memory"] = entry_limits["memory"]
        if "pids" in entry_limits:
            limits["pids"] = entry_limits["pids"]
        permissions = catalogue_entry.get("permissions") if isinstance(catalogue_entry.get("permissions"), dict) else {}
        egress = str(permissions.get("egress") or "none")
        if egress not in {"none", "internet"}:
            raise ValueError("Catalogue egress must be none or internet.")
    else:
        warnings.append("Outside integration uses conservative default limits; review its egress and resource use.")
    if not catalogue_entry and provenance.get("kind") == "catalogue" and provenance.get("image_digest"):
        raise ValueError("The signed catalogue entry for this installed image is unavailable.")
    if not catalogue_entry and provenance.get("kind") != "bundled":
        wheel_hash = str(provenance.get("sha256") or "")
        if not wheel_hash:
            warnings.append("Package wheel SHA-256 is not recorded; provide LYNEWRIGHT_RUNNER_WHEEL_SHA256 before building.")

    source = str(integration.get("source") or "")
    raw_configuration = integration.get("configuration") if isinstance(integration.get("configuration"), dict) else {}
    catalogue_package = raw_configuration.get("catalogue_package") if isinstance(raw_configuration.get("catalogue_package"), dict) else {}
    catalogue_container = bool(catalogue_entry and catalogue_entry.get("kind") == "container")
    if catalogue_container:
        image = str(catalogue_entry.get("source") or "")
        digest = str(provenance.get("image_digest") or "")
        declared_digest = image.rsplit("@", 1)[-1]
        if "@sha256:" not in image or digest not in {image, declared_digest}:
            raise ValueError("The installed catalogue image digest does not match its signed entry.")
        build: dict[str, Any] | None = None
    elif provenance.get("image_digest"):
        image = str(provenance["image_digest"])
        build = None
    elif source == "package":
        image = f"lynewright-runner/{name}:{version}"
        if catalogue_entry and catalogue_entry.get("kind") == "package" and catalogue_package:
            from app.config import get_settings

            packages_root = get_settings().packages_dir.resolve()
            wheel_directory = Path(str(catalogue_package.get("wheel_directory") or "")).resolve()
            try:
                relative_directory = wheel_directory.relative_to(packages_root)
            except ValueError as exc:
                raise ValueError("The verified package path is outside LYNEWRIGHT_PACKAGES_DIR.") from exc
            wheel_filename = str(catalogue_package.get("wheel_filename") or "")
            if not wheel_filename or Path(wheel_filename).name != wheel_filename:
                raise ValueError("The verified package wheel filename is invalid.")
            wheel_path = wheel_directory / wheel_filename
            expected_hash = str(provenance.get("sha256") or "")
            actual_hash = hashlib.sha256(wheel_path.read_bytes()).hexdigest() if wheel_path.is_file() else ""
            if not expected_hash or actual_hash != expected_hash or catalogue_package.get("sha256") != expected_hash:
                raise ValueError("The stored catalogue wheel is missing or no longer matches its verified SHA-256.")
            host_packages_root = str(get_settings().packages_host_dir or "").strip()
            build_context = (
                str(Path(host_packages_root) / relative_directory) if host_packages_root
                else str(wheel_directory)
            )
            warnings.append(
                "The verified wheel is stored in LYNEWRIGHT_PACKAGES_DIR. The API image does not need this package; the configured package host directory must be available to the container builder."
            )
            build = {
                "context": build_context,
                "dockerfile": "Containerfile.integration",
                "args": {
                    "RUNNER_BASE_IMAGE": "${LYNEWRIGHT_RUNNER_BASE_IMAGE:-lynewright-runner:latest}",
                    "INTEGRATION_WHEEL": wheel_filename,
                    "INTEGRATION_WHEEL_FILENAME": wheel_filename,
                    "INTEGRATION_WHEEL_SHA256": expected_hash,
                },
            }
        else:
            warnings.append(
                "Install the wheel into the API environment with python -m pip install --no-deps <wheel> and list its distribution in LYNEWRIGHT_INTEGRATION_PACKAGES; API discovery reads dist-info and lynewright-manifest.json only, without importing package code unless LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES=1."
            )
            build = {
                # Keep the integration image's context confined to runner/ so a
                # local Lynewright database, env file, or media directory can never
                # be sent to the Docker daemon during a package build.
                "context": "${LYNEWRIGHT_RUNNER_BUILD_CONTEXT:-runner}",
                "dockerfile": "Containerfile.integration",
                "args": {
                    "RUNNER_BASE_IMAGE": "${LYNEWRIGHT_RUNNER_BASE_IMAGE:-lynewright-runner:latest}",
                    "INTEGRATION_WHEEL": "${LYNEWRIGHT_RUNNER_WHEEL_FILE:?set the package wheel path relative to runner/ build context}",
                    "INTEGRATION_WHEEL_FILENAME": "${LYNEWRIGHT_RUNNER_WHEEL_FILENAME:?set the exact PEP 427 wheel filename}",
                    "INTEGRATION_WHEEL_SHA256": "${LYNEWRIGHT_RUNNER_WHEEL_SHA256:?set the exact package wheel SHA-256}",
                },
            }
    else:
        raise ValueError("Runner configuration is available only for native package or catalogue image integrations.")

    compose_env_file = f"${{LYNEWRIGHT_RUNNER_ENV_FILE:-./runner-secrets/{name}.env}}"
    quadlet_env_file = f"/etc/lynewright/runner-secrets/{name}.env"
    compose_environment = {
        "LYNEWRIGHT_RUNNER_API_URL": "${LYNEWRIGHT_RUNNER_API_URL:-http://api:8000}",
        "LYNEWRIGHT_RUNNER_INTEGRATION_ID": str(integration["id"]),
        "LYNEWRIGHT_RUNNER_MANIFEST": str(integration.get("source_entrypoint") or ""),
    }
    networks = ["lynewright-integration"] + (["default"] if egress == "internet" else [])
    if format == "compose":
        service: dict[str, Any] = {
            "image": image,
            "user": "10001:10001",
            "read_only": True,
            "tmpfs": ["/tmp:size=64m,mode=1777", "/work:size=256m,mode=1777"],
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "cpus": limits["cpus"],
            "mem_limit": limits["memory"],
            "pids_limit": limits["pids"],
        }
        if not catalogue_container:
            service["env_file"] = [compose_env_file]
            service["environment"] = compose_environment
        service["networks"] = networks
        service["restart"] = "unless-stopped"
        if build is not None:
            service["build"] = build
        document = {
            "services": {service_name: service},
            "networks": {"lynewright-integration": {"internal": True}},
        }
        content = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    else:
        unit_name = service_name
        lines = [
            "[Unit]", f"Description=Lynewright runner for {integration.get('name') or integration['id']}", "",
            "[Container]", f"Image={image}", f"ContainerName={unit_name}", "User=10001:10001",
            "ReadOnly=true", "NoNewPrivileges=true", "DropCapability=all",
            "Tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m", "Tmpfs=/work:rw,noexec,nosuid,nodev,size=256m",
            f"Memory={limits['memory']}", f"PidsLimit={limits['pids']}",
            f"CPUQuota={int(float(limits['cpus']) * 100)}%",
        ]
        quadlet_environment = {
            "LYNEWRIGHT_RUNNER_API_URL": "http://api:8000",
            "LYNEWRIGHT_RUNNER_INTEGRATION_ID": str(integration["id"]),
            "LYNEWRIGHT_RUNNER_MANIFEST": str(integration.get("source_entrypoint") or ""),
        }
        if not catalogue_container:
            lines.append(f"EnvironmentFile={quadlet_env_file}")
            for key, value in quadlet_environment.items():
                lines.append(f"Environment={_systemd_quote(f'{key}={value}')}")
        lines.append(f"Network={unit_name}.network")
        if build is not None:
            warnings.append("Build the image from runner/Containerfile.integration before applying this Quadlet unit.")
        content = "\n".join(lines) + "\n"
        network_content = "[Network]\nInternal=true\n" if egress == "none" else "[Network]\nInternal=false\n"
        return {
            "format": format, "content": content,
            "network_file": f"{unit_name}.network", "network_content": network_content,
            "token_env_file": None if catalogue_container else quadlet_env_file,
            "warnings": warnings, "egress": egress,
            "limits": limits,
        }
    return {
        "format": format, "content": content,
        "token_env_file": None if catalogue_container else compose_env_file,
        "warnings": warnings, "egress": egress, "limits": limits,
    }


__all__ = ["DEFAULT_LIMITS", "generate_runtime_config"]
