# CLI wrapper integrations

Choose the CLI wrapper when a useful tool has no HTTP API. The wrapper exposes a small OpenAPI service; Lynewright never runs the command itself. The starter is in `templates/cli-wrapper/` and the `templates/starters/cli-wrapper/` directory.

## Settings and credentials

The integration declaration may include a `settings_schema` for generic line settings. Put credentials in **Settings → Credentials** and attach named refs to the wrapper's Lynewright connection. Literal secrets are rejected because integration and command files are easy to commit or share; refs keep values in credential storage. A CLI command draft found by the helper is not executable or enableable in Lynewright; install and harden the wrapper separately.

## Async status checks

The wrapper submits a job and reports its status through a second HTTP operation. Its integration file links those operation IDs in `async`; the line builder can then create an `async_operation`. The declaration below is the real CLI thumbnail wrapper contract.

<!-- snippet: templates/cli-wrapper/lynewright-integration.yaml -->
<!-- schema: integration-file -->
```yaml
format: lynewright-integration/1
id: lynewright.integration.cli-thumbnail
name: CLI Thumbnail
version: 1.0.0
lynewright_version: ^0.1.0
runtime_kind: openapi
endpoint_url: https://example.com
openapi:
  inline:
    openapi: 3.0.3
    info:
      title: Lynewright CLI Wrapper
      version: 1.0.0
    paths:
      /health:
        get:
          operationId: health
          responses:
            "200":
              description: Wrapper health.
      /jobs:
        post:
          operationId: submitJob
          requestBody:
            required: true
            content:
              multipart/form-data:
                schema:
                  type: object
                  required: [args, input]
                  properties:
                    args:
                      type: string
                      description: JSON-encoded command arguments.
                    input:
                      type: string
                      format: binary
          responses:
            "202":
              description: Job accepted.
              content:
                application/json:
                  schema:
                    type: object
                    required: [job_id]
                    properties:
                      job_id: {type: string}
      /jobs/{job_id}:
        get:
          operationId: getJob
          parameters:
            - name: job_id
              in: path
              required: true
              schema: {type: string}
          responses:
            "200":
              description: Job state and output artifact links.
              content:
                application/json:
                  schema:
                    type: object
                    required: [job_id, status, progress, outputs, error, artifacts]
                    properties:
                      job_id: {type: string}
                      status: {type: string, enum: [queued, running, done, failed]}
                      progress:
                        type: array
                        items: {type: string}
                      outputs:
                        type: array
                        items: {type: string}
                      error:
                        type: string
                        nullable: true
                      artifacts:
                        type: array
                        items:
                          type: object
                          required: [name, url, content_type]
                          properties:
                            name: {type: string}
                            url: {type: string, format: uri}
                            content_type: {type: string}
                            media_id: {type: string}
      /jobs/{job_id}/files/{name}:
        get:
          operationId: getOutputFile
          parameters:
            - name: job_id
              in: path
              required: true
              schema: {type: string}
            - name: name
              in: path
              required: true
              schema: {type: string}
          responses:
            "200":
              description: Output file bytes.
              content:
                application/octet-stream:
                  schema:
                    type: string
                    format: binary
operations:
  - operation_id: health
    description: Check wrapper health.
  - operation_id: submitJob
    description: Submit one command with a managed file upload.
    input_hint:
      media:
        body.input: lynewright-upload
  - operation_id: getJob
    description: Read job status and materialize declared output files as managed media.
    output_hint:
      media:
        "artifacts.*.url": lynewright-artifact
  - operation_id: getOutputFile
    description: Download an output file that matched the command output glob.
async:
  operation_id: submitJob
  status_operation_id: getJob
  job_id_path: job_id
  status_argument_path: path.job_id
```

The command executed by that wrapper is fixed by `command.yaml`; request values fill only whole argv entries. The real command configuration is also schema checked:

<!-- snippet: templates/cli-wrapper/command.yaml -->
<!-- schema: cli-command -->
```yaml
# Trusted command configuration: all request placeholders occupy whole argv entries.
argv:
  - ffmpeg
  - -hide_banner
  - -loglevel
  - error
  - -ss
  - "{at_seconds}"
  - -i
  - "{input}"
  - -frames:v
  - "1"
  - -vf
  - "{width_filter}"
  - -y
  - "{output}"
params:
  width:
    type: integer
    min: 16
    max: 1024
  at_seconds:
    type: number
    min: 0
    max: 3600
    default: 0
  width_filter:
    type: string
    derive:
      from: width
      format: "scale={value}:-1"
inputs:
  input:
    extensions: [".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm"]
    max_bytes: 20000000
outputs:
  glob: "*.png"
limits:
  timeout_seconds: 30
  max_output_bytes: 1000000
  max_concurrent_jobs: 2
```

## Run actions and operation approval

The wrapper has no Lynewright-native run actions. Inspect its health, submit, status, and download operations; approve only what the line needs. `submitJob` and `getJob` are required for the async line. The integration does not provide review items; add a line `gate:review` after the result.

## File passing

Use `lynewright-upload` for the managed input file; the wrapper accepts it as multipart data. Mark the returned `artifacts.*.url` as `lynewright-artifact` so Lynewright imports output files as managed media. A `lynewright-media` link is available for services that fetch by URL instead. Links default to 15 minutes, cannot exceed 24 hours, and run-bound links are revoked when the run ends.

## Review gate and tests

Put a `gate:review` after the async stage if a person must inspect the output. Lynewright materializes the wrapper's declared output file as managed media, so the review queue can present a scoped preview. Run the wrapper's security tests, then use `lynewright-testkit` against a local wrapper service to test submit/status, uploads, artifact handling, and the review gate. Testkit invokes Lynewright's normal catalogue path; it does not build the wrapper container for you.

## Enable and approve

Build and run the wrapper with a read-only root, bounded tmpfs, non-root user, dropped capabilities, no-new-privileges, CPU/memory/PID and concurrency limits, and an internal-only network. Do not publish the wrapper port. Configure a named `LYNEWRIGHT_INTERNAL_ENDPOINT__<REF>` for it, create a connection using the same reference, then approve the required operations. See [security](security.md) and the source [command.yaml](../../templates/cli-wrapper/command.yaml).
