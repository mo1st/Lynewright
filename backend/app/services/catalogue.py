"""Signed integration catalogue verification and last-good cache."""
from __future__ import annotations

import base64
from contextlib import closing, contextmanager
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import urllib.parse
from typing import Any, Iterator

import jsonschema
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.config import get_settings
from app.services.integration_file import IntegrationFileService
from app.services.extension_manifest import _parse_semver, semver_specifier_set
from app.services.platform_services import (
    HTTPService,
    PlatformServiceError,
)


MAX_INDEX_BYTES = 5 * 1024 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024
FETCH_TIMEOUT_SECONDS = 15.0
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "catalogue.schema.json"
# Development-only key. Release signing material is provisioned separately in K6.
_BUILTIN_DEV_PUBLIC_KEY = "RWQrXeqKUo/v0RnGcOnF+NDjAC1WTrMwZzjR7wAluI0PfYCQOvFIgjWy"
# K6 fills this named slot with the release key. Empty means it trusts nothing.
RELEASE_KEY_PATH = Path(__file__).resolve().parents[1] / "resources" / "catalogue-release-key.pub"
_DEV_WARNING = (
    "Development catalogue key trusted, not for production. "
    "LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY=1 is for local development only."
)
_NO_TRUSTED_KEYS = "no catalogue keys configured"


class CatalogueError(ValueError):
    """A safe, operator-facing catalogue validation failure."""


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_catalogue_cache (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            index_bytes BLOB,
            index_sha256 TEXT NOT NULL DEFAULT '',
            serial INTEGER,
            generated_at TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL DEFAULT '',
            verified_key_id TEXT NOT NULL DEFAULT '',
            last_error TEXT
        )
        """
    )
    db.execute("INSERT OR IGNORE INTO integration_catalogue_cache(id) VALUES(1)")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogueError("Catalogue JSON contains a duplicate object key.")
        result[key] = value
    return result


def _public_key(value: str) -> tuple[bytes, bytes]:
    try:
        decoded = base64.b64decode(value.strip(), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise CatalogueError("A configured catalogue public key is malformed.") from exc
    if len(decoded) != 42 or decoded[:2] != b"Ed":
        raise CatalogueError("A configured catalogue public key is malformed.")
    return decoded[2:10], decoded[10:]


def _release_public_key() -> str:
    try:
        encoded = RELEASE_KEY_PATH.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RuntimeError("The built-in release catalogue key slot is unavailable.") from exc
    except UnicodeError as exc:
        raise RuntimeError("The built-in release catalogue public key is malformed.") from exc
    if not encoded:
        return ""
    try:
        _public_key(encoded)
    except (CatalogueError, ValueError) as exc:
        raise RuntimeError("The built-in release catalogue public key is malformed.") from exc
    return encoded


def validate_catalogue_key_configuration() -> None:
    """Fail startup for invalid built-in release key data or key-id collisions."""
    _verified_key_map()


def _verified_key_map() -> dict[bytes, tuple[str, bool, Ed25519PublicKey]]:
    settings = get_settings()
    keys: dict[bytes, tuple[str, bool, Ed25519PublicKey]] = {}
    builtins: list[tuple[bytes, bytes, str, bool]] = []
    # Parse the dev key even while disabled so an operator key-id collision is
    # rejected instead of silently shadowing or replacing the embedded key.
    for encoded, label, is_dev in (
        (_BUILTIN_DEV_PUBLIC_KEY, "development", True),
        (_release_public_key(), "release", False),
    ):
        if not encoded:
            continue
        try:
            raw_id, raw_public = _public_key(encoded)
        except (CatalogueError, ValueError) as exc:
            raise RuntimeError(f"The built-in {label} catalogue key is malformed.") from exc
        builtins.append((raw_id, raw_public, label, is_dev))

    reserved_ids = {raw_id: label for raw_id, _public, label, _is_dev in builtins}
    for encoded in settings.catalogue_keys:
        try:
            raw_id, raw_public = _public_key(encoded)
        except (CatalogueError, ValueError):
            continue
        if raw_id in reserved_ids:
            raise RuntimeError(
                f"Catalogue key id collision between the built-in {reserved_ids[raw_id]} key "
                "and an operator key; configure unique minisign key ids."
            )
        if raw_id in keys:
            raise RuntimeError("Catalogue operator keys have a duplicate minisign key id.")
        keys[raw_id] = (
            raw_id.hex(), False, Ed25519PublicKey.from_public_bytes(raw_public),
        )

    if settings.catalogue_allow_dev_key:
        for raw_id, raw_public, label, is_dev in builtins:
            if label == "development":
                keys[raw_id] = (
                    f"dev:{raw_id.hex()}", is_dev,
                    Ed25519PublicKey.from_public_bytes(raw_public),
                )
    for raw_id, raw_public, label, is_dev in builtins:
        if label == "release":
            keys[raw_id] = (
                raw_id.hex(), is_dev, Ed25519PublicKey.from_public_bytes(raw_public),
            )
    return keys


def trusted_signing_key_ids() -> set[str]:
    """Return the current trusted signing-key labels, including dev prefixes."""
    return {record[0] for record in _verified_key_map().values()}


def is_signing_key_trusted(key_label: str) -> bool:
    """Check a current label and accept legacy unprefixed dev provenance only if opted in."""
    labels = trusted_signing_key_ids()
    value = str(key_label or "")
    return value in labels or f"dev:{value.removeprefix('dev:')}" in labels


def _verify_minisign(index_bytes: bytes, signature_bytes: bytes) -> tuple[str, bool]:
    try:
        lines = signature_bytes.decode("ascii").splitlines()
        if len(lines) != 4 or not lines[0].startswith("untrusted comment: ") or not lines[2].startswith("trusted comment: "):
            raise CatalogueError("Catalogue signature file is malformed.")
        encoded_signature = base64.b64decode(lines[1], validate=True)
        encoded_global = base64.b64decode(lines[3], validate=True)
        if len(encoded_signature) != 74 or len(encoded_global) != 64:
            raise CatalogueError("Catalogue signature file is malformed.")
        algorithm, key_id, signature = encoded_signature[:2], encoded_signature[2:10], encoded_signature[10:]
        if algorithm not in {b"Ed", b"ED"}:
            raise CatalogueError("Catalogue signature algorithm is unsupported.")
        trusted_comment = lines[2][len("trusted comment: "):].encode("ascii")
    except CatalogueError:
        raise
    except (UnicodeError, ValueError, base64.binascii.Error) as exc:
        raise CatalogueError("Catalogue signature file is malformed.") from exc

    key_record = _verified_key_map().get(key_id)
    if key_record is None:
        raise CatalogueError("Catalogue signature uses an unknown trusted key.")
    key_label, is_dev, public_key = key_record
    signed_bytes = (
        hashlib.blake2b(index_bytes, digest_size=64).digest()
        if algorithm == b"ED"
        else index_bytes
    )
    try:
        public_key.verify(signature, signed_bytes)
        public_key.verify(encoded_global, signature + trusted_comment)
    except InvalidSignature as exc:
        raise CatalogueError("Catalogue signature verification failed.") from exc
    return key_label, is_dev


def _validate_index(index_bytes: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(index_bytes.decode("utf-8"), object_pairs_hook=_unique_object)
    except CatalogueError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogueError("Catalogue index is not valid UTF-8 JSON.") from exc
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8-sig"))
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker(),
        )
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        raise RuntimeError("The committed catalogue schema could not be loaded.") from exc
    errors = sorted(validator.iter_errors(parsed), key=lambda error: list(map(str, error.absolute_path)))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise CatalogueError(f"Catalogue schema error at {path}: {error.message}")
    for position, entry in enumerate(parsed["entries"]):
        try:
            _parse_semver(entry["version"])
        except Exception as exc:
            raise CatalogueError(
                f"Catalogue schema error at entries.{position}.version: invalid semantic-version value."
            ) from exc
        try:
            semver_specifier_set(entry["lynewright_version"])
        except Exception as exc:
            raise CatalogueError(
                f"Catalogue schema error at entries.{position}.lynewright_version: invalid semantic-version range."
            ) from exc
    seen: set[tuple[str, str]] = set()
    for entry in parsed["entries"]:
        identity = (entry["id"], entry["version"])
        if identity in seen:
            raise CatalogueError("Catalogue contains a duplicate (id, version) entry.")
        seen.add(identity)
    secret_errors = IntegrationFileService.secret_errors(parsed)
    if secret_errors:
        locations = ", ".join(item.split(":", 1)[0] for item in secret_errors[:5])
        raise CatalogueError(f"Catalogue contains secret-like literals at {locations}.")
    return parsed


def _validate_catalogue_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise CatalogueError("LYNEWRIGHT_CATALOGUE_URL must be a credential-free HTTPS URL.")
    return urllib.parse.urlunsplit(parts)


def _signature_url(index_url: str) -> str:
    parts = urllib.parse.urlsplit(index_url)
    return urllib.parse.urlunsplit(parts._replace(path=parts.path + ".minisig"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CatalogueService:
    def __init__(self, *, http_service: Any | None = None) -> None:
        settings = get_settings()
        self.url = str(settings.catalogue_url or "").strip()
        self.internal_endpoint_ref = str(settings.catalogue_internal_endpoint_ref or "").strip()
        self.http = http_service or HTTPService()
        if self.internal_endpoint_ref:
            from app.services.internal_endpoints import normalize_ref
            self.internal_endpoint_ref = normalize_ref(self.internal_endpoint_ref)

    def _internal_url(self, url: str) -> str:
        """Map a signed HTTPS catalogue URL to one configured internal origin."""
        from app.services.internal_endpoints import pinned_url, resolve_internal_endpoint

        configured = urllib.parse.urlsplit(_validate_catalogue_url(self.url))
        candidate = urllib.parse.urlsplit(_validate_catalogue_url(url))
        configured_origin = (configured.scheme.lower(), configured.hostname, configured.port or 443)
        candidate_origin = (candidate.scheme.lower(), candidate.hostname, candidate.port or 443)
        if candidate_origin != configured_origin:
            raise CatalogueError("Catalogue artifact URLs must use the configured catalogue origin when an internal endpoint ref is set.")
        return pinned_url(resolve_internal_endpoint(self.internal_endpoint_ref), candidate.path)

    def fetch(self, url: str, *, max_bytes: int, timeout_seconds: float) -> dict[str, Any]:
        """Use public pinned HTTPS, or the deployment-pinned internal endpoint transport."""
        if not 1 <= int(max_bytes) <= HTTPService.MAX_BUFFERED_RESPONSE_BYTES:
            raise CatalogueError("Buffered catalogue fetches are limited to 10 MiB; stream artifacts instead.")
        if self.internal_endpoint_ref:
            response = self.http.request(
                self._internal_url(url), method="GET", max_bytes=max_bytes,
                timeout_seconds=timeout_seconds, internal_endpoint_ref=self.internal_endpoint_ref,
            )
            if int(response.get("status_code") or 0) >= 400:
                raise PlatformServiceError("Catalogue HTTP request failed.")
            return response
        return self.http.get(
            _validate_catalogue_url(url), max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )

    def fetch_artifact(self, url: str, *, max_bytes: int, timeout_seconds: float) -> dict[str, Any]:
        """Fetch small responses only; large catalogue artifacts must stream."""
        return self.fetch(url, max_bytes=max_bytes, timeout_seconds=timeout_seconds)

    @contextmanager
    def stream_artifact(
        self, url: str, *, max_bytes: int, timeout_seconds: float,
    ) -> Iterator[dict[str, Any]]:
        """Stream one signed catalogue artifact through the guarded HTTP layer."""
        validated_url = _validate_catalogue_url(url)
        request_url = validated_url
        endpoint_ref = self.internal_endpoint_ref
        if endpoint_ref:
            request_url = self._internal_url(validated_url)
        with self.http.stream_to_temp_file(
            request_url, max_bytes=max_bytes, timeout_seconds=timeout_seconds,
            internal_endpoint_ref=endpoint_ref,
        ) as response:
            if int(response.get("status_code") or 0) >= 400:
                raise PlatformServiceError("Catalogue artifact request failed.")
            yield response

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        ensure_schema(db)

    def _connection(self) -> sqlite3.Connection:
        from app.database import get_connection, init_db

        init_db()
        return get_connection()

    def _cache_row(self) -> dict[str, Any]:
        with closing(self._connection()) as db:
            row = db.execute(
                "SELECT * FROM integration_catalogue_cache WHERE id=1"
            ).fetchone()
            return dict(row) if row is not None else {}

    def _record_error(self, message: str) -> None:
        with closing(self._connection()) as db, db:
            db.execute(
                "UPDATE integration_catalogue_cache SET last_error=? WHERE id=1",
                (message[:500],),
            )

    def _record_success(self, index_bytes: bytes, index: dict[str, Any], key_label: str) -> None:
        digest = hashlib.sha256(index_bytes).hexdigest()
        with closing(self._connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT serial,index_sha256 FROM integration_catalogue_cache WHERE id=1"
            ).fetchone()
            if row is not None and row["serial"] is not None:
                old_serial = int(row["serial"])
                old_hash = str(row["index_sha256"])
                if index["serial"] < old_serial:
                    raise CatalogueError("Catalogue rollback refused: serial is lower than the cached serial.")
                if index["serial"] == old_serial and digest != old_hash:
                    raise CatalogueError("Catalogue rollback refused: equal serial has different index bytes.")
            db.execute(
                """
                UPDATE integration_catalogue_cache
                SET index_bytes=?,index_sha256=?,serial=?,generated_at=?,fetched_at=?,
                    verified_key_id=?,last_error=NULL
                WHERE id=1
                """,
                (
                    index_bytes, digest, index["serial"], index["generated_at"],
                    _now(), key_label,
                ),
            )
            db.commit()
        from app.services.integration_provenance import reconcile_catalogue_drift
        from app.services.platform_services import PlatformStore
        reconcile_catalogue_drift(
            PlatformStore(), index, int(index["serial"]),
            trusted_signing_key_ids=trusted_signing_key_ids(),
        )

    def refresh(self) -> dict[str, Any]:
        if not _verified_key_map():
            self._record_error(_NO_TRUSTED_KEYS)
            return self.status()
        if not self.url:
            return self.status()
        try:
            index_url = _validate_catalogue_url(self.url)
            index_response = self.fetch(
                index_url, max_bytes=MAX_INDEX_BYTES,
                timeout_seconds=FETCH_TIMEOUT_SECONDS,
            )
            index_bytes = index_response.get("body", b"")
            if not isinstance(index_bytes, bytes) or len(index_bytes) > MAX_INDEX_BYTES:
                raise CatalogueError("Catalogue index exceeds the 5 MiB size limit.")
            signature_response = self.fetch(
                _signature_url(index_url), max_bytes=MAX_SIGNATURE_BYTES,
                timeout_seconds=FETCH_TIMEOUT_SECONDS,
            )
            signature_bytes = signature_response.get("body", b"")
            if not isinstance(signature_bytes, bytes) or len(signature_bytes) > MAX_SIGNATURE_BYTES:
                raise CatalogueError("Catalogue signature exceeds the size limit.")
            key_label, _is_dev = _verify_minisign(index_bytes, signature_bytes)
            parsed = _validate_index(index_bytes)
            self._record_success(index_bytes, parsed, key_label)
        except Exception as exc:
            if isinstance(exc, (CatalogueError, PlatformServiceError)):
                message = str(exc)
            else:
                message = "Catalogue refresh failed."
            try:
                self._record_error(message)
            except Exception:
                pass
            from app.services.application_logging import log_event

            log_event(
                level="warning", category="integration", source="lynewright.catalogue",
                message="Signed catalogue refresh was refused.",
                details={"error": message}, capture_active_exception=False,
            )
        return self.status()

    def startup_refresh(self) -> dict[str, Any]:
        from app.services.application_logging import log_event
        from app.services.integration_provenance import reconcile_catalogue_drift
        from app.services.platform_services import PlatformStore

        status = self.status()
        key_ids = trusted_signing_key_ids()
        if status["dev_key_trusted"]:
            log_event(
                level="warning", category="security", source="lynewright.catalogue",
                message=_DEV_WARNING, capture_active_exception=False,
            )
        if not key_ids:
            log_event(
                level="warning", category="security", source="lynewright.catalogue",
                message="No catalogue signing keys are trusted; catalogue refresh and installs are unavailable.",
                capture_active_exception=False,
            )
            if get_settings().install_policy == "catalogue-only":
                log_event(
                    level="warning", category="security", source="lynewright.catalogue",
                    message="LYNEWRIGHT_INSTALL_POLICY=catalogue-only is active with no trusted catalogue keys; no integrations can be installed.",
                    capture_active_exception=False,
                )
        cache = self._cache_row()
        cached_index = self._index() or {"entries": []}
        reconcile_catalogue_drift(
            PlatformStore(), cached_index, int(cache.get("serial") or 0),
            trusted_signing_key_ids=key_ids,
        )
        return self.refresh()

    def _index(self) -> dict[str, Any] | None:
        row = self._cache_row()
        raw = row.get("index_bytes")
        if not raw:
            return None
        if not is_signing_key_trusted(str(row.get("verified_key_id") or "")):
            return None
        try:
            return json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_unique_object)
        except (CatalogueError, UnicodeError, json.JSONDecodeError):
            return None

    def list_installable(self) -> list[dict[str, Any]]:
        if not self.url:
            return []
        index = self._index()
        if not index:
            return []
        return [entry for entry in index["entries"] if "yanked" not in entry]

    def get(self, integration_id: str, version: str) -> dict[str, Any] | None:
        index = self._index()
        if not index:
            return None
        for entry in index["entries"]:
            if entry["id"] == integration_id and entry["version"] == version:
                return entry
        return None

    def status(self) -> dict[str, Any]:
        row = self._cache_row()
        key_records = _verified_key_map()
        keys = [
            {"id": value[0], "source": "built-in development key" if value[1] else "operator key"}
            for value in key_records.values()
        ]
        dev_key_trusted = any(record[1] for record in key_records.values())
        has_trusted_keys = bool(key_records)
        cached_key_id = str(row.get("verified_key_id") or "")
        cached_key_trusted = bool(row.get("index_bytes")) and is_signing_key_trusted(cached_key_id)
        if not has_trusted_keys:
            state = "untrusted"
            error = _NO_TRUSTED_KEYS
        elif row.get("index_bytes") and not cached_key_trusted:
            state = "error"
            error = "Cached catalogue signing key is no longer trusted."
        elif not self.url:
            state = "offline"
            error = None
        elif row.get("last_error"):
            state = "error"
            error = row.get("last_error")
        elif row.get("index_bytes"):
            state = "ready"
            error = None
        else:
            state = "empty"
            error = None
        return {
            "url": self.url,
            "status": state,
            "trusted_key_ids": keys,
            "dev_key_trusted": dev_key_trusted,
            "dev_key_warning": dev_key_trusted,
            "dev_key_warning_message": _DEV_WARNING if dev_key_trusted else None,
            "verified_key_id": row.get("verified_key_id") or None,
            "serial": row.get("serial") if row.get("index_bytes") else None,
            "generated_at": row.get("generated_at") or None,
            "fetched_at": row.get("fetched_at") or None,
            "last_error": error,
        }


def list_installable() -> list[dict[str, Any]]:
    return CatalogueService().list_installable()


def get_entry(integration_id: str, version: str) -> dict[str, Any] | None:
    return CatalogueService().get(integration_id, version)
