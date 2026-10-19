# Add from link or docs

The **Add integration** helper turns a source into an editable, unverified draft. It can read an OpenAPI URL, discover tools from a running MCP endpoint, inspect a public GitHub repository, or draft an integration file from pasted documentation. A GitHub repository is inspected for a spec or project files; it is not treated as a running service.

## What the AI does

When configured, the AI returns data that must match the integration-file or CLI-command schema. It can suggest operation names, descriptions, input/output schemas, settings, and named credential refs. It does not write or run Python, shell commands, or provider code; it does not receive secret values; and it cannot prove an endpoint, schema, safety property, or permission is correct. Treat all generated and fetched material as untrusted. Replace placeholders such as `api.example.invalid` and inspect every method, tool, schema, and capability.

The helper source types are `openapi_url`, `mcp_endpoint`, `github_repo`, and `pasted_docs`. OpenAPI URLs are fetched and parsed directly; MCP endpoints are inspected by live tool discovery. Those two paths do not need AI. A GitHub repository is searched for an OpenAPI file first; the AI is used to draft a CLI command only when repository files look like a command-line project. Pasted API docs are sent to the configured AI drafter. The MCP source must be a running endpoint. For private services, configure a named internal endpoint as described in [security](security.md). The helper refuses URL credentials, query strings, fragments, and path traversal; download the document and paste it, or provide a clean URL.

## Probe before enable

For OpenAPI and MCP integration-file drafts, choose one enabled operation or tool and successfully probe it before enabling the draft. The probe catches an unreachable endpoint or a method/tool mismatch before the integration is activated; Lynewright blocks enable until the exact current draft has a passing probe. The probe is read-only where the operation supports that property; it can still call the remote service, so choose a harmless operation and use a test environment. Editing the draft invalidates the probe. Re-probe the exact final draft, and keep the probed operation enabled. A passed probe proves only that one request worked; it is not a security review or an approval of every operation.

The helper can draft CLI `command.yaml` from a GitHub repository, but Lynewright currently cannot probe or enable that draft. Deploy and harden the wrapper separately, then import its OpenAPI integration declaration. See the [CLI wrapper guide](cli-wrapper.md).

## Settings, credentials, async, and files

Review the draft's `settings_schema`; store secret values under **Settings → Credentials** and use only named refs in the file and connection. OpenAPI drafts can declare an async status operation. MCP drafts cannot declare async polling. File input and output hints use `lynewright-media`, `lynewright-upload`, and `lynewright-artifact` as described in the kind-specific pages. Do not enable an operation just because the AI proposed it.

## Review and test

The draft helper does not invent line review policy. After importing an enabled integration and approved operation/tool, add a line `gate:review` to show output or managed-media previews in **Reviews**. Use the matching starter with `lynewright-testkit` against a local or test service before connecting production data.
