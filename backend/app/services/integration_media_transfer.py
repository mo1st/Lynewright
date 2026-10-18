"""Bounded media transfer for generic integrations.

Two modes, both hard-capped:

``bounded_memory``
    Legacy in-memory transfer below ``BOUNDED_MEMORY_MAX_BYTES``.
``streaming_temp_file``
    Disk-staged streaming for large artifacts such as short videos. Bytes are
    never fully buffered in memory; the staged file lives in a private
    directory, every path is confinement-checked, and success, failure,
    cancellation, timeout, and restart-orphan paths all clean up.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from app.services.integration_sdk import IntegrationSDK
from app.services.platform_services import (
    MediaService,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
)

def managed_media_root() -> Path:
    """The shared durable-media directory mounted by API and runtime worker."""

    configured = os.getenv("LYNEWRIGHT_MANAGED_MEDIA_DIR", "/app/backend/data/media")
    return Path(configured).resolve()


@dataclass(slots=True)
class _Lease:
    integration_id: str
    media_id: str
    url: str
    content_type: str
    expires_at: float


class IntegrationMediaTransferService:
    """Issues opaque leases with an explicit, extensible transfer capability."""

    BOUNDED_MEMORY_MAX_BYTES = 10_000_000
    MAX_STREAMING_BYTES = 250_000_000
    TransferMode = Literal["bounded_memory", "streaming_temp_file"]

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        stream_fetch: Callable[[str], tuple[dict[str, str], Any]] | None = None,
        url_validator: Callable[[str], dict[str, Any]] | None = None,
        temp_dir: Path | str | None = None,
    ) -> None:
        self.now = now
        self._leases: dict[str, _Lease] = {}
        self._stream_fetch = stream_fetch
        self._url_validator = url_validator
        self._temp_root = Path(temp_dir) if temp_dir is not None else None

    def capabilities(self) -> dict[str, dict[str, Any]]:
        """Describe available transfer levels and their ceilings."""
        return {
            "bounded_memory": {"available": True, "max_bytes": self.BOUNDED_MEMORY_MAX_BYTES},
            "streaming_temp_file": {"available": True, "max_bytes": self.MAX_STREAMING_BYTES},
        }

    def begin_upload(self, sdk: IntegrationSDK, media_id: str, *, ttl_seconds: int = 300) -> dict[str, Any]:
        if not 1 <= ttl_seconds <= 3_600:
            raise PlatformValidationError("Media transfer lease lifetime must be between 1 second and one hour.")
        media = sdk.media.get(media_id)
        sdk.http.validate_url(str(media["url"]))
        transfer_id = f"transfer_{secrets.token_hex(12)}"
        lease = _Lease(sdk.integration_id, media_id, str(media["url"]), str(media["content_type"]), self.now() + ttl_seconds)
        self._leases[transfer_id] = lease
        return {"transfer_id": transfer_id, "media_id": media_id, "content_type": lease.content_type, "expires_in_seconds": ttl_seconds}

    def stream_upload(self, sdk: IntegrationSDK, transfer_id: str, *, chunk_size: int = 64 * 1024, max_bytes: int = BOUNDED_MEMORY_MAX_BYTES, mode: TransferMode = "bounded_memory") -> Iterator[bytes]:
        self._require_mode(mode)
        if not 1 <= chunk_size <= 1_000_000 or not 1 <= max_bytes <= self.BOUNDED_MEMORY_MAX_BYTES:
            raise PlatformValidationError("Invalid media transfer size limit.")
        lease = self._lease(sdk, transfer_id)
        response = sdk.http.get(lease.url, max_bytes=max_bytes)
        body = response["body"]
        for start in range(0, len(body), chunk_size):
            yield body[start:start + chunk_size]

    def download_to_media(
        self,
        sdk: IntegrationSDK,
        *,
        url: str,
        name: str,
        content_type: str = "application/octet-stream",
        max_bytes: int = BOUNDED_MEMORY_MAX_BYTES,
        mode: TransferMode = "bounded_memory",
        expect_content_type: str | None = None,
        timeout_seconds: float = 120.0,
        cancel_check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        self._require_mode(mode)
        if mode == "bounded_memory":
            if not 1 <= max_bytes <= self.BOUNDED_MEMORY_MAX_BYTES:
                raise PlatformValidationError("Invalid media transfer size limit.")
            sdk.http.validate_url(url)
            response = sdk.http.get(url, max_bytes=max_bytes)
            if len(response["body"]) > max_bytes:
                raise PlatformOperationError("Media download exceeded the configured transfer limit.")
            return sdk.media.create({"name": name, "url": url, "content_type": content_type, "metadata": {"transfer": "integration_download", "mode": mode, "bytes": len(response["body"])}})
        return self._download_streaming(
            sdk, url=url, name=name, content_type=content_type, max_bytes=max_bytes,
            expect_content_type=expect_content_type, timeout_seconds=timeout_seconds,
            cancel_check=cancel_check,
        )

    def revoke(self, sdk: IntegrationSDK, transfer_id: str) -> None:
        self._lease(sdk, transfer_id)
        del self._leases[transfer_id]

    def recover_pending_transfers(self) -> list[str]:
        """Remove staging files orphaned by a crashed previous process."""
        removed: list[str] = []
        root = self._temp_root
        if root is None or not root.exists():
            return removed
        for item in sorted(root.glob("*.part")):
            try:
                item.unlink()
                removed.append(item.name)
            except FileNotFoundError:
                continue
        return removed

    # ------------------------------------------------------------------ internals

    def _download_streaming(
        self,
        sdk: IntegrationSDK,
        *,
        url: str,
        name: str,
        content_type: str,
        max_bytes: int,
        expect_content_type: str | None,
        timeout_seconds: float,
        cancel_check: Callable[[], None] | None,
    ) -> dict[str, Any]:
        if not 1 <= max_bytes <= self.MAX_STREAMING_BYTES:
            raise PlatformValidationError("Invalid media transfer size limit.")
        if self._stream_fetch is None:
            raise PlatformOperationError("Streaming media transport is not configured on this host.")
        # SSRF gate first: a blocked destination must never touch the filesystem.
        validator = self._url_validator or (lambda candidate: sdk.http.validate_url(candidate))
        validator(url)

        deadline = self.now() + max(1.0, float(timeout_seconds))
        headers, chunks = self._stream_fetch(url)
        try:
            declared_length = self._declared_length(headers)
            actual_mime = str(headers.get("content-type") or content_type).split(";")[0].strip().lower()
            if expect_content_type is not None and actual_mime != expect_content_type.split(";")[0].strip().lower():
                raise PlatformOperationError(
                    f"Media mime type mismatch: expected {expect_content_type}, provider served {actual_mime}."
                )
            if declared_length is not None and declared_length > max_bytes:
                raise PlatformOperationError("Media download exceeded the configured transfer limit.")

            staging = self._staging_path(None)
            written = 0
            try:
                with staging.open("wb") as handle:
                    for chunk in chunks:
                        if cancel_check is not None:
                            cancel_check()
                        if self.now() > deadline:
                            raise PlatformOperationError("Media streaming timed out before completion.")
                        written += len(chunk)
                        if written > max_bytes:
                            raise PlatformOperationError("Media download exceeded the configured transfer limit.")
                        handle.write(chunk)
                if declared_length is not None and written != declared_length:
                    raise PlatformOperationError("Media body length did not match its declared content-length.")
            except BaseException:
                try:
                    staging.unlink()
                except FileNotFoundError:
                    pass
                raise
            final = self._staging_path(f"media-{secrets.token_hex(12)}.stored")
            staging.replace(final)
            return sdk.media.create({
                "name": name, "url": url, "content_type": content_type,
                "metadata": {
                    "transfer": "integration_download", "mode": "streaming_temp_file",
                    "bytes": written, "stored_as": final.name,
                },
            })
        finally:
            close = getattr(chunks, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    def _staging_path(self, name: str | None) -> Path:
        root = self._temp_root
        if root is None:
            raise PlatformOperationError("Streaming staging directory is not configured on this host.")
        candidate = root / name if name else root / f"media-{secrets.token_hex(12)}.part"
        resolved = candidate.resolve()
        if resolved.parent != root.resolve():
            raise PlatformValidationError("Media staging paths must remain confined to the transfer directory.")
        return resolved

    @staticmethod
    def _declared_length(headers: dict[str, str]) -> int | None:
        raw = headers.get("content-length")
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(str(raw).strip())
        except ValueError:
            return None

    def _lease(self, sdk: IntegrationSDK, transfer_id: str) -> _Lease:
        lease = self._leases.get(transfer_id)
        if lease is None or lease.integration_id != sdk.integration_id:
            raise PlatformNotFoundError("Media transfer lease not found.")
        if self.now() >= lease.expires_at:
            del self._leases[transfer_id]
            raise PlatformOperationError("Media transfer lease has expired.")
        return lease

    def _require_mode(self, mode: TransferMode) -> None:
        if mode in {"bounded_memory", "streaming_temp_file"}:
            return
        raise PlatformValidationError("Unsupported media transfer mode.")

    # --------------------------------------------------- managed-file lifecycle

    def resolve_managed_path(self, stored_as: str) -> Path:
        """Resolve a managed file path under the staging root, refusing traversal."""
        root = self._temp_root
        if root is None:
            raise PlatformOperationError("Streaming staging directory is not configured on this host.")
        candidate = root / stored_as
        resolved = candidate.resolve()
        if resolved.parent != root.resolve():
            raise PlatformValidationError(
                f"managed path '{stored_as}' escapes or is not confined to the media root."
            )
        return resolved

    def open_managed_stream(self, stored_as: str, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Open a managed file for streaming read without buffering into RAM."""
        path = self.resolve_managed_path(stored_as)
        if not path.is_file():
            raise PlatformNotFoundError(f"managed file '{stored_as}' does not exist.")
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    def delete_managed_copy(self, stored_as: str) -> None:
        """Remove the managed file bytes; safe to call on already-deleted files."""
        path = self.resolve_managed_path(stored_as)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def reconcile_orphan_stored_files(self, referenced_names: set[str]) -> list[str]:
        """Remove .stored files that no platform_media row references.

        Called during startup/restart reconciliation. Returns removed names.
        """
        root = self._temp_root
        if root is None or not root.exists():
            return []
        removed: list[str] = []
        for item in sorted(root.glob("*.stored")):
            if item.name in referenced_names:
                continue
            try:
                item.unlink()
                removed.append(item.name)
            except FileNotFoundError:
                continue
        return removed


UPLOAD_MEDIA_SCHEME = "lynewright-upload://"
_MEDIA_TYPE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$", re.IGNORECASE)


def normalize_media_content_type(value: Any) -> str:
    candidate = str(value or "").split(";", 1)[0].strip()
    if len(candidate) <= 120 and _MEDIA_TYPE.fullmatch(candidate):
        return candidate.lower()
    return "application/octet-stream"


class ManagedMediaUpload:
    """A project-checked managed file exposed as a capped upload stream.

    Small files use the transfer service's bounded-memory ceiling. Larger
    files are read lazily from the existing managed-media root through its
    confined streaming mode; no second staging copy is created.
    """

    __slots__ = ("media_id", "name", "content_type", "size", "mode", "_transfer", "_stored_as", "_body")

    def __init__(
        self, *, media_id: str, name: str, content_type: str, stored_as: str,
        transfer: IntegrationMediaTransferService,
    ) -> None:
        path = transfer.resolve_managed_path(stored_as)
        if not path.is_file():
            raise PlatformNotFoundError("Managed media upload was not found.")
        self.size = int(path.stat().st_size)
        if self.size > transfer.MAX_STREAMING_BYTES:
            raise PlatformOperationError("Managed media exceeds the upload transfer limit.")
        self.media_id = str(media_id)
        self.name = str(name or "upload")[:255]
        self.content_type = normalize_media_content_type(content_type)
        self._transfer = transfer
        self._stored_as = str(stored_as)
        if self.size <= transfer.BOUNDED_MEMORY_MAX_BYTES:
            body = b"".join(transfer.open_managed_stream(self._stored_as))
            if len(body) != self.size:
                raise PlatformOperationError("Managed media changed while it was being prepared for upload.")
            self._body: bytes | None = body
            self.mode = "bounded_memory"
        else:
            self._body = None
            self.mode = "streaming_temp_file"

    def iter_chunks(self, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        if self._body is not None:
            for start in range(0, len(self._body), chunk_size):
                yield self._body[start:start + chunk_size]
            return
        remaining = self.size
        for chunk in self._transfer.open_managed_stream(self._stored_as, chunk_size=chunk_size):
            remaining -= len(chunk)
            if remaining < 0:
                raise PlatformOperationError("Managed media changed while it was being uploaded.")
            yield chunk
        if remaining:
            raise PlatformOperationError("Managed media changed while it was being uploaded.")

    def read(self) -> bytes:
        """Read a bounded file for native providers that require bytes."""
        if self.size > self._transfer.BOUNDED_MEMORY_MAX_BYTES:
            raise PlatformOperationError("This managed file is too large for a bounded-memory read.")
        return self._body if self._body is not None else b"".join(self.iter_chunks())


def managed_media_upload(
    store: PlatformStore,
    *,
    tenant_id: str,
    project_id: str,
    media_id: str,
) -> ManagedMediaUpload:
    media = MediaService(store, tenant_id=tenant_id).get(media_id)
    if str(media.get("project_id") or "") != str(project_id or ""):
        raise PlatformNotFoundError("Managed media upload was not found.")
    metadata = media.get("metadata") if isinstance(media.get("metadata"), dict) else {}
    stored_as = str(metadata.get("stored_as") or "")
    if not stored_as:
        raise PlatformValidationError("Only managed media can be passed as an upload.")
    return ManagedMediaUpload(
        media_id=media_id, name=str(media.get("name") or "upload"),
        content_type=str(media.get("content_type") or "application/octet-stream"),
        stored_as=stored_as,
        transfer=IntegrationMediaTransferService(temp_dir=managed_media_root()),
    )
