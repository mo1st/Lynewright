from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.integration_media_transfer import IntegrationMediaTransferService
from app.services.platform_services import PlatformOperationError, PlatformValidationError


CHUNK = 1024 * 1024


def _sdk(validator_error: Exception | None = None) -> SimpleNamespace:
    def validate(url: str) -> dict[str, str]:
        if validator_error is not None:
            raise validator_error
        return {"hostname": "cdn.example.test"}
    return SimpleNamespace(
        integration_id="lynewright.integration.mock",
        media=SimpleNamespace(create=lambda value: value),
        http=SimpleNamespace(validate_url=validate),
    )


class ChunkSource:
    """Fake remote body: declared length may lie; iteration may fail or hang."""

    def __init__(self, payload: bytes, *, chunk_size: int = CHUNK, declared_length: int | None = None, content_type: str = "video/mp4", fail_after: int | None = None, failure: Exception | None = None) -> None:
        self.payload = payload
        self.chunk_size = chunk_size
        self.declared_length = declared_length if declared_length is not None else len(payload)
        self.content_type = content_type
        self.fail_after = fail_after
        self.failure = failure or RuntimeError("remote connection reset mid-stream")

    def __call__(self, url: str) -> tuple[dict[str, str], Any]:
        headers = {"content-type": self.content_type}
        if self.declared_length is not None:
            headers["content-length"] = str(self.declared_length)

        def chunks() -> Any:
            delivered = 0
            for start in range(0, len(self.payload), self.chunk_size):
                if self.fail_after is not None and delivered >= self.fail_after:
                    raise self.failure
                yield self.payload[start:start + self.chunk_size]
                delivered += 1
        return headers, chunks()


@pytest.fixture()
def temp_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="lynewright_stream_test_"))


def _service(temp_root: Path, source: ChunkSource | None = None, *, validator_error: Exception | None = None, now: list[float] | None = None) -> IntegrationMediaTransferService:
    clock = now if now is not None else [1_000.0]

    def stream_fetch(url: str) -> tuple[dict[str, str], Any]:
        if source is None:
            raise AssertionError("stream_fetch must not be called in this test")
        return source(url)

    return IntegrationMediaTransferService(
        now=lambda: clock[0],
        stream_fetch=stream_fetch,
        url_validator=lambda url: _sdk(validator_error).http.validate_url(url),
        temp_dir=temp_root,
    )


def test_streaming_temp_file_mode_is_reported_available(temp_root: Path) -> None:
    service = _service(temp_root)
    capabilities = service.capabilities()
    assert capabilities["streaming_temp_file"] == {
        "available": True,
        "max_bytes": service.MAX_STREAMING_BYTES,
    }
    assert capabilities["bounded_memory"]["available"] is True


def test_bounded_memory_mode_still_caps_at_its_existing_ceiling(temp_root: Path) -> None:
    service = _service(temp_root)
    assert service.capabilities()["bounded_memory"]["max_bytes"] == 10_000_000
    sdk = _sdk()
    with pytest.raises(PlatformValidationError):
        service.download_to_media(
            sdk, url="https://cdn.example.test/v.mp4", name="v.mp4",
            max_bytes=10_000_001, mode="bounded_memory",
        )


def test_realistic_video_above_ten_megabytes_streams_to_temp_file(temp_root: Path) -> None:
    payload = b"\x00mp4" * (3 * CHUNK // 4) + b"\x00" * CHUNK  # 4 MB per repeat below
    payload = (payload * 3)[: 12 * CHUNK]  # 12 MB total, above the 10 MB bounded ceiling
    source = ChunkSource(payload)
    service = _service(temp_root, source)
    record = service.download_to_media(
        _sdk(), url="https://cdn.example.test/short.mp4", name="short.mp4",
        content_type="video/mp4", max_bytes=50_000_000, mode="streaming_temp_file",
    )
    assert record["metadata"]["bytes"] == len(payload)
    assert record["metadata"]["mode"] == "streaming_temp_file"
    assert record["content_type"] == "video/mp4"
    assert not list(temp_root.glob("*.part")), "completed transfer must not leave staging files"


def test_transfer_rejects_payload_above_max_streaming_bytes(temp_root: Path) -> None:
    source = ChunkSource(b"x" * 1024, declared_length=500_000_000)
    service = _service(temp_root, source)
    with pytest.raises(PlatformOperationError, match="limit"):
        service.download_to_media(
            _sdk(), url="https://cdn.example.test/huge.mp4", name="huge.mp4",
            max_bytes=250_000_000, mode="streaming_temp_file",
        )
    assert list(temp_root.glob("*")) == []


def test_transfer_rejects_mime_type_mismatch(temp_root: Path) -> None:
    source = ChunkSource(b"<html>login page</html>", content_type="text/html")
    service = _service(temp_root, source)
    with pytest.raises(PlatformOperationError, match="mime"):
        service.download_to_media(
            _sdk(), url="https://cdn.example.test/fake.mp4", name="fake.mp4",
            content_type="video/mp4", expect_content_type="video/mp4",
            mode="streaming_temp_file",
        )
    assert list(temp_root.glob("*")) == []


def test_lying_content_length_is_caught_by_byte_count_enforcement(temp_root: Path) -> None:
    payload = b"A" * (3 * CHUNK)
    source = ChunkSource(payload, declared_length=1024)  # claims 1 KB, delivers 3 MB
    service = _service(temp_root, source)
    with pytest.raises(PlatformOperationError, match="limit"):
        service.download_to_media(
            _sdk(), url="https://cdn.example.test/liar.mp4", name="liar.mp4",
            max_bytes=2 * CHUNK, mode="streaming_temp_file",
        )
    assert list(temp_root.glob("*.part")) == []


def test_missing_content_length_still_enforces_the_byte_cap(temp_root: Path) -> None:
    source = ChunkSource(b"B" * (5 * CHUNK), declared_length=None)
    service = _service(temp_root, source)
    with pytest.raises(PlatformOperationError, match="limit"):
        service.download_to_media(
            _sdk(), url="https://cdn.example.test/nolen.mp4", name="nolen.mp4",
            max_bytes=2 * CHUNK, mode="streaming_temp_file",
        )
    assert list(temp_root.glob("*.part")) == []


def test_timeout_removes_the_temp_file(temp_root: Path) -> None:
    clock = [1_000.0]
    payload = b"C" * (10 * CHUNK)
    source = ChunkSource(payload)

    slow_source = ChunkSource(payload)

    def fetching(url: str) -> tuple[dict[str, str], Any]:
        headers, chunks = slow_source(url)
        clock[0] += 999.0  # time passes between starting and consuming the stream
        return headers, chunks

    service = _service(temp_root, fetching, now=clock)
    with pytest.raises(PlatformOperationError, match="timed out"):
        service.download_to_media(
            _sdk(), url="https://cdn.example.test/slow.mp4", name="slow.mp4",
            max_bytes=50_000_000, mode="streaming_temp_file", timeout_seconds=30.0,
        )
    assert list(temp_root.glob("*.part")) == []


def test_cancel_mid_stream_removes_the_temp_file(temp_root: Path) -> None:
    payload = b"D" * (10 * CHUNK)
    source = ChunkSource(payload)
    service = _service(temp_root, source)
    consumed = {"chunks": 0}

    def cancel_after_two() -> None:
        consumed["chunks"] += 1
        if consumed["chunks"] >= 2:
            raise PlatformOperationError("media transfer cancelled by caller.")

    with pytest.raises(PlatformOperationError, match="cancelled"):
        service.download_to_media(
            _sdk(), url="https://cdn.example.test/cancel.mp4", name="cancel.mp4",
            max_bytes=50_000_000, mode="streaming_temp_file", cancel_check=cancel_after_two,
        )
    assert consumed["chunks"] == 2
    assert list(temp_root.glob("*.part")) == []


def test_ssrf_blocked_destination_never_creates_a_temp_file(temp_root: Path) -> None:
    class SSRFBlocked(Exception):
        pass

    source = ChunkSource(b"E" * 1024)
    fetch_calls: list[str] = []

    def guarded_fetch(url: str) -> tuple[dict[str, str], Any]:
        fetch_calls.append(url)
        return source(url)

    service = IntegrationMediaTransferService(
        now=lambda: 1_000.0,
        stream_fetch=guarded_fetch,
        url_validator=lambda url: (_ for _ in ()).throw(SSRFBlocked("blocked private address")),
        temp_dir=temp_root,
    )
    with pytest.raises(Exception, match="blocked"):
        service.download_to_media(
            _sdk(), url="http://169.254.169.254/latest/meta-data", name="steal.mp4",
            max_bytes=50_000_000, mode="streaming_temp_file",
        )
    assert fetch_calls == []
    assert list(temp_root.glob("*")) == []


def test_failed_transfer_leaves_no_partial_artifact(temp_root: Path) -> None:
    source = ChunkSource(b"F" * (6 * CHUNK), fail_after=2)
    service = _service(temp_root, source)
    with pytest.raises(RuntimeError, match="reset"):
        service.download_to_media(
            _sdk(), url="https://cdn.example.test/broken.mp4", name="broken.mp4",
            max_bytes=50_000_000, mode="streaming_temp_file",
        )
    assert list(temp_root.glob("*")) == []


def test_orphaned_temp_files_are_cleaned_on_restart(temp_root: Path) -> None:
    orphan = temp_root / "orphaned.part"
    orphan.write_bytes(b"stale bytes from a crashed process")
    recovered_service = IntegrationMediaTransferService(now=lambda: 5_000.0, temp_dir=temp_root)
    removed = recovered_service.recover_pending_transfers()
    assert orphan.name in removed
    assert not orphan.exists()


def test_whole_video_is_never_buffered_in_memory(temp_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tracemalloc

    payload = bytes(12 * CHUNK)  # allocated before tracing starts
    source = ChunkSource(payload, chunk_size=CHUNK)
    service = _service(temp_root, source)

    tracemalloc.start()
    try:
        record = service.download_to_media(
            _sdk(), url="https://cdn.example.test/big.mp4", name="big.mp4",
            content_type="video/mp4", max_bytes=50_000_000, mode="streaming_temp_file",
        )
    finally:
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    assert record["metadata"]["bytes"] == len(payload)
    # The payload is 12 MB; the service must never hold anything close to the
    # bounded-memory ceiling while streaming it through.
    assert len(payload) > 10_000_000
    assert peak < 10_000_000, f"streaming buffered {peak} bytes in memory"


def test_temp_paths_are_confined_and_reject_traversal(temp_root: Path) -> None:
    service = _service(temp_root)
    with pytest.raises(PlatformValidationError, match="confined"):
        service._staging_path("../../etc/passwd.part")
    safe = service._staging_path(None)
    assert safe.resolve().parent == temp_root.resolve()
