"""The demo inspector is discoverable only when the demo is explicitly enabled."""
from __future__ import annotations

import os


DEMO_INSPECTOR_MANIFEST = {
    "schema_version": "1.2",
    "id": "lynewright.integration.demo-inspector",
    "name": "Demo Inspector",
    "version": "1.0.0",
    "type": "native",
    "lynewright_version": "0.1.0",
    "sdk_version": "1.0.0",
    "requested_capabilities": ["network:use"],
    "contributions": ["services"],
    "provides": [{
        "contract": "lynewright.demo.inspector",
        "implementation": "app.integrations.demo_inspector.provider:DemoInspector",
        "version": "1.0.0",
        "capabilities": ["inspect_link", "inspect_upload"],
        "invocation": "sdk_scoped",
    }],
} if os.getenv("LYNEWRIGHT_DEMO", "").strip() == "1" else {}
