"""Operator settings declared by the demo writer integration."""

SETTINGS_SCHEMA = {
    "schema_id": "demo_writer",
    "fields": [{
        "key": "tone",
        "label": "Tone",
        "type": "enum",
        "description": "Choose the voice used by the demo writer.",
        "options": ["plain", "friendly"],
        "default": "plain",
    }],
}
