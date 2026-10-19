from __future__ import annotations

import hashlib
import secrets
import struct
import zlib
from typing import Any


def _chunk(kind: bytes, body: bytes) -> bytes:
    payload = kind + body
    return struct.pack(">I", len(body)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)


def _png(style: str) -> bytes:
    color = (60, 120, 220) if style == "calm" else (245, 150, 35)
    row = b"\x00" + bytes(color) * 8
    pixels = row * 8
    return b"\x89PNG\r\n\x1a\n" + b"".join((
        _chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)),
        _chunk(b"IDAT", zlib.compress(pixels, level=9)),
        _chunk(b"IEND", b""),
    ))


class ExampleMediaProvider:
    operation_schemas = {
        "render": {
            "type": "object",
            "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["prompt"],
            "additionalProperties": False,
            "caller_capability": "media:create",
            "read_only": True,
            "risk_class": "READ_ONLY",
            "description": "Create a small managed PNG asynchronously.",
        },
        "status": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "minLength": 1}},
            "required": ["job_id"],
            "additionalProperties": False,
            "caller_capability": "media:create",
            "read_only": True,
            "risk_class": "READ_ONLY",
            "description": "Poll a managed PNG render job.",
        },
    }

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._style = "calm"

    def configure(self, configuration: dict[str, Any]) -> None:
        settings = configuration.get("settings") if isinstance(configuration, dict) else None
        style = settings.get("style") if isinstance(settings, dict) else None
        self._style = style if style in {"calm", "bright"} else "calm"

    def invoke(
        self, sdk: Any, operation: str, payload: dict[str, Any], *, invocation_context: Any = None,
    ) -> dict[str, Any]:
        del invocation_context
        if operation == "render":
            prompt = str(payload.get("prompt") or "").strip()
            if not prompt:
                raise ValueError("A prompt is required.")
            job_id = secrets.token_hex(8)
            self._jobs[job_id] = {"polls": 0, "prompt": prompt, "style": self._style}
            return {"job_id": job_id, "status": "accepted"}
        if operation != "status":
            raise ValueError("Unsupported example operation.")

        job_id = str(payload.get("job_id") or "")
        job = self._jobs.get(job_id)
        if job is None:
            raise ValueError("Render job was not found.")
        job["polls"] += 1
        if job["polls"] == 1:
            return {"job_id": job_id, "status": "pending"}

        body = _png(str(job["style"]))
        artifact = sdk.media.upload_artifact(
            name=f"{job['style']}-render.png", content_type="image/png", body=body,
        )
        return {
            "job_id": job_id,
            "status": "done",
            "style": job["style"],
            "media_refs": [artifact],
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
