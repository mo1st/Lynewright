# Webhook integrations

Choose a webhook integration when another service sends events into Lynewright. It verifies and maps inbound event payloads; it is not an outbound operation that a production-line stage can call.

## Settings and credentials

Declare editable non-secret fields in `settings_schema`. The webhook verification block names a credential ref; enter that secret under **Settings → Credentials** and configure the integration connection to reference it. Literal secrets are rejected because files and source material are easy to commit or share; a named ref keeps the signing key in credential storage.

<!-- snippet: backend/app/tests/fixtures/integration_files/webhook.yaml -->
<!-- schema: integration-file -->
```yaml
format: lynewright-integration/1
id: lynewright.integration.docs-webhook
name: Job Events
version: 1.0.0
lynewright_version: ^0.1.0
runtime_kind: webhook
endpoint_url: https://api.example.test/hooks/lynewright
credentials:
  - name: events.signing-key
    description: HMAC key stored in Lynewright Settings.
    required: true
settings_schema:
  schema_id: event_settings
  fields:
    - key: event_source
      label: Event source
      type: text
      required: true
webhook:
  event_type: job.completed
  payload_mapping:
    job_id: data.id
    status: data.status
  verification:
    mode: hmac_sha256
    credential_ref: events.signing-key
    signature_header: x-lynewright-signature
    timestamp_header: x-lynewright-timestamp
    delivery_header: x-event-id
    tolerance_seconds: 300
```

## Async status checks and run actions

Webhook declarations do not have an async polling block or callable operations. If an inbound event should start or continue work, handle that through the event integration flow; use an operation integration for outbound work. Custom run actions are currently native-only.

## File passing

The webhook contract maps event fields, not binary file inputs or artifact outputs. Use an operation stage with `lynewright-media`, `lynewright-upload`, or `lynewright-artifact` when a workflow needs managed files. Managed links last 15 minutes by default, no more than 24 hours, and run-bound links are revoked when the run ends.

## Review gate and tests

Webhook declarations cannot supply review items. Put a line review gate after any operation started by your event workflow. There is no dedicated signed-webhook sender in `lynewright-testkit`; use the Lynewright test client and local callback tests to cover signature verification, timestamp tolerance, replay handling, and event mapping. The generic importer can validate the declaration schema.

## Enable and approve

Configure the inbound endpoint and stored signing credential, check the event name and payload mapping, then enable the integration. There are no OpenAPI operations or MCP tools to approve for a webhook declaration. Follow the [security guide](security.md) for endpoint exposure and signing-secret handling.
