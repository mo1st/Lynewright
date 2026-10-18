# Native Python integrations

Choose native Python only when you trust the package author and code. Lynewright reads the companion starter's `example_native/lynewright-manifest.json` as package data; the API does not import `manifest.py` or provider modules during discovery. The Python entry-point module runs in a per-integration runner by default. Setting `LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES=1` opts into trusted API-process execution and shows a critical red **in-process (trusted)** badge.

## Settings and credentials

Declare fields through the manifest's `settings_schema`; Lynewright renders them as integration settings. A field may be a `credential_ref`, but its secret value still belongs in **Settings → Credentials**. Literal secrets are rejected because package configuration can be committed or shared; providers should receive a ref through the SDK and must never persist or return the raw value.

The native wheel's data-only manifest is the source of truth for the API. The entry-point module adapts that JSON for the runner:

<!-- snippet: backend/app/tests/fixtures/native/lynewright-manifest.json -->
<!-- schema: native-manifest -->
```json
{
  "schema_version": "1.2",
  "id": "lynewright.integration.example-native",
  "name": "Example Native Integration",
  "version": "1.0.0",
  "type": "native",
  "lynewright_version": "^0.1.0",
  "sdk_version": "1.0.0",
  "requested_capabilities": ["media:create"],
  "contributions": ["services", "settings"],
  "settings_schema": {
    "schema_id": "example_native_style",
    "fields": [
      {"key": "style", "label": "Render style", "type": "enum", "options": ["calm", "bright"], "default": "calm"}
    ]
  },
  "provides": [{
    "contract": "lynewright.example.native-media",
    "implementation": "example_native.provider:ExampleMediaProvider",
    "version": "1.0.0",
    "capabilities": ["render", "status"],
    "invocation": "sdk_scoped",
    "operation_schemas": {
      "render": {
        "type": "object",
        "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 200}},
        "required": ["prompt"],
        "additionalProperties": false,
        "caller_capability": "media:create",
        "read_only": true,
        "risk_class": "READ_ONLY",
        "description": "Create a small managed PNG asynchronously."
      },
      "status": {
        "type": "object",
        "properties": {"job_id": {"type": "string", "minLength": 1}},
        "required": ["job_id"],
        "additionalProperties": false,
        "caller_capability": "media:create",
        "read_only": true,
        "risk_class": "READ_ONLY",
        "description": "Poll a managed PNG render job."
      }
    },
    "async_operations": {
      "render": {
        "poll_tool": "status",
        "task_id_field": "job_id",
        "poll_argument": "job_id",
        "state_field": "status",
        "active_states": ["accepted", "pending"],
        "terminal_states": ["done", "failed", "cancelled", "canceled", "rejected"]
      }
    }
  }]
}
```

## Async status checks

Native providers can declare async operations in their manifest or runtime definition. Implement the start and status behavior using the integration SDK and keep the project-scoped invocation context. Lynewright's demo formatter is a native example that a line can poll. This differs from declarative MCP and webhook files, which cannot declare polling.

## Run actions and operation approval

Native integrations are the only current source of custom run actions and custom review items. Declare only the actions and review structure the provider actually implements. The Lynewright operator still grants requested capabilities and enables the integration; native entry-point discovery also requires its distribution name in `LYNEWRIGHT_INTEGRATION_PACKAGES`.

## File passing

Use the SDK's managed-media references and upload interface rather than local paths or arbitrary URLs. Native providers create output bytes with `sdk.media.upload_artifact(...)` and return the resulting managed-media reference; do not write directly to Lynewright's managed-media directory. At a declarative operation boundary, `lynewright-media` means a scoped GET link and `lynewright-upload` means a managed upload stream. `lynewright-artifact` is the output hint for declarative operations that return file URLs. Links last 15 minutes by default, at most 24 hours, and run-bound links are revoked when the run ends.

## Review gate and tests

Native code can provide review items and run actions. A line can also place its own `gate:review` after the provider operation; reviewers see the submitted text and eligible managed-media previews in **Reviews**. Use the template's native starter and `lynewright-testkit` to verify package discovery, settings, async execution, and line behavior without a running dashboard.

## Enable and approve

For an outside package, install the wheel into the API environment with `python -m pip install --no-deps /path/to/lynewright_integration_example-1.0.0-py3-none-any.whl` and list its distribution name in `LYNEWRIGHT_INTEGRATION_PACKAGES`. The API reads installed `*.dist-info` metadata and `lynewright-manifest.json` data without importing package modules; it imports the implementation only if the operator opts into `LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES=1`. A catalogue-installed package is stored under `LYNEWRIGHT_PACKAGES_DIR`; its wheel does not need to be installed in the API image. Check the source, version, compatibility, capabilities, and settings, then authorize and enable it. See the [runner guide](runners.md) for the per-integration container flow. Never install an unreviewed native package on a production host.
