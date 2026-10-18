"""The demo imager is discoverable only when the demo is explicitly enabled."""
from __future__ import annotations

import os


DEMO_IMAGER_MANIFEST = {
    "schema_version": "1.2",
    "id": "lynewright.integration.demo-imager",
    "name": "Demo Imager",
    "version": "1.0.0",
    "type": "native",
    "lynewright_version": "0.1.0",
    "sdk_version": "1.0.0",
    "requested_capabilities": ["media:create"],
    "contributions": ["services"],
    "provides": [{
        "contract": "lynewright.demo.imager",
        "implementation": "app.integrations.demo_imager.provider:DemoImager",
        "version": "1.0.0",
        "capabilities": ["generate"],
        "invocation": "sdk_scoped",
    }],
} if os.getenv("LYNEWRIGHT_DEMO", "").strip() == "1" else {}
