"""Small deterministic operation provider used by the demo line."""
from __future__ import annotations

from typing import Any


class DemoWriter:
    operation_schemas = {
        "write": {
            "type": "object",
            "properties": {"topic": {"type": "string", "minLength": 1}},
            "required": ["topic"],
            "additionalProperties": False,
            "caller_capability": "system:read",
            "read_only": True,
            "risk_class": "READ_ONLY",
            "description": "Write a deterministic short note about a topic.",
        },
    }

    def __init__(self) -> None:
        self.tone = "plain"

    def configure(self, configuration: dict[str, Any]) -> None:
        settings = configuration.get("settings") if isinstance(configuration, dict) else None
        tone = str(settings.get("tone") or "plain") if isinstance(settings, dict) else "plain"
        self.tone = tone if tone in {"plain", "friendly"} else "plain"

    def invoke(
        self, operation: str, payload: dict[str, Any], *, invocation_context: Any = None,
    ) -> dict[str, Any]:
        del invocation_context
        if operation != "write":
            raise ValueError("Unsupported demo writer operation.")
        topic = str(payload.get("topic") or "").strip()
        if not topic:
            raise ValueError("A topic is required.")
        text = (
            f"Here's a friendly note about {topic}: small, steady steps make progress easier to see."
            if self.tone == "friendly"
            else f"A practical note about {topic}: small, steady steps make progress easier to measure."
        )
        return {"topic": topic, "tone": self.tone, "text": text}
