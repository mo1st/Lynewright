"""Discovery of bundled and allowlisted native integration packages.

This used to hold a manifest and a factory per provider, which was the shape the
compiler exists to replace: adding a provider meant editing Core. It now walks
`app.integrations`, reads selected distributions' entry-point manifests, and
installs them. A listed distribution is trusted Python code; an unlisted
distribution is never imported.

Installation is idempotent and always leaves the integration **disabled**:
`ExtensionRegistry.register` inserts `enabled=0`, so shipping a release can
never switch on a provider that makes outbound calls, and re-running it on one
an operator turned off reports "present" without re-enabling it.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import hashlib
import json
import logging
import os
import pkgutil
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from packaging.version import Version

from app.services.platform_services import PlatformConflictError, PlatformNotFoundError


# Bundled packages are classified by their host-owned root. External packages
# get their role from the entry-point group, never from a manifest field.
INTEGRATIONS_PACKAGE = "app.integrations"
FEATURES_PACKAGE = "app.features"
PRODUCT_ROLE_EXTERNAL_INTEGRATION = "external_integration"
PRODUCT_ROLE_FIRST_PARTY_FEATURE = "first_party_feature"
PRODUCT_KIND_INTEGRATION = "integration"
PRODUCT_KIND_FEATURE = "feature"
# The namespace is reserved for statically shipped feature identities. It lets
# a stale registry row retain its honest classification after package removal.
FIRST_PARTY_FEATURE_ID_PREFIX = "lynewright.feature."

# The attribute an integration package exposes to declare itself. A package
# without one is not an integration; it is skipped rather than guessed at.
MANIFEST_SUFFIX = "_MANIFEST"

# Bundled integrations expose a Python manifest module; installed packages
# expose the same declaration as lynewright-manifest.json package data.
MANIFEST_MODULE = "manifest"
PACKAGE_ENTRY_POINT_GROUP = "lynewright.integrations"
_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_LOG = logging.getLogger(__name__)

# A package listed in LYNEWRIGHT_INTEGRATION_PACKAGES is rediscovered on API
# reads. Suppress it for the rest of this process after an explicit removal;
# removing it from the setting prevents discovery after the next restart.
_REMOVED_INTEGRATIONS: set[tuple[str, str]] = set()


def mark_integration_removed_live(tenant_id: str, integration_id: str) -> None:
    _REMOVED_INTEGRATIONS.add((str(tenant_id), str(integration_id)))


def clear_integration_removed_live(tenant_id: str, integration_id: str) -> None:
    _REMOVED_INTEGRATIONS.discard((str(tenant_id), str(integration_id)))


def _excluded_integration_ids() -> frozenset[str]:
    """Return deployment-owned integration identities that must stay absent.

    Bundled packages are discovered dynamically, so removing an integration
    from the database alone would make the next startup install it again.
    This narrow, generic exclusion list keeps retirement reversible and does
    not require provider-specific branches in the host.  First-party features
    are never filtered here; the setting only applies to the external
    integration package root.
    """
    raw = os.getenv("LYNEWRIGHT_EXCLUDED_INTEGRATIONS", "")
    return frozenset(
        item.strip()
        for item in raw.split(",")
        if item.strip()
    )


@dataclass(frozen=True, slots=True)
class DiscoveredManifest:
    """A manifest plus its host-derived source and integration classification."""

    manifest: dict[str, Any]
    package_root: str
    product_role: str
    kind: str
    source: str = "bundled"
    distribution: str = ""
    distribution_version: str = ""
    entrypoint_module: str = ""
    artifact_sha256: str = ""
    installed_sha256: str = ""


def _listed_packages() -> dict[str, str]:
    from packaging.utils import canonicalize_name

    return {
        canonicalize_name(item.strip()): item.strip()
        for item in os.getenv("LYNEWRIGHT_INTEGRATION_PACKAGES", "").split(",")
        if item.strip()
    }


def _distribution_wheel_sha256(distribution: Any) -> str:
    """Read pip's recorded source-wheel digest without importing package code."""
    try:
        raw = distribution.read_text("direct_url.json")
        direct_url = json.loads(raw) if raw else {}
        archive = direct_url.get("archive_info") if isinstance(direct_url, dict) else {}
        if not isinstance(archive, dict):
            return ""
        digest = (archive.get("hashes") or {}).get("sha256") if isinstance(archive.get("hashes"), dict) else ""
        if not digest and str(archive.get("hash") or "").startswith("sha256="):
            digest = str(archive["hash"]).removeprefix("sha256=")
        digest = str(digest or "").lower()
        return digest if re.fullmatch(r"[a-f0-9]{64}", digest) else ""
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return ""


def _distribution_file_set_sha256(distribution: Any) -> str:
    """Fingerprint installed distribution files when pip has no wheel digest."""
    try:
        files = sorted(distribution.files or (), key=lambda item: str(item).replace("\\", "/"))
        if not files:
            return ""
        digest = hashlib.sha256()
        hashed_files = 0
        for item in files:
            relative = str(item).replace("\\", "/")
            if relative.startswith("/") or any(part in {"", ".", ".."} for part in relative.split("/")):
                continue
            location = distribution.locate_file(item)
            if not location.is_file():
                return ""
            content_digest = hashlib.sha256(location.read_bytes()).digest()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(content_digest)
            digest.update(b"\n")
            hashed_files += 1
        return digest.hexdigest() if hashed_files else ""
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError):
        return ""


def _catalogue_package_root(record: dict[str, Any], registry: Any, provenance: dict[str, Any] | None) -> str:
    """Allow verified stored catalogue wheels without installing them in the API."""
    configuration = record.get("configuration") if isinstance(record.get("configuration"), dict) else {}
    stored = configuration.get("catalogue_package") if isinstance(configuration.get("catalogue_package"), dict) else None
    if not stored or not provenance or provenance.get("kind") != "catalogue":
        return ""
    try:
        from app.config import get_settings

        root = get_settings().packages_dir.resolve()
        directory = Path(str(stored.get("wheel_directory") or "")).resolve()
        directory.relative_to(root)
        filename = str(stored.get("wheel_filename") or "")
        if not filename or Path(filename).name != filename:
            return ""
        wheel = directory / filename
        expected = str(provenance.get("sha256") or "").lower()
        if (
            not expected
            or expected != str(stored.get("sha256") or "").lower()
            or not wheel.is_file()
            or hashlib.sha256(wheel.read_bytes()).hexdigest() != expected
        ):
            return ""
        entrypoint = str(record.get("source_entrypoint") or "")
        if _MODULE_NAME.fullmatch(entrypoint) is None:
            return ""
        package_root = entrypoint.rpartition(".")[0]
        return package_root if package_root and _MODULE_NAME.fullmatch(package_root) else ""
    except (OSError, ValueError, TypeError):
        return ""


def _read_package_manifest_data(distribution: Any, manifest_path: str) -> str:
    """Read packaged or PEP 660 editable manifest data without importing code."""
    normalized = manifest_path.replace("\\", "/")
    manifest_file = next((
        file for file in (distribution.files or ())
        if str(file).replace("\\", "/") == normalized
    ), None)
    if manifest_file is not None:
        candidate = Path(distribution.locate_file(manifest_file))
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    # Some legacy egg-info distributions do not enumerate package data in
    # files/RECORD. Keep their metadata-root fallback, still resolving only the
    # validated entry-point package-relative path.
    candidate = Path(distribution.locate_file(normalized))
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8")

    # PEP 660 editable wheels list only dist-info files. Their direct_url.json
    # points at the source tree, where package data remains unambiguously
    # addressable by the entry point's validated package name.
    raw_direct_url = distribution.read_text("direct_url.json")
    direct_url = json.loads(raw_direct_url) if raw_direct_url else {}
    directory = direct_url.get("dir_info") if isinstance(direct_url, dict) else None
    source_url = str(direct_url.get("url") or "") if isinstance(direct_url, dict) else ""
    parts = urlsplit(source_url)
    if not isinstance(directory, dict) or directory.get("editable") is not True or parts.scheme != "file" or parts.netloc not in {"", "localhost"}:
        raise FileNotFoundError(manifest_path)
    source_root = Path(url2pathname(unquote(parts.path))).resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(manifest_path)
    relative_parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in relative_parts):
        raise FileNotFoundError(manifest_path)
    candidate = source_root.joinpath(*relative_parts).resolve()
    try:
        candidate.relative_to(source_root)
    except ValueError as exc:
        raise FileNotFoundError(manifest_path) from exc
    if not candidate.is_file():
        raise FileNotFoundError(manifest_path)
    return candidate.read_text(encoding="utf-8")


def _bundled_manifests() -> list[DiscoveredManifest]:
    roots = (
        (INTEGRATIONS_PACKAGE, PRODUCT_ROLE_EXTERNAL_INTEGRATION, PRODUCT_KIND_INTEGRATION),
        (FEATURES_PACKAGE, PRODUCT_ROLE_FIRST_PARTY_FEATURE, PRODUCT_KIND_FEATURE),
    )
    manifests: list[DiscoveredManifest] = []
    for package_root, product_role, kind in roots:
        try:
            package = importlib.import_module(package_root)
        except Exception:
            continue
        for module in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
            if not module.ispkg:
                continue
            try:
                # Only the manifest module. Importing the package itself would
                # run whatever its __init__ exports, so a package could execute
                # code merely by existing. Compilation imports implementations.
                loaded = importlib.import_module(f"{package_root}.{module.name}.{MANIFEST_MODULE}")
            except Exception:
                # One unimportable or manifest-less package must not hide the rest.
                continue
            for attribute in sorted(dir(loaded)):
                if not attribute.endswith(MANIFEST_SUFFIX):
                    continue
                candidate = getattr(loaded, attribute, None)
                if isinstance(candidate, dict) and candidate.get("id"):
                    if (
                        product_role == PRODUCT_ROLE_EXTERNAL_INTEGRATION
                        and str(candidate.get("id")) in _excluded_integration_ids()
                    ):
                        continue
                    manifests.append(DiscoveredManifest(
                        manifest=candidate,
                        package_root=package_root,
                        product_role=product_role,
                        kind=kind,
                        source="bundled",
                        entrypoint_module=f"{package_root}.{module.name}",
                    ))
    return manifests


def _discover(lynewright_version: str) -> tuple[list[DiscoveredManifest], list[dict[str, str]]]:
    """Discover bundled code and allowlisted package manifests only."""
    from packaging.utils import canonicalize_name
    from app.services.extension_manifest import semver_specifier_set, validate_extension_manifest

    bundled = _bundled_manifests()
    listed = _listed_packages()
    if not listed:
        return bundled, []

    # Metadata inspection and data-manifest reads never import a distribution.
    # The allowlist is applied before any wheel file is opened; EntryPoint.load
    # is never called by API discovery.
    try:
        installed = {
            canonicalize_name(str(dist.metadata.get("Name") or "")): dist
            for dist in importlib.metadata.distributions()
            if dist.metadata.get("Name")
        }
        entry_points = list(importlib.metadata.entry_points(group=PACKAGE_ENTRY_POINT_GROUP))
    except Exception as exc:
        _LOG.warning("Could not inspect Lynewright integration package metadata: %s", type(exc).__name__)
        return bundled, [
            {"distribution": name, "version": "", "status": "metadata_error", "message": "Installed package metadata could not be read."}
            for name in listed.values()
        ]

    entries_by_distribution: dict[str, list[Any]] = {}
    for entry_point in entry_points:
        try:
            distribution_name = str(entry_point.dist.metadata.get("Name") or "")
        except Exception:
            distribution_name = ""
        normalized = canonicalize_name(distribution_name)
        if normalized in listed:
            entries_by_distribution.setdefault(normalized, []).append(entry_point)

    # Keep all valid id claimants until collision policy can be applied.
    candidates: list[tuple[DiscoveredManifest, bool, str]] = []
    statuses: list[dict[str, str]] = []
    for normalized, requested_name in sorted(listed.items()):
        distribution = installed.get(normalized)
        if distribution is None:
            message = f"Listed integration distribution {requested_name!r} is not installed."
            _LOG.warning(message)
            statuses.append({"distribution": requested_name, "version": "", "status": "not_installed", "message": message})
            continue
        distribution_name = str(distribution.metadata.get("Name") or requested_name)
        distribution_version = str(getattr(distribution, "version", "") or distribution.metadata.get("Version") or "")
        entries = sorted(entries_by_distribution.get(normalized, []), key=lambda entry: (str(entry.name), str(entry.value)))
        if not entries:
            message = f"Listed integration distribution {distribution_name!r} has no {PACKAGE_ENTRY_POINT_GROUP!r} entry point."
            _LOG.warning(message)
            statuses.append({"distribution": distribution_name, "version": distribution_version, "status": "no_entry_point", "message": message})
            continue
        found_manifest = False
        for entry_point in entries:
            module_name = str(entry_point.value or "").strip()
            if ":" in module_name or not _MODULE_NAME.fullmatch(module_name):
                message = f"Integration entry point in {distribution_name!r} must name a manifest module only."
                _LOG.warning(message)
                statuses.append({"distribution": distribution_name, "version": distribution_version, "status": "invalid_entry_point", "message": message})
                continue
            package_root = module_name.rpartition(".")[0]
            if not package_root:
                message = f"Integration entry point {module_name!r} must be a package manifest module."
                _LOG.warning(message)
                statuses.append({"distribution": distribution_name, "version": distribution_version, "status": "invalid_entry_point", "message": message})
                continue
            manifest_path = f"{package_root.replace('.', '/')}/lynewright-manifest.json"
            try:
                manifest_text = _read_package_manifest_data(distribution, manifest_path)
                manifest_data = json.loads(manifest_text)
                manifests = [manifest_data] if isinstance(manifest_data, dict) and manifest_data.get("id") else []
            except (OSError, StopIteration, UnicodeError, ValueError, AttributeError, TypeError) as exc:
                message = f"Data manifest {manifest_path!r} from {distribution_name!r} could not be read ({type(exc).__name__})."
                _LOG.warning(message)
                statuses.append({"distribution": distribution_name, "version": distribution_version, "status": "manifest_error", "message": message})
                continue
            if not manifests:
                message = f"Data manifest {manifest_path!r} in {distribution_name!r} has no integration id."
                _LOG.warning(message)
                statuses.append({"distribution": distribution_name, "version": distribution_version, "status": "no_manifest", "message": message})
                continue
            for manifest in manifests:
                found_manifest = True
                integration_id = str(manifest.get("id") or "")
                if integration_id in _excluded_integration_ids():
                    statuses.append({"distribution": distribution_name, "version": distribution_version, "status": "excluded", "message": f"Integration {integration_id!r} is excluded by LYNEWRIGHT_EXCLUDED_INTEGRATIONS.", "integration_id": integration_id})
                    continue
                try:
                    compatible = Version(lynewright_version) in semver_specifier_set(str(manifest.get("lynewright_version") or ""))
                    compatibility_message = ""
                except Exception as exc:
                    compatible = False
                    compatibility_message = f"Invalid Lynewright compatibility range ({type(exc).__name__})."
                if compatible:
                    try:
                        validate_extension_manifest(
                            manifest, lynewright_version=lynewright_version, sdk_version="1.0.0",
                        )
                    except Exception as exc:
                        compatible = False
                        compatibility_message = f"Invalid or incompatible integration manifest ({type(exc).__name__})."
                candidates.append((DiscoveredManifest(
                    manifest=manifest,
                    package_root=package_root,
                    product_role=PRODUCT_ROLE_EXTERNAL_INTEGRATION,
                    kind=PRODUCT_KIND_INTEGRATION,
                    source="package",
                    distribution=distribution_name,
                    distribution_version=distribution_version,
                    entrypoint_module=module_name,
                    artifact_sha256=_distribution_wheel_sha256(distribution),
                    installed_sha256=_distribution_file_set_sha256(distribution),
                ), compatible, compatibility_message))
        if not found_manifest and not any(item.get("distribution") == distribution_name for item in statuses):
            message = f"Listed integration distribution {distribution_name!r} did not provide a usable manifest."
            _LOG.warning(message)
            statuses.append({"distribution": distribution_name, "version": distribution_version, "status": "no_manifest", "message": message})

    bundled_ids = {str(item.manifest.get("id")) for item in bundled}
    grouped: dict[str, list[tuple[DiscoveredManifest, bool, str]]] = {}
    for candidate in candidates:
        grouped.setdefault(str(candidate[0].manifest.get("id")), []).append(candidate)
    accepted = list(bundled)
    for integration_id, matches in sorted(grouped.items()):
        collision = integration_id in bundled_ids or len(matches) > 1
        for item, compatible, compatibility_message in matches:
            if collision:
                why = "A bundled integration already owns this id." if integration_id in bundled_ids else "Multiple selected distributions declare this integration id."
                message = f"Refusing package integration {integration_id!r}: {why}"
                status = "id_collision"
                _LOG.warning(message)
            elif not compatible:
                message = compatibility_message or f"Integration {integration_id!r} requires Lynewright {item.manifest.get('lynewright_version')!r}, running {lynewright_version}."
                status = "incompatible"
                _LOG.warning(message)
            else:
                message = f"Integration {integration_id!r} is discovered and compatible."
                status = "available"
                accepted.append(item)
            statuses.append({"distribution": item.distribution, "version": item.distribution_version, "status": status, "message": message, "integration_id": integration_id})
    return sorted(accepted, key=lambda item: (item.source, item.distribution.casefold(), str(item.manifest.get("id")))), statuses


def discover_shipped_manifests(lynewright_version: str = "0.1.0") -> list[DiscoveredManifest]:
    """Return bundled manifests plus selected, compatible package manifests."""
    return _discover(lynewright_version)[0]


def integration_package_statuses(registry: Any | None = None, *, lynewright_version: str = "0.1.0") -> list[dict[str, str]]:
    """Return UI-safe discovery status, including persisted delisted packages."""
    discovered, statuses = _discover(lynewright_version)
    available_ids = {str(item.manifest.get("id")) for item in discovered}
    if registry is not None:
        from packaging.utils import canonicalize_name

        listed = _listed_packages()
        records = registry.list()
        by_id = {str(record.get("id") or ""): record for record in records}
        for item in discovered:
            if item.source != "package":
                continue
            owner = by_id.get(str(item.manifest.get("id") or ""))
            if owner is not None and owner.get("source") not in {"package", "bundled"}:
                statuses = [status for status in statuses if not (status.get("integration_id") == str(item.manifest.get("id")) and status.get("distribution") == item.distribution)]
                statuses.append({"distribution": item.distribution, "version": item.distribution_version, "status": "id_collision", "message": f"Refusing package integration {item.manifest.get('id')!r}: an existing non-package record owns this id.", "integration_id": str(item.manifest.get("id") or "")})
        for record in records:
            if record.get("source") != "package" or str(record.get("id")) in available_ids:
                continue
            distribution = str(record.get("source_distribution") or "")
            if canonicalize_name(distribution) not in listed:
                statuses.append({"distribution": distribution, "version": str(record.get("source_version") or ""), "status": "not_listed", "message": "This installed integration is not enabled by LYNEWRIGHT_INTEGRATION_PACKAGES; it remains installed but unavailable.", "integration_id": str(record.get("id") or "")})
            elif not any(item.get("integration_id") == str(record.get("id")) for item in statuses):
                statuses.append({"distribution": distribution, "version": str(record.get("source_version") or ""), "status": "unavailable", "message": "This installed integration was not discovered and cannot run.", "integration_id": str(record.get("id") or "")})
    return sorted(statuses, key=lambda item: (item.get("distribution", "").casefold(), item.get("integration_id", ""), item.get("status", "")))


def package_manifest_source_available(
    integration_id: str, distribution: str, entrypoint_module: str = "", *, lynewright_version: str = "0.1.0",
) -> bool:
    """Whether a persisted package source is currently selected and discovered."""
    return any(
        item.source == "package"
        and str(item.manifest.get("id")) == integration_id
        and item.distribution.casefold() == distribution.casefold()
        and (not entrypoint_module or item.entrypoint_module == entrypoint_module)
        for item in discover_shipped_manifests(lynewright_version)
    )


def discover_integration_manifests() -> list[dict[str, Any]]:
    """Return shipped manifests using the historical raw-list API."""
    return [item.manifest for item in discover_shipped_manifests()]


def shipped_product_metadata(lynewright_version: str = "0.1.0") -> dict[str, dict[str, str | bool]]:
    """Return trusted product metadata for shipped and reserved identities."""
    result = {
        str(item.manifest["id"]): {
            "available": True,
            "product_role": item.product_role,
            "kind": item.kind,
            "package_root": item.package_root,
            "source": item.source,
            "source_distribution": item.distribution,
            "source_version": item.distribution_version,
            "source_entrypoint": item.entrypoint_module,
        }
        for item in discover_shipped_manifests(lynewright_version)
    }
    return result


def product_metadata_for(integration_id: str, lynewright_version: str = "0.1.0") -> dict[str, str | bool]:
    """Classify one record without trusting operator-authored manifest fields."""
    shipped = shipped_product_metadata(lynewright_version).get(str(integration_id))
    if shipped is not None:
        return shipped
    if str(integration_id).startswith(FIRST_PARTY_FEATURE_ID_PREFIX):
        return {
            "available": False,
            "product_role": PRODUCT_ROLE_FIRST_PARTY_FEATURE,
            "kind": PRODUCT_KIND_FEATURE,
            "package_root": FEATURES_PACKAGE,
            "source": "bundled",
            "source_distribution": "",
            "source_version": "",
            "source_entrypoint": "",
        }
    return {
        "available": False,
        "product_role": PRODUCT_ROLE_EXTERNAL_INTEGRATION,
        "kind": PRODUCT_KIND_INTEGRATION,
        "package_root": "",
        "source": "manual",
        "source_distribution": "",
        "source_version": "",
        "source_entrypoint": "",
    }


def is_reserved_first_party_feature_id(integration_id: str) -> bool:
    """Return whether an identity is reserved for a shipped feature."""
    return str(integration_id).startswith(FIRST_PARTY_FEATURE_ID_PREFIX)


def _declaration_matches(
    stored: dict[str, Any], shipped: dict[str, Any], *,
    lynewright_version: str = "0.1.0", sdk_version: str = "1.0.0",
) -> bool:
    """Whether the stored declaration already matches what this build ships.

    Both sides are normalized through the manifest validator before comparing.
    The stored copy has been through it and the shipped literal has not, so a
    raw comparison would differ on defaults alone and rewrite the row on every
    restart forever.
    """
    from app.services.extension_manifest import validate_extension_manifest

    def normalized(value: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return validate_extension_manifest(
                value,
                lynewright_version=lynewright_version,
                sdk_version=sdk_version,
            ).model_dump(mode="json")
        except Exception:
            return None

    left, right = normalized(stored), normalized(shipped)
    if left is None or right is None:
        # An unparseable stored manifest is exactly what a refresh is for.
        return False
    return left == right


def ensure_installed(
    registry: Any,
    manifest: dict[str, Any],
    *,
    source: str = "bundled",
    distribution: str = "",
    distribution_version: str = "",
    entrypoint_module: str = "",
    artifact_sha256: str = "",
    installed_sha256: str = "",
) -> str:
    """Install/refresh one shipped or selected package and record its source."""
    integration_id = str(manifest["id"])
    store = getattr(registry, "store", None)
    if store is None or source not in {"bundled", "package"}:
        # Do not mutate the integration registry if this installer cannot
        # persist the corresponding provenance record.
        return "unavailable"
    policy = "confirm"
    catalogue_match = False
    if source == "package":
        from app.services.integration_install_policy import catalogue_status, current_install_policy

        policy = current_install_policy()
        if not artifact_sha256:
            try:
                from importlib.metadata import distribution as get_distribution
                artifact_sha256 = _distribution_wheel_sha256(get_distribution(distribution)) if distribution else ""
            except Exception:
                artifact_sha256 = ""
        if not installed_sha256:
            try:
                from importlib.metadata import distribution as get_distribution
                installed_sha256 = _distribution_file_set_sha256(get_distribution(distribution)) if distribution else ""
            except Exception:
                installed_sha256 = ""
        state = catalogue_status(
            store,
            integration_id=integration_id,
            version=str(manifest.get("version") or distribution_version),
            artifact_kind="package",
            sha256=artifact_sha256,
        )
        catalogue_match = state == "matched"
    try:
        existing = registry.get(integration_id)
    except PlatformNotFoundError:
        existing = None
    except Exception:
        return "unavailable"

    if source == "package" and policy == "catalogue-only" and not catalogue_match and existing is None:
        return "policy_blocked"

    if existing is None:
        try:
            registry.register(manifest)
            outcome = "installed"
        except PlatformConflictError:
            # Another process installed it between the check and the write.
            outcome = "present"
        except Exception:
            return "unavailable"
        source_changed = outcome == "installed"
    else:
        stored = existing.get("manifest")
        if isinstance(stored, dict) and _declaration_matches(
            stored, manifest,
            lynewright_version=str(getattr(registry, "lynewright_version", "0.1.0")),
            sdk_version=str(getattr(registry, "sdk_version", "1.0.0")),
        ):
            outcome = "present"
        else:
            try:
                registry.refresh_manifest(manifest)
                outcome = "refreshed"
            except Exception:
                outcome = "present"
        source_changed = bool(
            existing.get("source") != source
            or existing.get("source_distribution", "") != distribution
            or existing.get("source_version", "") != distribution_version
            or existing.get("source_entrypoint", "") != entrypoint_module
        )

    if not (outcome in {"installed", "refreshed"} or source_changed):
        from app.services.integration_provenance import get_provenance
        prior = get_provenance(store, str(getattr(registry, "tenant_id", "ten_default")), integration_id)
        source_changed = prior is None
        if prior is not None and source == "package":
            # Policy changes and newly verified catalogue metadata update only
            # provenance; the package declaration and operator state stay put.
            source_changed = (
                (catalogue_match and prior.get("kind") != "catalogue")
                or (policy == "open" and prior.get("status") == "awaiting_confirmation")
                or (
                    policy == "confirm" and not catalogue_match
                    and prior.get("kind") == "outside"
                    and not prior.get("confirmed_at")
                    and prior.get("status") != "awaiting_confirmation"
                )
            )

    if outcome in {"installed", "refreshed"} or source_changed:
        tenant_id = str(getattr(registry, "tenant_id", "ten_default"))
        from app.services.integration_provenance import record_provenance

        try:
            if source == "bundled":
                provenance = record_provenance(store, tenant_id, integration_id, kind="bundled")
            else:
                package_name = distribution or "unknown-package"
                package_version = distribution_version or "unknown-version"
                provenance_source = f"package:{package_name}=={package_version}"
                if artifact_sha256:
                    provenance_source += f"; wheel-sha256:{artifact_sha256}"
                elif installed_sha256:
                    provenance_source += f"; installed-files-sha256:{installed_sha256}"
                pending = policy == "confirm" and not catalogue_match
                open_install = policy == "open" and not catalogue_match
                open_confirmed_at = datetime.now(UTC).isoformat() if open_install else None
                provenance = record_provenance(
                    store, tenant_id, integration_id, kind="outside",
                    source=provenance_source,
                    confirmed_by="admin:LYNEWRIGHT_INTEGRATION_PACKAGES" if open_install else None,
                    confirmed_at=open_confirmed_at,
                    entry_version=str(manifest.get("version") or distribution_version),
                    sha256=artifact_sha256 or None,
                    status="awaiting_confirmation" if pending else "current",
                )
                if open_install:
                    from app.services.integration_install_policy import (
                        InstallDecision, build_install_summary, log_install_confirmation, summary_hash,
                    )
                    summary = build_install_summary(
                        manifest, kind="native package",
                        source=provenance_source,
                        sha256=artifact_sha256 or installed_sha256,
                        catalogue_status_value=state,
                    )
                    log_install_confirmation(
                        integration_id=integration_id,
                        actor="admin:LYNEWRIGHT_INTEGRATION_PACKAGES",
                        decision=InstallDecision(
                            "admin:LYNEWRIGHT_INTEGRATION_PACKAGES", open_confirmed_at,
                            summary_hash(summary), "open", False,
                        ),
                    )
            if provenance is None:
                raise RuntimeError("Provenance writer did not return a persisted record.")
        except Exception:
            # A just-created row can be removed safely. For a refreshed
            # declaration, restore the prior manifest while retaining
            # operator-owned enabled/configuration/authorization state.
            try:
                if existing is None and outcome == "installed":
                    registry.uninstall(integration_id)
                elif existing is not None and outcome == "refreshed":
                    previous = existing.get("manifest")
                    if isinstance(previous, dict):
                        registry.refresh_manifest(previous)
            except Exception:
                _LOG.exception("Could not roll back integration %s after provenance write failure.", integration_id)
            _LOG.exception("Could not record provenance for integration %s.", integration_id)
            return "unavailable"
    if source == "package" and policy == "confirm" and not catalogue_match:
        try:
            installed = registry.get(integration_id)
            if installed.get("enabled"):
                registry.disable(integration_id)
        except Exception:
            _LOG.warning("Could not disable unconfirmed outside package %s.", integration_id)
    return outcome

def ensure_discovered_integrations_installed(registry: Any) -> dict[str, str]:
    """Install every discovered shipped package, idempotently."""
    outcome: dict[str, str] = {}
    for item in discover_shipped_manifests(str(getattr(registry, "lynewright_version", "0.1.0"))):
        integration_id = str(item.manifest["id"])
        if (str(getattr(registry, "tenant_id", "ten_default")), integration_id) in _REMOVED_INTEGRATIONS:
            outcome[integration_id] = "removed_until_restart"
            continue
        try:
            existing = registry.get(integration_id)
        except PlatformNotFoundError:
            existing = None
        if item.source == "package" and existing is not None and existing.get("source") not in {"package", "bundled"}:
            message = f"Refusing package integration {integration_id!r}: an existing non-package record owns this id."
            _LOG.warning(message)
            outcome[integration_id] = "id_collision"
            continue
        outcome[integration_id] = ensure_installed(
            registry, item.manifest, source=item.source,
            distribution=item.distribution,
            distribution_version=item.distribution_version,
            entrypoint_module=item.entrypoint_module,
            artifact_sha256=item.artifact_sha256,
            installed_sha256=item.installed_sha256,
        )
        try:
            registry.set_source_metadata(
                integration_id,
                source=item.source,
                distribution=item.distribution,
                version=item.distribution_version,
                entrypoint_module=item.entrypoint_module,
            )
        except Exception as exc:
            _LOG.warning("Could not record source metadata for integration %s: %s", integration_id, type(exc).__name__)
    return outcome


def compile_installed_definitions(registry: Any) -> dict[str, Any]:
    """Compile a runtime definition for every installed integration that can.

    Returns the compiled definitions and the diagnostics for those that could
    not, so a caller can register the former and report the latter. Nothing here
    knows which provider is which: the manifest decides, the compiler validates.
    """
    from app.services.integration_definition_compiler import (
        COMPILED,
        IntegrationDefinitionCompiler,
    )

    compiler = IntegrationDefinitionCompiler(
        lynewright_version=str(getattr(registry, "lynewright_version", "0.1.0")),
        sdk_version=str(getattr(registry, "sdk_version", "1.0.0")),
        integrations=registry,
    )
    definitions: list[Any] = []
    diagnostics: dict[str, str] = {}
    try:
        records = registry.list()
    except Exception:
        return {"definitions": [], "diagnostics": {}}
    discovered = {
        str(item.manifest.get("id")): item
        for item in discover_shipped_manifests(str(getattr(registry, "lynewright_version", "0.1.0")))
    }
    for record in records:
        allowed_roots = None
        if record.get("source") == "package":
            from app.services.integration_provenance import get_provenance
            provenance = None
            try:
                provenance = get_provenance(
                    registry.store,
                    str(getattr(registry, "tenant_id", "ten_default")),
                    str(record.get("id") or ""),
                )
            except Exception:
                pass
            if provenance and provenance.get("status") == "awaiting_confirmation":
                diagnostics[str(record.get("id") or "")] = "awaiting_confirmation:admin_review_required"
                continue
            stored_root = _catalogue_package_root(record, registry, provenance)
            if stored_root:
                # Catalogue wheels were inspected from their verified ZIP and
                # are kept on the managed data volume. They need no API
                # distribution install or LYNEWRIGHT_INTEGRATION_PACKAGES entry.
                allowed_roots = (stored_root,)
            else:
                current = discovered.get(str(record.get("id") or ""))
                same_distribution = current is not None and current.source == "package" and current.distribution.casefold() == str(record.get("source_distribution") or "").casefold()
                if not same_distribution:
                    diagnostics[str(record.get("id") or "")] = "unavailable:package_not_allowlisted_or_discovered"
                    continue
                allowed_roots = (current.package_root,)
        result = compiler.compile(record, allowed_roots=allowed_roots)
        if result.outcome == COMPILED and result.definition is not None:
            definition = result.definition
            if record.get("source") == "package":
                identity_value = {
                    "manifest": record.get("manifest") or {},
                    "distribution": record.get("source_distribution") or "",
                    "distribution_version": record.get("source_version") or "",
                }
                fingerprint = hashlib.sha256(
                    json.dumps(identity_value, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                definition = replace(definition, manifest_fingerprint=fingerprint)
            definitions.append(definition)
        else:
            diagnostics[result.integration_id] = f"{result.outcome}:{result.reason}"
    return {"definitions": definitions, "diagnostics": diagnostics}


def integration_availability(registry: Any) -> dict[str, dict[str, Any]]:
    """Report the four facts that were previously collapsed into one status.

    "Enabled but runtime definition unavailable" was the representation of a
    package that was never executable, which is why it invited someone to go
    build a definition for it. These are separate facts and stay separate:

        available   the package ships in this image (derived, never stored, so
                    it cannot disagree with reality after a deploy removes one)
        enabled     an operator turned it on
        executable  its manifest declares something the compiler can build
        runtime     what compilation actually concluded
    """
    compiled = compile_installed_definitions(registry)
    ready = {definition.integration_id for definition in compiled["definitions"]}
    report: dict[str, dict[str, Any]] = {}
    try:
        records = registry.list()
    except Exception:
        return report
    for record in records:
        integration_id = str(record.get("id") or "")
        diagnostic = compiled["diagnostics"].get(integration_id, "")
        metadata = product_metadata_for(integration_id, str(getattr(registry, "lynewright_version", "0.1.0")))
        report[integration_id] = {
            "available": bool(metadata["available"]),
            "product_role": metadata["product_role"],
            "kind": metadata["kind"],
            "enabled": bool(record.get("enabled")),
            "executable": integration_id in ready,
            "runtime": "compiled" if integration_id in ready else (diagnostic.split(":", 1)[0] or "unknown"),
            "diagnostic": diagnostic,
        }
    return report


def configured_runtime_definition_factories(registry: Any | None = None) -> list[Any]:
    """Factories for every installed integration whose manifest compiles.

    `bootstrap_runtime_definitions` treats anything that is not a definition as
    a factory failure, so each compiled definition is handed back inside a
    closure rather than recompiled on call.
    """
    if registry is None:
        return []
    compiled = compile_installed_definitions(registry)
    return [(lambda built=definition: built) for definition in compiled["definitions"]]


__all__ = [
    "INTEGRATIONS_PACKAGE",
    "FEATURES_PACKAGE",
    "FIRST_PARTY_FEATURE_ID_PREFIX",
    "PRODUCT_KIND_FEATURE",
    "PRODUCT_KIND_INTEGRATION",
    "PRODUCT_ROLE_EXTERNAL_INTEGRATION",
    "PRODUCT_ROLE_FIRST_PARTY_FEATURE",
    "compile_installed_definitions",
    "configured_runtime_definition_factories",
    "discover_shipped_manifests",
    "discover_integration_manifests",
    "integration_package_statuses",
    "ensure_discovered_integrations_installed",
    "ensure_installed",
    "mark_integration_removed_live",
    "clear_integration_removed_live",
    "is_reserved_first_party_feature_id",
    "integration_availability",
    "product_metadata_for",
    "shipped_product_metadata",
]
