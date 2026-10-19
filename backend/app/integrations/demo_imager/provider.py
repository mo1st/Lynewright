"""Generate a small deterministic PNG and register it as managed media."""
from __future__ import annotations

import hashlib
import struct
import zlib
from typing import Any



def _png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)


def demo_png() -> bytes:
    width, height = 24, 24
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend((28 + x * 5, 84 + y * 4, 148 + (x + y) * 2))
        rows.append(bytes(row))
    return b"\x89PNG\r\n\x1a\n" + b"".join((
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=9)),
        _png_chunk(b"IEND", b""),
    ))


class DemoImager:
    operation_schemas = {
        "generate": {
            "type": "object", "properties": {}, "required": [], "additionalProperties": False,
            "caller_capability": "media:create", "read_only": True, "risk_class": "READ_ONLY",
            "description": "Generate a small deterministic PNG as managed media.",
            "output_schema": {
                "type": "object",
                "properties": {
                    "media_refs": {"type": "array", "items": {"type": "object"}},
                    "bytes": {"type": "integer"}, "sha256": {"type": "string"},
                },
                "required": ["media_refs", "bytes", "sha256"], "additionalProperties": False,
            },
        },
    }

    def invoke(
        self, sdk: Any, operation: str, payload: dict[str, Any], *, invocation_context: Any = None,
    ) -> dict[str, Any]:
        del payload
        if operation != "generate":
            raise ValueError("Unsupported demo imager operation.")
        del invocation_context
        body = demo_png()
        artifact = sdk.media.upload_artifact(
            name="Lynewright demo image.png", content_type="image/png", body=body,
        )
        return {
            "media_refs": [{
                "media_id": str(artifact["media_id"]), "name": str(artifact["name"]),
                "content_type": "image/png", "url": str(artifact["url"]),
            }],
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        }


__all__ = ["DemoImager", "demo_png"]
