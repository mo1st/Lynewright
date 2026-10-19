"""The demo formatter is discoverable only when the demo is explicitly enabled."""
from __future__ import annotations

import os


DEMO_FORMATTER_MANIFEST = {
    "schema_version": "1.2",
    "id": "lynewright.integration.demo-formatter",
    "name": "Demo Formatter",
    "version": "1.0.0",
    "type": "native",
    "lynewright_version": "0.1.0",
    "sdk_version": "1.0.0",
    "requested_capabilities": [],
    "contributions": ["services"],
    "provides": [{
        "contract": "lynewright.demo.formatter",
        "implementation": "app.integrations.demo_formatter.provider:DemoFormatter",
        "version": "1.0.0",
        "capabilities": ["format", "status"],
        "async_operations": {
            "format": {
                "poll_tool": "status",
                "task_id_field": "job_id",
                "poll_argument": "job_id",
                "state_field": "status",
                "terminal_states": ["completed", "succeeded", "failed", "cancelled", "canceled", "rejected", "done"],
            },
        },
    }],
} if os.getenv("LYNEWRIGHT_DEMO", "").strip() == "1" else {}
