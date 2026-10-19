# OpenAPI integrations

Choose this path when you have an OpenAPI 3.x document and a running HTTP service. Import a file or use **Add from link or docs** with the document URL. Lynewright stores the normalized contract and sends operations through its guarded HTTP client.

## Settings and credentials

An optional `settings_schema` declares fields that Lynewright renders in the integration settings panel. The example below has a workspace setting. Declare credential names and descriptions in the file; enter secret values at **Settings → Credentials**, then attach the named refs to the connection. Literal secrets are rejected because files and source material are easy to commit or share; a named ref keeps the value in credential storage.

<!-- snippet: backend/app/tests/fixtures/integration_files/docs-openapi.yaml -->
<!-- schema: integration-file -->
```yaml
format: lynewright-integration/1
id: lynewright.integration.docs-reports
name: Reports API
version: 1.0.0
lynewright_version: ^0.1.0
runtime_kind: openapi
endpoint_url: https://api.example.test
credentials:
  - name: reports.primary
    description: Reports API credential stored in Lynewright Settings.
    required: true
settings_schema:
  schema_id: reports_settings
  fields:
    - key: workspace_id
      label: Workspace ID
      type: text
      required: true
      description: Workspace used by report operations.
openapi:
  inline:
    openapi: 3.0.3
    info:
      title: Reports API
      version: 1.0.0
    paths:
      /reports:
        post:
          operationId: createReport
          requestBody:
            required: true
            content:
              multipart/form-data:
                schema:
                  type: object
                  properties:
                    attachment:
                      type: string
                      format: binary
                  required: [attachment]
          responses:
            '200':
              description: Report created
              content:
                application/json:
                  schema:
                    type: object
                    properties:
                      artifact_url:
                        type: string
                        format: uri
                    required: [artifact_url]
operations:
  - operation_id: createReport
    enabled: false
    description: Create a report from one managed attachment.
    input_hint:
      media:
        body.attachment: lynewright-upload
    output_hint:
      media:
        artifact_url: lynewright-artifact
```

`enabled: false` leaves this operation unapproved. After inspecting the method and schema, an administrator may set it true to request operation approval during import.

## Async status checks

An `async` block links a start operation to a status operation. The line runner submits once, reads the configured job id path, polls the status operation, and finishes when the returned state is terminal. Keep both operations in the declaration and approve both. See the template's `openapi/` starter for a runnable pending-then-done service.

<!-- snippet: backend/app/tests/fixtures/integration_files/async-openapi.yaml -->
<!-- schema: integration-file -->
```yaml
format: lynewright-integration/1
id: lynewright.integration.fixture-async-openapi
name: Local Async OpenAPI Fixture
version: 1.0.0
lynewright_version: ^0.1.0
runtime_kind: openapi
endpoint_url: http://127.0.0.1:8000
openapi:
  inline:
    openapi: 3.0.3
    info:
      title: Local async fixture API
      version: 1.0.0
    paths:
      /start:
        post:
          operationId: submit
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  properties:
                    text:
                      type: string
                  required: [text]
          responses:
            '200':
              description: Accepted
      /status:
        get:
          operationId: status
          parameters:
            - name: job_id
              in: query
              required: true
              schema:
                type: string
          responses:
            '200':
              description: Job status
operations:
  - operation_id: submit
    enabled: true
    description: Submit an async job.
  - operation_id: status
    enabled: true
    description: Poll an async job.
async:
  operation_id: submit
  status_operation_id: status
  job_id_path: job_id
  status_argument_path: query.job_id
```

## Run actions and operation approval

Review each HTTP method, path, input schema, and required capability in **Integrations**. In a declarative file, `operations[].enabled: true` requests approval of that operation on import; leave it false until reviewed. The integration and connection must still be enabled. Add the operation from the project-scoped catalogue to a line. Declarative files cannot contribute run actions of their own.

## File passing

Use `input_hint.media` to tell Lynewright how a file field is delivered. `lynewright-media` gives the service a scoped, short-lived GET link; `lynewright-upload` sends a managed file as multipart data. Use `output_hint.media` with `lynewright-artifact` for returned file links that Lynewright should ingest as managed artifacts. Link lifetime defaults to 15 minutes, is capped at 24 hours, and links issued for a run are revoked when that run ends. A line's `gate:review` can show those media previews in **Reviews**.

## Review gate and tests

Review items are not supplied by the declarative integration. Put a line `gate:review` after the operation and show the output or media refs you want a person to inspect. Run the OpenAPI starter's `lynewright-testkit` tests with its local service; they cover import, operation approval, sync invocation, and async polling through a line.

## Enable and approve

Import the validated file, configure a connection for the correct project, grant only required capabilities, and approve the minimum operation set. For an async line, enable the start and poll operations. For private endpoints, follow the named internal-endpoint setup in the [security guide](security.md); do not make the SSRF guard permissive.

The settings and file hints example is maintained in [docs-openapi.yaml](../../backend/app/tests/fixtures/integration_files/docs-openapi.yaml); the async example above is included from [async-openapi.yaml](../../backend/app/tests/fixtures/integration_files/async-openapi.yaml).

## Five-minute walkthrough

1. In `templates/starters/openapi/`, run `python -m pip install -r requirements-test.txt`, then `python -m uvicorn example_openapi.app:app --host 127.0.0.1 --port 8000`. Keep the service reachable from the Lynewright API. If Lynewright runs in Docker, bind it to a reachable host address and configure a named internal endpoint for the origin the API container can reach.
2. In **Integrations → Integration file**, choose **Import file** and select `openapi/lynewright-integration.yaml` from `templates/starters/`. That starter lists `health`, `startJob`, and `jobStatus`; operations are off unless the file says `enabled: true`; you still approve them in Integrations.
3. Configure the primary connection with the reachable service endpoint, grant `network:use`, and enable the integration and connection. Confirm that only the intended operations are approved.
4. In **Lines**, create a line with the approved `health` operation, then add a `gate:review` stage whose caption references `{{stages.health.output.status}}`. Save and run it.
5. In **Reviews**, approve the waiting health result. Open **Runs** and confirm the run is done.

`python tools/docs/walkthrough_openapi.py` automates these same service, import, operation, line, and review steps with a fresh isolated Lynewright data directory.
