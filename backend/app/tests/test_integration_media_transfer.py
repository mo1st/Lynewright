from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.integration_media_transfer import IntegrationMediaTransferService
from app.services.platform_services import PlatformOperationError


def test_media_transfer_leases_stream_bounded_data_and_expire() -> None:
    now=[0.0]; calls=[]
    sdk=SimpleNamespace(integration_id="lynewright.integration.mock", media=SimpleNamespace(get=lambda _id:{"id":"media-1","url":"https://cdn.example.test/a","content_type":"image/png"}, create=lambda value:value), http=SimpleNamespace(validate_url=lambda _url:None, get=lambda url, **kwargs:calls.append((url,kwargs)) or {"body":b"abcdef"}))
    service=IntegrationMediaTransferService(now=lambda:now[0]); lease=service.begin_upload(sdk,"media-1",ttl_seconds=2)
    capabilities=service.capabilities()
    assert capabilities["bounded_memory"] == {"available": True, "max_bytes": 10_000_000}
    assert capabilities["streaming_temp_file"]["available"] is True
    assert list(service.stream_upload(sdk,lease["transfer_id"],chunk_size=2))==[b"ab",b"cd",b"ef"]
    downloaded=service.download_to_media(sdk,url="https://cdn.example.test/b",name="b.png",content_type="image/png")
    assert downloaded["metadata"]["bytes"]==6 and calls[0][1]["max_bytes"]==10_000_000
    now[0]=2
    with pytest.raises(PlatformOperationError,match="expired"): list(service.stream_upload(sdk,lease["transfer_id"]))
