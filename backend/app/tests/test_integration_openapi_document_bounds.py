from __future__ import annotations

import pytest

from app.services.integration_openapi import MAX_DOCUMENT_BYTES, IntegrationOpenAPIClient
from app.services.platform_services import PlatformValidationError


@pytest.mark.parametrize(
    "document",
    [
        b"x" * (MAX_DOCUMENT_BYTES + 1),
        {"openapi": "3.0.3", "paths": {}, "description": "x" * MAX_DOCUMENT_BYTES},
    ],
    ids=["bytes", "object"],
)
def test_uploaded_or_pasted_openapi_documents_are_size_bounded(document) -> None:
    with pytest.raises(PlatformValidationError, match="1 MB size limit"):
        IntegrationOpenAPIClient(document, None)  # type: ignore[arg-type]
