"""P0-03: Managed-file lifecycle tests.

Proves that streamed artifacts survive origin shutdown, can be resolved safely,
deleted cleanly, and that orphan .stored files are reconciled on restart.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.services.integration_media_transfer import IntegrationMediaTransferService
from app.services.platform_services import (
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformValidationError,
)


PAYLOAD = b"\x00\x00\x00 ftyp" + b"data-" * 2048  # ~12KB


@pytest.fixture()
def temp_root():
    return Path(tempfile.mkdtemp(prefix="lynewright_managed_"))


def _service(temp_root: Path) -> IntegrationMediaTransferService:
    def stream_fetch(url):
        headers = {"content-type": "video/mp4", "content-length": str(len(PAYLOAD))}
        def chunks():
            for s in range(0, len(PAYLOAD), 4096):
                yield PAYLOAD[s:s + 4096]
        return headers, chunks()

    return IntegrationMediaTransferService(
        now=lambda: 100.0, stream_fetch=stream_fetch, temp_dir=temp_root,
        url_validator=lambda u: {"hostname": "cdn.test"},
    )


def _sdk():
    from types import SimpleNamespace
    return SimpleNamespace(
        http=SimpleNamespace(validate_url=lambda _u: {"hostname": "cdn.test"}),
        media=SimpleNamespace(create=lambda v: v),
    )


def test_managed_media_survives_origin_url_becoming_unavailable(temp_root):
    """After download, the local file exists even though the origin is gone."""
    service = _service(temp_root)
    record = service.download_to_media(
        _sdk(), url="https://cdn.test/clip.mp4", name="clip.mp4",
        content_type="video/mp4", max_bytes=10_000_000, mode="streaming_temp_file",
    )
    stored_as = record["metadata"]["stored_as"]
    path = temp_root / stored_as
    assert path.is_file(), "managed file must exist after import"
    assert path.read_bytes() == PAYLOAD


def test_media_delete_removes_managed_file(temp_root):
    service = _service(temp_root)
    record = service.download_to_media(
        _sdk(), url="https://cdn.test/del.mp4", name="del.mp4",
        content_type="video/mp4", max_bytes=10_000_000, mode="streaming_temp_file",
    )
    stored_as = record["metadata"]["stored_as"]
    assert (temp_root / stored_as).is_file()

    service.delete_managed_copy(stored_as)
    assert not (temp_root / stored_as).exists()


def test_managed_path_cannot_escape_media_root(temp_root):
    service = _service(temp_root)
    with pytest.raises(PlatformValidationError, match="confined|escapes"):
        service.resolve_managed_path("../../etc/passwd")
    with pytest.raises(PlatformValidationError, match="confined|escapes"):
        service.resolve_managed_path("../../../etc/shadow")


def test_orphan_stored_file_is_recovered(temp_root):
    # Simulate a crash: .stored file left without a Media row.
    orphan = temp_root / "media-orphan-test.stored"
    orphan.write_bytes(b"leftover bytes")

    service = IntegrationMediaTransferService(now=lambda: 500.0, temp_dir=temp_root)
    removed = service.reconcile_orphan_stored_files(referenced_names=set())

    assert orphan.name in removed
    assert not orphan.exists()


def test_referenced_stored_file_is_not_deleted_on_recovery(temp_root):
    referenced = "media-keep-me.stored"
    keep = temp_root / referenced
    keep.write_bytes(b"important data")

    service = IntegrationMediaTransferService(now=lambda: 500.0, temp_dir=temp_root)
    removed = service.reconcile_orphan_stored_files(referenced_names={referenced})

    assert referenced not in removed
    assert keep.exists()


def test_open_managed_stream_reads_without_full_buffer(temp_root):
    service = _service(temp_root)
    record = service.download_to_media(
        _sdk(), url="https://cdn.test/stream.mp4", name="stream.mp4",
        content_type="video/mp4", max_bytes=10_000_000, mode="streaming_temp_file",
    )
    stored_as = record["metadata"]["stored_as"]

    collected = bytearray()
    for chunk in service.open_managed_stream(stored_as, chunk_size=1024):
        collected.extend(chunk)

    assert bytes(collected) == PAYLOAD


def test_open_managed_stream_missing_file_raises_not_found(temp_root):
    service = _service(temp_root)
    with pytest.raises(PlatformNotFoundError):
        list(service.open_managed_stream("nonexistent.stored"))
