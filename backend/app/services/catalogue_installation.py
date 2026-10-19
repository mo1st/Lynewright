"""Install artifacts from the last verified signed catalogue."""
from __future__ import annotations

import configparser
from contextlib import contextmanager
from email.parser import Parser
import hashlib
import io
from importlib.resources import files
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit
import zipfile

from app.services.catalogue import CatalogueError, CatalogueService
from app.services.extension_manifest import semver_specifier_set, validate_extension_manifest
from app.services.integration_file import IntegrationFileService
from app.services.integration_provenance import get_provenance, record_provenance
from app.services.platform_services import (
    PlatformConflictError, PlatformNotFoundError, PlatformOperationError,
    PlatformValidationError,
)

MAX_WHEEL_FILES = 2_000
MAX_WHEEL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_ID = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
_MODULE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*$")


def _entry_name(integration_id: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[.-]+", integration_id))


def _matches_catalogue_entry(integration: dict[str, Any], provenance: dict[str, Any] | None, entry: dict[str, Any]) -> bool:
    if not provenance or provenance.get("kind") != "catalogue":
        return False
    if (
        str(integration.get("version") or "") != str(entry.get("version") or "")
        or str(provenance.get("catalogue_entry_id") or "") != str(entry.get("id") or "")
        or str(provenance.get("entry_version") or "") != str(entry.get("version") or "")
    ):
        return False
    if entry.get("kind") in {"file", "package"}:
        return str(provenance.get("sha256") or "").lower() == str(entry.get("sha256") or "").lower()
    if entry.get("kind") == "container":
        expected = str(entry.get("source") or "").rsplit("@", 1)[-1]
        return str(provenance.get("image_digest") or "").lower() == expected.lower()
    return False


def _package_directory(integration: dict[str, Any]) -> tuple[Path, str] | None:
    from app.config import get_settings

    configuration = integration.get("configuration") if isinstance(integration.get("configuration"), dict) else {}
    package = configuration.get("catalogue_package") if isinstance(configuration.get("catalogue_package"), dict) else {}
    directory_value = str(package.get("wheel_directory") or "")
    filename = str(package.get("wheel_filename") or "")
    if not directory_value or not filename or Path(filename).name != filename:
        return None
    root = get_settings().packages_dir.resolve()
    candidate = Path(directory_value)
    if not candidate.is_absolute() or candidate.is_symlink():
        return None
    resolved = candidate.resolve(strict=False)
    if resolved == root or root not in resolved.parents:
        return None
    return resolved, filename


def catalogue_package_files_incomplete(integration: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Return whether the catalogue-owned wheel or runner template is absent or corrupt."""
    directory_data = _package_directory(integration)
    if directory_data is None:
        return True
    directory, filename = directory_data
    wheel = directory / filename
    containerfile = directory / "Containerfile.integration"
    if (
        not directory.is_dir() or wheel.is_symlink() or containerfile.is_symlink()
        or not wheel.is_file() or not containerfile.is_file()
    ):
        return True
    try:
        with wheel.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return True
    return actual.lower() != str(entry.get("sha256") or "").lower()


def catalogue_installation_incomplete(
    integration: dict[str, Any], provenance: dict[str, Any] | None, entry: dict[str, Any],
) -> bool:
    if not _matches_catalogue_entry(integration, provenance, entry):
        return False
    return entry.get("kind") == "package" and catalogue_package_files_incomplete(integration, entry)


def _read_wheel(wheel_source: bytes | Path, *, expected_id: str, expected_version: str) -> tuple[dict[str, Any], str, str, str]:
    """Read a native manifest and dist-info from a wheel without importing it."""
    try:
        archive_source = io.BytesIO(wheel_source) if isinstance(wheel_source, bytes) else wheel_source
        archive = zipfile.ZipFile(archive_source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise CatalogueError("Catalogue package artifact is not a valid wheel ZIP.") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_WHEEL_FILES:
            raise CatalogueError("Catalogue wheel contains an invalid number of files.")
        total_size = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            directory = name.endswith("/")
            parts = name[:-1].split("/") if directory else name.split("/")
            if (
                not name or name.startswith("/") or "\x00" in name
                or any(part in {"", ".", ".."} for part in parts)
                or (len(parts[0]) >= 2 and parts[0][1] == ":")
            ):
                raise CatalogueError("Catalogue wheel contains an unsafe archive path.")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise CatalogueError("Catalogue wheel may not contain symbolic links.")
            total_size += info.file_size
            if total_size > MAX_WHEEL_UNCOMPRESSED_BYTES:
                raise CatalogueError("Catalogue wheel expands beyond the size limit.")

        metadata_files = [item for item in infos if item.filename.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise CatalogueError("Catalogue wheel must contain exactly one dist-info/METADATA file.")
        try:
            metadata = Parser().parsestr(archive.read(metadata_files[0]).decode("utf-8"))
        except UnicodeError as exc:
            raise CatalogueError("Catalogue wheel metadata is not UTF-8.") from exc
        distribution = str(metadata.get("Name") or "").strip()
        distribution_version = str(metadata.get("Version") or "").strip()
        if not distribution or distribution_version != expected_version:
            raise CatalogueError("Catalogue wheel distribution version does not match its signed entry.")
        from packaging.utils import canonicalize_name
        dist_info_directory = metadata_files[0].filename.split("/", 1)[0]
        expected_dist_info = (
            canonicalize_name(distribution).replace("-", "_")
            + "-" + distribution_version + ".dist-info"
        )
        if dist_info_directory.casefold() != expected_dist_info.casefold():
            raise CatalogueError("Catalogue wheel dist-info directory does not match its metadata.")

        entry_points_files = [item for item in infos if item.filename.endswith(".dist-info/entry_points.txt")]
        if len(entry_points_files) != 1:
            raise CatalogueError("Catalogue wheel must declare exactly one Lynewright integration manifest entry point.")
        parser = configparser.ConfigParser()
        try:
            parser.read_string(archive.read(entry_points_files[0]).decode("utf-8"))
            points = parser["lynewright.integrations"]
        except (UnicodeError, configparser.Error, KeyError) as exc:
            raise CatalogueError("Catalogue wheel does not declare a valid Lynewright integration entry point.") from exc
        modules = [str(value).strip() for value in points.values()]
        if len(modules) != 1 or _MODULE.fullmatch(modules[0]) is None or ":" in modules[0]:
            raise CatalogueError("Catalogue wheel must declare one manifest module without importing it.")
        manifest_module = modules[0]
        package_root = manifest_module.rpartition(".")[0]
        if not package_root:
            raise CatalogueError("Catalogue wheel manifest entry point must be inside a package.")
        manifest_path = package_root.replace(".", "/") + "/lynewright-manifest.json"
        matches = [item for item in infos if item.filename == manifest_path]
        if len(matches) != 1:
            raise CatalogueError("Catalogue wheel is missing its package data lynewright-manifest.json.")
        try:
            manifest = json.loads(archive.read(matches[0]).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CatalogueError("Catalogue wheel contains an invalid data manifest.") from exc
        if not isinstance(manifest, dict) or manifest.get("id") != expected_id or manifest.get("version") != expected_version:
            raise CatalogueError("Catalogue wheel integration identity does not match its signed entry.")
        provides = manifest.get("provides")
        if not isinstance(provides, list) or not provides:
            raise CatalogueError("Catalogue native wheel does not declare any operations.")
        for item in provides:
            implementation = str(item.get("implementation") or "") if isinstance(item, dict) else ""
            module, separator, attribute = implementation.partition(":")
            if not separator or (module != package_root and not module.startswith(package_root + ".")) or not attribute:
                raise CatalogueError("Catalogue wheel implementation must stay inside its manifest package.")
        return manifest, distribution, distribution_version, manifest_module


class CatalogueInstaller:
    """Install a verified catalogue artifact through Lynewright's existing registries."""

    def __init__(self, catalogue: CatalogueService, management: Any) -> None:
        self.catalogue = catalogue
        self.management = management

    def _verified_context(self) -> tuple[int, str]:
        row = self.catalogue._cache_row()
        serial = row.get("serial")
        key_id = str(row.get("verified_key_id") or "")
        if serial is None or not key_id:
            raise CatalogueError("No verified signed catalogue is cached.")
        return int(serial), key_id

    def _compatible(self, entry: dict[str, Any]) -> None:
        running = str(getattr(self.management.integrations, "lynewright_version", "0.1.0"))
        try:
            compatible = running in semver_specifier_set(str(entry.get("lynewright_version") or ""))
        except Exception as exc:
            raise CatalogueError("Catalogue entry has an invalid Lynewright compatibility range.") from exc
        if not compatible:
            raise CatalogueError(
                f"Catalogue entry requires Lynewright {entry.get('lynewright_version')}; this host is {running}."
            )

    def _entry(self, integration_id: str, version: str) -> dict[str, Any]:
        if not _ID.fullmatch(integration_id):
            raise PlatformValidationError("Catalogue integration id is invalid.")
        entry = self.catalogue.get(integration_id, version)
        if entry is None:
            raise PlatformNotFoundError("Catalogue entry was not found in the verified index.")
        if "yanked" in entry:
            raise PlatformConflictError("This catalogue version is yanked and cannot be installed.")
        self._compatible(entry)
        return entry

    def _refuse_existing_install(self, entry: dict[str, Any]) -> None:
        try:
            current = self.management.integrations.get(str(entry["id"]))
        except PlatformNotFoundError:
            return
        provenance = get_provenance(self.management.store, self.management.tenant_id, str(entry["id"]))
        if not _matches_catalogue_entry(current, provenance, entry):
            raise PlatformConflictError("An integration with this id is already installed.")
        if catalogue_installation_incomplete(current, provenance, entry):
            raise PlatformConflictError("This installation is incomplete; remove it or repair it.")
        raise PlatformConflictError("Already installed.")

    @contextmanager
    def _artifact(self, entry: dict[str, Any]) -> Iterator[Path]:
        from app.config import get_settings

        kind = str(entry.get("kind") or "")
        settings = get_settings()
        if kind == "file":
            max_bytes = settings.catalogue_file_max_bytes
        elif kind == "package":
            max_bytes = settings.catalogue_wheel_max_bytes
        else:
            raise CatalogueError("Only file and package catalogue artifacts can be downloaded.")
        try:
            with self.catalogue.stream_artifact(
                str(entry.get("source") or ""), max_bytes=max_bytes,
                timeout_seconds=20.0,
            ) as response:
                path = Path(response.get("path") or "")
                size = int(response.get("bytes") or 0)
                if not path.is_file() or size <= 0 or size > max_bytes:
                    raise CatalogueError(f"Catalogue {kind} artifact is empty or exceeds its configured size limit.")
                actual = str(response.get("sha256") or "").lower()
                if actual != str(entry.get("sha256") or "").lower():
                    raise CatalogueError("Catalogue artifact SHA-256 does not match the signed entry.")
                yield path
        except (CatalogueError, PlatformValidationError):
            raise
        except PlatformOperationError as exc:
            # HTTPService deliberately maps transport exceptions to a typed
            # operation error. Only its actual httpx cause is a reachability
            # failure; size, status and storage errors retain their own cause.
            import httpx

            cause: BaseException | None = exc
            transport_error: httpx.HTTPError | None = None
            while cause is not None:
                if isinstance(cause, httpx.HTTPError):
                    transport_error = cause
                    break
                cause = cause.__cause__ or cause.__context__
            if transport_error is None:
                raise
            from app.services.application_logging import log_event

            log_event(
                level="error", category="integration", source="lynewright.catalogue.install",
                message="Catalogue artifact download failed.",
                details={"integration_id": str(entry.get("id") or "")},
                exc=transport_error, tenant_id=str(self.management.tenant_id),
            )
            raise PlatformOperationError(
                "Catalogue artifact could not be reached through the safe HTTP service."
            ) from exc

    def install(self, integration_id: str, version: str, *, internal_endpoint_ref: str = "") -> dict[str, Any]:
        entry = self._entry(integration_id, version)
        self._refuse_existing_install(entry)
        if entry["kind"] == "container":
            result = self._install_container(entry)
        elif entry["kind"] == "file":
            with self._artifact(entry) as artifact_path:
                # File declarations are capped at 1 MiB, so the importer can
                # receive the verified exact bytes without buffering the fetch.
                body = artifact_path.read_bytes()
                result = self._install_file(entry, body, internal_endpoint_ref=internal_endpoint_ref)
        elif entry["kind"] == "package":
            with self._artifact(entry) as artifact_path:
                result = self._install_package(entry, artifact_path)
        else:
            raise CatalogueError("Unsupported catalogue artifact kind.")
        from app.services.production_integrations import clear_integration_removed_live
        clear_integration_removed_live(self.management.tenant_id, integration_id)
        return result

    def repair(self, integration_id: str, version: str) -> dict[str, Any]:
        """Restore missing catalogue package files only after an explicit repair request."""
        entry = self._entry(integration_id, version)
        if entry.get("kind") != "package":
            raise PlatformConflictError("Only catalogue package files can be repaired.")
        try:
            current = self.management.integrations.get(integration_id)
        except PlatformNotFoundError as exc:
            raise PlatformNotFoundError("There is no installed integration to repair.") from exc
        provenance = get_provenance(self.management.store, self.management.tenant_id, integration_id)
        if not _matches_catalogue_entry(current, provenance, entry):
            raise PlatformConflictError("The installed package does not match this catalogue entry.")
        if not catalogue_installation_incomplete(current, provenance, entry):
            raise PlatformConflictError("The catalogue installation is complete; no repair is needed.")

        package_directory = _package_directory(current)
        if package_directory is None:
            raise PlatformOperationError("The stored package path is invalid; remove the integration and reinstall it.")
        target, stored_filename = package_directory
        source_filename = unquote(urlsplit(str(entry.get("source") or "")).path).rsplit("/", 1)[-1]
        if source_filename != stored_filename:
            raise PlatformConflictError("The stored wheel filename does not match the catalogue entry.")

        with self._artifact(entry) as wheel_path:
            manifest, distribution, distribution_version, _manifest_module = _read_wheel(
                wheel_path, expected_id=integration_id, expected_version=version,
            )
            from packaging.utils import canonicalize_name, parse_wheel_filename

            try:
                wheel_name, wheel_version, _build, _tags = parse_wheel_filename(source_filename)
            except Exception as exc:
                raise CatalogueError("Catalogue package source must end in a valid PEP 427 wheel filename.") from exc
            if canonicalize_name(str(wheel_name)) != canonicalize_name(distribution) or str(wheel_version) != distribution_version:
                raise CatalogueError("Catalogue wheel filename does not match its distribution metadata.")
            validate_extension_manifest(
                manifest,
                lynewright_version=str(getattr(self.management.integrations, "lynewright_version", "0.1.0")),
                sdk_version="1.0.0",
            )
            try:
                containerfile_bytes = files("app").joinpath(
                    "resources", "runner", "Containerfile.integration",
                ).read_bytes()
            except OSError as exc:
                raise PlatformOperationError("Packaged runner integration Containerfile is unavailable.") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".catalogue-repair-{integration_id}-", dir=target.parent))
            try:
                staged_wheel = staging / stored_filename
                shutil.copyfile(wheel_path, staged_wheel)
                (staging / "Containerfile.integration").write_bytes(containerfile_bytes)
                target.mkdir(parents=True, exist_ok=True)
                os.replace(staged_wheel, target / stored_filename)
                os.replace(staging / "Containerfile.integration", target / "Containerfile.integration")
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        return self.management.detail(integration_id)

    def _install_file(self, entry: dict[str, Any], body: bytes, *, internal_endpoint_ref: str) -> dict[str, Any]:
        value = IntegrationFileService.parse(body)
        if not isinstance(value, dict) or value.get("id") != entry["id"] or value.get("version") != entry["version"]:
            raise CatalogueError("Catalogue file identity does not match its signed entry.")
        if internal_endpoint_ref:
            from app.services.internal_endpoints import normalize_ref, resolve_internal_endpoint
            ref = normalize_ref(internal_endpoint_ref)
            endpoint = str(value.get("endpoint_url") or "")
            path = urlsplit(endpoint).path.rstrip("/")
            value["endpoint_url"] = resolve_internal_endpoint(ref) + path
            value["internal_endpoint_ref"] = ref
        try:
            result = IntegrationFileService(self.management).import_value(
                value, artifact_bytes=body,
                provenance_source=f"catalogue:{entry['id']}@{entry['version']}",
                provenance_actor=self.management.actor_id,
            )
        except (PlatformValidationError, PlatformOperationError) as exc:
            notes = entry.get("requires_operator_setup")
            message = str(getattr(exc, "message", exc))
            if isinstance(notes, list) and notes and any(
                marker in message.casefold()
                for marker in ("port is not allowed", "safe http service")
            ):
                guidance = " ".join(str(note) for note in notes)
                raise type(exc)(f"{message} Catalogue operator setup: {guidance}") from exc
            raise
        return result["integration"]

    def _catalogue_provenance(self, entry: dict[str, Any], *, sha256: str = "", image_digest: str = "") -> dict[str, Any]:
        serial, key_id = self._verified_context()
        review = entry.get("review") if isinstance(entry.get("review"), dict) else {}
        return record_provenance(
            self.management.store, self.management.tenant_id, str(entry["id"]),
            kind="catalogue", catalogue_entry_id=str(entry["id"]),
            entry_version=str(entry["version"]), sha256=sha256 or None,
            image_digest=image_digest or None, catalogue_serial=serial,
            signing_key_id=key_id, reviewer=str(review.get("reviewer") or ""),
            review_date=str(review.get("date") or ""),
        ) or {}

    def _install_package(self, entry: dict[str, Any], wheel_path: Path) -> dict[str, Any]:
        from app.config import get_settings
        from packaging.utils import canonicalize_name, parse_wheel_filename

        manifest, distribution, distribution_version, manifest_module = _read_wheel(
            wheel_path, expected_id=str(entry["id"]), expected_version=str(entry["version"]),
        )
        wheel_filename = unquote(urlsplit(str(entry.get("source") or "")).path).rsplit("/", 1)[-1]
        try:
            wheel_name, wheel_version, _build, _tags = parse_wheel_filename(wheel_filename)
        except Exception as exc:
            raise CatalogueError("Catalogue package source must end in a valid PEP 427 wheel filename.") from exc
        if canonicalize_name(str(wheel_name)) != canonicalize_name(distribution) or str(wheel_version) != distribution_version:
            raise CatalogueError("Catalogue wheel filename does not match its distribution metadata.")
        validate_extension_manifest(
            manifest,
            lynewright_version=str(getattr(self.management.integrations, "lynewright_version", "0.1.0")),
            sdk_version="1.0.0",
        )
        try:
            self.management.integrations.get(str(entry["id"]))
        except PlatformNotFoundError:
            pass
        else:
            raise PlatformConflictError("An integration with this id is already installed.")

        root = get_settings().packages_dir.resolve()
        target = root / str(entry["id"]) / str(entry["version"])
        if target.exists():
            raise PlatformConflictError("The managed package directory already exists; inspect it before reinstalling.")
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".catalogue-install-{entry['id']}-", dir=root))
        try:
            shutil.copyfile(wheel_path, staging / wheel_filename)
            # The API image contains backend/app but not the repository's runner/
            # directory. Keep the synchronized build template in package data.
            containerfile = files("app").joinpath("resources", "runner", "Containerfile.integration")
            try:
                containerfile_bytes = containerfile.read_bytes()
            except OSError as exc:
                raise PlatformOperationError("Packaged runner integration Containerfile is unavailable.") from exc
            (staging / "Containerfile.integration").write_bytes(containerfile_bytes)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise PlatformConflictError("The managed package directory already exists.")
            staging.rename(target)
            configuration = {"catalogue_package": {
                "wheel_directory": str(target),
                "wheel_filename": wheel_filename,
                "sha256": str(entry["sha256"]),
            }}
            self.management.integrations.register(manifest, configuration=configuration)
            self.management.integrations.set_source_metadata(
                str(entry["id"]), source="package", distribution=distribution,
                version=distribution_version, entrypoint_module=manifest_module,
            )
            self._catalogue_provenance(entry, sha256=str(entry["sha256"]))
            self.management.ensure_runtime_definition_registered(str(entry["id"]))
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(target, ignore_errors=True)
            try:
                target.parent.rmdir()
            except OSError:
                pass
            try:
                self.management.integrations.get(str(entry["id"]))
            except PlatformNotFoundError:
                pass
            else:
                try:
                    self.management.integrations.disable(str(entry["id"]))
                    self.management.integrations.uninstall(str(entry["id"]))
                except Exception:
                    pass
            raise
        return self.management.detail(str(entry["id"]))

    def _install_container(self, entry: dict[str, Any]) -> dict[str, Any]:
        source = str(entry["source"])
        if "@sha256:" not in source:
            raise CatalogueError("Catalogue container source must be pinned by digest.")
        image_digest = source.rsplit("@", 1)[-1]
        if not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", image_digest):
            raise CatalogueError("Catalogue container image digest is malformed.")
        try:
            self.management.integrations.get(str(entry["id"]))
        except PlatformNotFoundError:
            pass
        else:
            raise PlatformConflictError("An integration with this id is already installed.")
        manifest = {
            "schema_version": "1.2", "id": str(entry["id"]), "name": _entry_name(str(entry["id"]))[:120],
            "version": str(entry["version"]), "type": "native",
            "lynewright_version": str(entry["lynewright_version"]), "sdk_version": "1.0.0",
            "requested_capabilities": list((entry.get("permissions") or {}).get("capabilities") or []),
            "contributions": [],
        }
        self.management.integrations.register(
            manifest, configuration={"catalogue_container": {"image": source, "digest": image_digest}},
        )
        try:
            self._catalogue_provenance(entry, image_digest=image_digest)
        except Exception:
            try:
                self.management.integrations.disable(str(entry["id"]))
                self.management.integrations.uninstall(str(entry["id"]))
            except Exception:
                pass
            raise
        return self.management.detail(str(entry["id"]))


__all__ = ["CatalogueInstaller", "MAX_ARTIFACT_BYTES"]
