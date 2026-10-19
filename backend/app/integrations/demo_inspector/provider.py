"""Inspect the same managed image through a link and a streamed upload."""
from __future__ import annotations

import hashlib
from typing import Any


class DemoInspector:
    operation_schemas = {
        "inspect_link": {
            "type": "object",
            "properties": {"media_url": {"type": "string", "format": "lynewright-media"}},
            "required": ["media_url"], "additionalProperties": False,
            "caller_capability": "network:use", "read_only": True, "risk_class": "READ_ONLY",
            "description": "Fetch a managed media link and report its byte count and hash.",
        },
        "inspect_upload": {
            "type": "object",
            "properties": {"media_file": {"type": "string", "format": "lynewright-upload"}},
            "required": ["media_file"], "additionalProperties": False,
            "caller_capability": "network:use", "read_only": True, "risk_class": "READ_ONLY",
            "description": "Receive a managed file through the bounded or streaming upload path.",
        },
    }

    def invoke(
        self, sdk: Any, operation: str, payload: dict[str, Any], *, invocation_context: Any = None,
    ) -> dict[str, Any]:
        del invocation_context
        if operation == "inspect_link":
            response = sdk.http.request(
                str(payload.get("media_url") or ""), method="GET", max_bytes=10_000_000,
                timeout_seconds=30, internal_endpoint_ref="media",
            )
            if not 200 <= int(response["status_code"]) < 300:
                raise ValueError("The managed media link could not be read.")
            body = bytes(response.get("body") or b"")
            content_type = str(response.get("content_type") or "application/octet-stream").split(";", 1)[0]
        elif operation == "inspect_upload":
            upload = payload.get("media_file")
            reader = getattr(upload, "read", None)
            if not callable(reader):
                raise ValueError("The upload did not resolve to managed media bytes.")
            body = bytes(reader())
            content_type = str(getattr(upload, "content_type", "application/octet-stream"))
        else:
            raise ValueError("Unsupported demo inspector operation.")
        return {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "content_type": content_type}


__all__ = ["DemoInspector"]
