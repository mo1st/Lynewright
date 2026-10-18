"""A deterministic, no-network asynchronous formatter."""
from __future__ import annotations

import threading
import uuid
from typing import Any


class DemoFormatter:
    operation_schemas = {
        "format": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "minLength": 1},
                "text": {"type": "string", "minLength": 1},
            },
            "required": ["topic", "text"],
            "additionalProperties": False,
            "caller_capability": "system:read",
            "read_only": True,
            "risk_class": "READ_ONLY",
            "description": "Queue a deterministic title-cased markdown format job.",
        },
        "status": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "minLength": 1}},
            "required": ["job_id"],
            "additionalProperties": False,
            "caller_capability": "system:read",
            "read_only": True,
            "risk_class": "READ_ONLY",
            "description": "Poll a demo formatting job.",
        },
    }

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def invoke(
        self, operation: str, payload: dict[str, Any], *, invocation_context: Any = None,
    ) -> dict[str, Any]:
        del invocation_context
        if operation == "format":
            topic = str(payload.get("topic") or "").strip()
            text = str(payload.get("text") or "").strip()
            if not topic or not text:
                raise ValueError("A topic and text are required.")
            job_id = uuid.uuid4().hex
            formatted = f"# {topic.title()}\n\n> {text.title()}"
            with self._lock:
                self._jobs[job_id] = {"polls": 0, "formatted": formatted}
            return {"job_id": job_id, "status": "accepted"}
        if operation == "status":
            job_id = str(payload.get("job_id") or "")
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    raise ValueError("Formatting job was not found.")
                job["polls"] += 1
                if job["polls"] == 1:
                    return {"job_id": job_id, "status": "pending"}
                return {
                    "job_id": job_id,
                    "status": "done",
                    "formatted": str(job["formatted"]),
                }
        raise ValueError("Unsupported demo formatter operation.")
