from __future__ import annotations

import json
import re
from pathlib import Path

from app.services.line_engine import validate_definition


def test_passing_data_documentation_example_is_a_valid_line() -> None:
    document = Path(__file__).resolve().parents[3] / "docs" / "lines.md"
    text = document.read_text(encoding="utf-8")
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    assert match, "Expected one JSON line definition in the data-passing guide."
    definition = json.loads(match.group(1))
    operations = {
        ("example.generic", "list"): {"schema": {"type": "object", "properties": {}}},
        ("example.generic", "process"): {
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }

    normalized = validate_definition(definition, operation_directory=operations)

    assert [stage["kind"] for stage in normalized["stages"]] == [
        "operation", "map", "for_each", "gate:review", "notify",
    ]
