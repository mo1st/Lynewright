"""SQLite-safe local backups with one bounded S3-compatible upload step."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
import hmac
import secrets

from app.config import get_settings
from app.services.platform_services import (
    CredentialService,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformValidationError,
    SettingsService,
)


SETTING_KEY = "platform.system.backups"
STATUS_KEY = "platform.system.backups.status"
DEFAULTS: dict[str, Any] = {
    "remote_enabled": False,
    "endpoint": "",
    "region": "us-east-1",
    "bucket": "",
    "prefix": "lynewright",
    "credential_ref": "backup.s3",
    "retention_days": 30,
}
STATUS_DEFAULTS: dict[str, Any] = {
    "last_local_backup": None,
    "last_remote_sync": None,
    "last_error": "",
}
_BUCKET_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?$")
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")


def get_backup_settings() -> dict[str, Any]:
    try:
        value = SettingsService().get(SETTING_KEY).get("value")
    except PlatformNotFoundError:
        value = {}
    merged = dict(DEFAULTS)
    if isinstance(value, dict):
        merged.update(value)
        if "target_ref" in merged:
            merged["credential_ref"] = merged.pop("target_ref")
    return _validate(merged, check_credential=False)


def save_backup_settings(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(payload or {})
    values = _validate(merged, check_credential=True)
    stored = dict(values)
    stored["target_ref"] = stored.pop("credential_ref")
    saved = SettingsService().set({"key": SETTING_KEY, "value": stored})["value"]
    saved["credential_ref"] = saved.pop("target_ref", DEFAULTS["credential_ref"])
    return saved


def get_backup_status() -> dict[str, Any]:
    try:
        value = SettingsService().get(STATUS_KEY).get("value")
    except PlatformNotFoundError:
        value = {}
    status = dict(STATUS_DEFAULTS)
    if isinstance(value, dict):
        status.update(value)
    if not isinstance(status.get("last_local_backup"), dict):
        status["last_local_backup"] = None
    if not isinstance(status.get("last_remote_sync"), dict):
        status["last_remote_sync"] = None
    status["last_error"] = str(status.get("last_error") or "")[:300]
    return status


def _save_backup_status(**changes: Any) -> None:
    status = get_backup_status()
    status.update(changes)
    SettingsService().set({"key": STATUS_KEY, "value": status})


def test_backup_destination() -> dict[str, Any]:
    settings = get_backup_settings()
    if not settings["remote_enabled"]:
        raise PlatformValidationError("Enable remote backups before testing the destination.")
    _validate(settings, check_credential=True)
    credential = _credential(settings["credential_ref"])
    target = _s3_request(settings, credential, method="HEAD", object_key="")
    return {"ok": True, "provider": "s3", "status_code": target["status_code"]}


def run_backup_now() -> dict[str, Any]:
    try:
        local = _create_local_backup()
    except Exception as exc:
        _save_backup_status(last_error=str(exc)[:300])
        raise
    completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _save_backup_status(last_local_backup={**local, "completed_at": completed_at}, last_error="")
    settings = get_backup_settings()
    remote: dict[str, Any] = {"enabled": bool(settings["remote_enabled"]), "ok": None}
    if settings["remote_enabled"]:
        try:
            _validate(settings, check_credential=True)
            credential = _credential(settings["credential_ref"])
            uploaded = _upload_backup(settings, credential, local)
            remote.update(uploaded, ok=True)
            _save_backup_status(last_remote_sync={**remote, "completed_at": completed_at}, last_error="")
        except Exception as exc:
            # The local backup is authoritative and remains successful. Remote
            # failure is returned explicitly; no retry loop or fake success.
            remote.update({"ok": False, "error": str(exc)[:300]})
            _save_backup_status(last_remote_sync={**remote, "completed_at": completed_at}, last_error=remote["error"])
    else:
        _save_backup_status(last_remote_sync={**remote, "completed_at": completed_at}, last_error="")
    return {"ok": remote.get("ok") is not False, "local": local, "remote": remote}


#: Upper bound for the independent remote readback. The outbound HTTP
#: boundary refuses bodies above 10 MiB, so archives larger than this fail
#: closed as oversized instead of being fetched unbounded.
VERIFY_MAX_BYTES = 10_000_000
_VERIFY_OBJECT_KEY_LIMIT = 1024


def verify_remote_backup() -> dict[str, Any]:
    """Independently read back the last uploaded object and compare SHA-256.

    The download goes to a temporary disposable file that is always removed,
    stays inside the configured bucket/prefix, refuses redirects, and enforces
    :data:`VERIFY_MAX_BYTES`. Only status, object metadata, the checksum
    comparison, duration, and a redacted error are returned -- never
    credentials or a signed authorization header. The local archive is never
    modified or removed by verification.
    """
    started = time.monotonic()
    settings = get_backup_settings()
    if not settings["remote_enabled"]:
        raise PlatformValidationError("Enable remote backups before verifying the remote object.")
    _validate(settings, check_credential=True)
    credential = _credential(settings["credential_ref"])
    status = get_backup_status()
    remote = status.get("last_remote_sync")
    object_key = remote.get("object_key") if isinstance(remote, dict) else ""
    if not object_key or not isinstance(object_key, str):
        raise PlatformOperationError("No verified remote upload is recorded for comparison.")
    _check_verify_object_key(settings, object_key)
    local_path = _resolve_verify_local_archive(object_key)
    try:
        local_size = local_path.stat().st_size
    except OSError as exc:
        raise PlatformOperationError("The local archive is not available for comparison.") from exc
    if local_size > VERIFY_MAX_BYTES:
        return _verify_result(False, object_key, None, None, False, started, "The local archive exceeds the bounded remote-verification size limit.")
    try:
        response = _s3_request(settings, credential, method="GET", object_key=object_key, max_bytes=VERIFY_MAX_BYTES)
    except (PlatformOperationError, PlatformValidationError) as exc:
        message = str(exc)[:300]
        if "size limit" in message or "exceeded" in message:
            message = "The remote object exceeds the bounded remote-verification size limit."
        return _verify_result(False, object_key, None, None, False, started, message)
    status_code = int(response.get("status_code") or 0)
    if 300 <= status_code < 400:
        return _verify_result(False, object_key, None, None, False, started, "The remote destination refused verification: redirects are not followed.")
    if status_code >= 400:
        return _verify_result(False, object_key, None, None, False, started, f"S3 destination returned HTTP {status_code}.")
    body = response.get("body") or b""
    if isinstance(body, str):
        body = body.encode()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".lynewright-verify-", dir=str(local_path.parent), delete=False) as stream:
            stream.write(body)
            tmp_path = Path(stream.name)
        remote_sha = _sha256(tmp_path)
        local_sha = _sha256(local_path)
        checksum_match = secrets.compare_digest(remote_sha, local_sha)
        metadata = {"content_type": str(response.get("content_type") or "")[:120]}
        if checksum_match:
            return {**_verify_result(True, object_key, len(body), remote_sha, True, started, ""), **metadata}
        return {**_verify_result(False, object_key, len(body), remote_sha, False, started, "The remote object checksum does not match the local archive."), **metadata}
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _verify_result(ok: bool, object_key: str, size_bytes: int | None, sha256: str | None, checksum_match: bool, started: float, error: str) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "object_key": object_key,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "checksum_match": bool(checksum_match),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "error": str(error or "")[:300],
    }


def _check_verify_object_key(settings: dict[str, Any], object_key: str) -> None:
    """Fail closed unless the recorded key is a bounded archive inside the prefix."""
    if len(object_key) > _VERIFY_OBJECT_KEY_LIMIT or "\\" in object_key:
        raise PlatformValidationError("The recorded remote object key is invalid.")
    parts = object_key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PlatformValidationError("The recorded remote object key is invalid.")
    prefix = str(settings["prefix"]).strip("/")
    if prefix and object_key != prefix and not object_key.startswith(prefix + "/"):
        raise PlatformValidationError("The recorded remote object is outside the configured backup prefix.")
    if not Path(object_key).name.startswith("lynewright-"):
        raise PlatformValidationError("The recorded remote object is not a Lynewright backup archive.")


def _resolve_verify_local_archive(object_key: str) -> Path:
    """Resolve the comparison local archive without allowing path escape."""
    folder = _backup_dir().resolve()
    candidate = (folder / Path(object_key).name).resolve()
    if candidate.parent != folder or not candidate.is_file():
        raise PlatformOperationError("The local archive is not available for comparison.")
    return candidate


def list_local_backups(limit: int = 20) -> list[dict[str, Any]]:
    folder = _backup_dir()
    if not folder.exists():
        return []
    rows: list[dict[str, Any]] = []
    candidates = list(folder.glob("lynewright-*.tar.gz")) + list(folder.glob("lynewright-*.sqlite3"))
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)[: max(1, min(limit, 100))]:
        try:
            rows.append({"name": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256(path), "scope": "local"})
        except OSError:
            continue
    return rows


def _create_local_backup() -> dict[str, Any]:
    source_path = Path(get_settings().database_path).resolve()
    if not source_path.exists():
        raise PlatformOperationError("The application database is not available for backup.")
    folder = _backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = folder / f"lynewright-backup-{stamp}-{secrets.token_hex(4)}.tar.gz"
    temporary = folder / f".{destination.name}.tmp"
    staging = Path(tempfile.mkdtemp(prefix=".lynewright-staging-", dir=str(folder)))
    database_copy = staging / "database" / "lynewright.sqlite3"
    media_stage = staging / "media"
    database_copy.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{quote(source_path.as_posix(), safe='/:')}?mode=ro", uri=True)
    target = sqlite3.connect(str(database_copy))
    try:
        source.backup(target)
        target.commit()
        check = target.execute("PRAGMA integrity_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise PlatformOperationError("Local backup integrity check failed.")
    finally:
        target.close()
        source.close()
    try:
        media_count, media_bytes = _stage_managed_media(media_stage)
        database_checksum = _sha256(database_copy)
        manifest = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "format_version": 1,
            "database_sha256": database_checksum,
            "database_checksum": database_checksum,
            "media_file_count": media_count,
            "media_bytes": media_bytes,
            "source_version": os.getenv("LYNEWRIGHT_APPLICATION_VERSION", "unknown")[:120],
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        with tarfile.open(temporary, mode="w:gz") as archive:
            archive.add(database_copy, arcname="database/lynewright.sqlite3", recursive=False)
            if media_stage.exists():
                for path in sorted(media_stage.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        archive.add(path, arcname=Path("media") / path.relative_to(media_stage), recursive=False)
            archive.add(staging / "manifest.json", arcname="manifest.json", recursive=False)
        temporary.replace(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        temporary.unlink(missing_ok=True)
    result = {"name": destination.name, "size_bytes": destination.stat().st_size, "sha256": _sha256(destination), "media_file_count": media_count, "format": "tar.gz"}
    result["retention"] = _apply_local_retention(get_backup_settings().get("retention_days", 30), destination.name)
    return result


def _stage_managed_media(destination: Path) -> tuple[int, int]:
    """Copy only durable managed media under a fixed, confined root."""
    from app.services.integration_media_transfer import managed_media_root

    root = Path(managed_media_root()).resolve()
    if not root.exists():
        return 0, 0
    destination.mkdir(parents=True, exist_ok=True)
    count = total = 0
    max_files, max_bytes = 20_000, 10 * 1024 * 1024 * 1024
    excluded_dirs = {".cache", "cache", "tmp", "temp", "logs", "log", "node_modules"}
    backup_folder = _backup_dir().resolve()
    # A misconfigured managed-media root must not cause the backup archive (or
    # its staging directory) to recursively include itself.
    try:
        backup_folder.relative_to(root)
        excluded_dirs.add(backup_folder.name)
    except ValueError:
        pass
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current).resolve()
        dirs[:] = [name for name in dirs if name not in excluded_dirs and not name.startswith(".lynewright-staging-") and not (current_path / name).is_symlink()]
        for name in files:
            source = current_path / name
            if source.is_symlink() or not source.is_file():
                continue
            try:
                relative = source.relative_to(root)
            except ValueError as exc:
                raise PlatformOperationError("Managed media path escaped its approved root.") from exc
            size = source.stat().st_size
            count += 1; total += size
            if count > max_files or total > max_bytes:
                raise PlatformOperationError("Managed media exceeds the bounded backup limit.")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return count, total


def _apply_local_retention(retention_days: Any, keep_name: str) -> dict[str, Any]:
    try:
        days = max(1, min(int(retention_days), 3650))
    except (TypeError, ValueError):
        days = 30
    cutoff = time.time() - (days * 86400)
    deleted: list[str] = []
    folder = _backup_dir().resolve()
    if not folder.exists():
        return {"scope": "local", "deleted": deleted, "retention_days": days}
    for path in list(folder.glob("lynewright-*.tar.gz")) + list(folder.glob("lynewright-*.sqlite3")):
        try:
            if path.name == keep_name or path.resolve().parent != folder or path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            deleted.append(path.name)
        except OSError:
            continue
    return {"scope": "local", "deleted": deleted, "retention_days": days}


def _upload_backup(settings: dict[str, Any], credential: dict[str, str], local: dict[str, Any]) -> dict[str, Any]:
    path = _backup_dir() / str(local["name"])
    object_key = "/".join(part for part in (str(settings["prefix"]).strip("/"), path.name) if part)
    result = _s3_request(settings, credential, method="PUT", object_key=object_key, body=path.read_bytes())
    return {"object_key": object_key, "status_code": result["status_code"], "sha256": local["sha256"]}


def _credential(ref: str) -> dict[str, str]:
    def parse(secret: str) -> dict[str, str]:
        try:
            value = json.loads(secret)
        except json.JSONDecodeError as exc:
            raise PlatformValidationError("S3 credential must be JSON with access_key_id and secret_access_key.") from exc
        if not isinstance(value, dict):
            raise PlatformValidationError("S3 credential must be a JSON object.")
        access = str(value.get("access_key_id") or "").strip()
        secret_key = str(value.get("secret_access_key") or "").strip()
        if not access or not secret_key:
            raise PlatformValidationError("S3 credential requires access_key_id and secret_access_key.")
        return {"access_key_id": access, "secret_access_key": secret_key, "session_token": str(value.get("session_token") or "").strip()}
    try:
        return CredentialService().use(ref, lambda secret: {"value": parse(secret)})["value"]
    except PlatformNotFoundError as exc:
        raise PlatformValidationError("The selected backup credential is not configured.") from exc


def _s3_request(settings: dict[str, Any], credential: dict[str, str], *, method: str, object_key: str, body: bytes = b"", max_bytes: int = 1_024) -> dict[str, Any]:
    from app.services.platform_services import HTTPService

    endpoint = str(settings["endpoint"]).rstrip("/")
    parsed = urlsplit(endpoint)
    path = f"/{settings['bucket']}" + (f"/{object_key}" if object_key else "")
    url = endpoint + path
    payload_hash = hashlib.sha256(body).hexdigest()
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = parsed.netloc
    canonical_uri = quote(path, safe="/-_.~")
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if credential.get("session_token"):
        headers["x-amz-security-token"] = credential["session_token"]
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{key}:{headers[key].strip()}\n" for key in sorted(headers))
    canonical_request = "\n".join([method, canonical_uri, "", canonical_headers, signed_headers, payload_hash])
    scope = f"{date_stamp}/{settings['region']}/s3/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    key_date = hmac.new(("AWS4" + credential["secret_access_key"]).encode(), date_stamp.encode(), hashlib.sha256).digest()
    key_region = hmac.new(key_date, str(settings["region"]).encode(), hashlib.sha256).digest()
    key_service = hmac.new(key_region, b"s3", hashlib.sha256).digest()
    signing_key = hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    request_headers = {key: value for key, value in headers.items()}
    request_headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={credential['access_key_id']}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    response = HTTPService().request(url, method=method, headers=request_headers, content=body, max_bytes=max_bytes, timeout_seconds=20)
    if int(response["status_code"]) >= 400:
        raise PlatformOperationError(f"S3 destination returned HTTP {response['status_code']}.")
    return response


def _validate(values: dict[str, Any], *, check_credential: bool) -> dict[str, Any]:
    enabled = values.get("remote_enabled")
    if not isinstance(enabled, bool):
        raise PlatformValidationError("remote_enabled must be boolean.")
    endpoint = str(values.get("endpoint") or "").strip().rstrip("/")
    region = str(values.get("region") or "us-east-1").strip()
    bucket = str(values.get("bucket") or "").strip().lower()
    prefix = str(values.get("prefix") or "").strip("/")
    ref = str(values.get("credential_ref") or "backup.s3").strip()
    try:
        retention = int(values.get("retention_days", 30))
    except (TypeError, ValueError) as exc:
        raise PlatformValidationError("retention_days must be an integer.") from exc
    if enabled:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"}:
            raise PlatformValidationError("S3 endpoint must be an HTTPS origin without embedded credentials or a path.")
        if not _BUCKET_RE.fullmatch(bucket):
            raise PlatformValidationError("S3 bucket name is invalid.")
        if not 1 <= len(region) <= 64 or not re.fullmatch(r"^[A-Za-z0-9-]+$", region):
            raise PlatformValidationError("S3 region is invalid.")
        if len(prefix) > 180 or any(part in {".", ".."} for part in prefix.split("/")):
            raise PlatformValidationError("S3 backup prefix is invalid.")
        if not _REF_RE.fullmatch(ref):
            raise PlatformValidationError("S3 credential reference is invalid.")
        if not 1 <= retention <= 3650:
            raise PlatformValidationError("Backup retention must be between 1 and 3650 days.")
        if check_credential:
            _credential(ref)
    return {
        "remote_enabled": enabled,
        "endpoint": endpoint,
        "region": region,
        "bucket": bucket,
        "prefix": prefix,
        "credential_ref": ref,
        "retention_days": retention,
    }


def _backup_dir() -> Path:
    return Path(get_settings().database_path).resolve().parent / "backups"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["DEFAULTS", "SETTING_KEY", "get_backup_settings", "get_backup_status", "list_local_backups", "run_backup_now", "save_backup_settings", "test_backup_destination", "verify_remote_backup"]


if __name__ == "__main__":  # pragma: no cover - host timer/one-shot entrypoint
    print(json.dumps(run_backup_now(), sort_keys=True))
