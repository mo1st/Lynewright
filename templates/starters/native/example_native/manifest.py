"""Runner-side adapter for the data-only wheel manifest."""
from __future__ import annotations

import json
from pathlib import Path


EXAMPLE_NATIVE_MANIFEST = json.loads(
    Path(__file__).with_name("lynewright-manifest.json").read_text(encoding="utf-8")
)
