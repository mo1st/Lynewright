"""The demo writer is discoverable only when the demo is explicitly enabled."""
from __future__ import annotations

import os


DEMO_WRITER_MANIFEST = {
    "schema_version": "1.2",
    "id": "lynewright.integration.demo-writer",
    "name": "Demo Writer",
    "version": "1.0.0",
    "type": "native",
    "lynewright_version": "0.1.0",
    "sdk_version": "1.0.0",
    "requested_capabilities": [],
    "contributions": ["services", "settings"],
    "settings_schema": "app.integrations.demo_writer.settings:SETTINGS_SCHEMA",
    "provides": [{
        "contract": "lynewright.demo.writer",
        "implementation": "app.integrations.demo_writer.provider:DemoWriter",
        "version": "1.0.0",
        "capabilities": ["write"],
    }],
} if os.getenv("LYNEWRIGHT_DEMO", "").strip() == "1" else {}
