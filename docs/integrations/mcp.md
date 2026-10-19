# MCP integrations

Choose MCP when you already run an MCP server and want its tools available through Lynewright. Lynewright connects to the server, discovers its current tool list, and exposes only approved tools and capability mappings to projects.

## Settings and credentials

An optional `settings_schema` declares generic settings rendered by the integration panel. List named credentials in the integration file and enter their values under **Settings → Credentials**. Add only the refs to the connection. Literal secrets are rejected because files and source material are easy to commit or share; a named ref keeps the value in credential storage.

## Async status checks

An MCP file cannot declare an async start/status pair today. A tool may return immediately; use a native integration if the provider needs native async handling. Do not model an MCP poll loop as an OpenAPI `async` block.

## Run actions and operation approval

Discover tools against the configured connection. Inspect each input schema and safety metadata, approve only the needed tools, and create an MCP contract mapping to the Lynewright capability the line should invoke. The imported file's `operations[].enabled: true` is an explicit request to discover and approve that tool; inspect the target and mapping before putting it in a line. Run actions remain native-only.

<!-- snippet: backend/app/tests/fixtures/integration_files/mcp.yaml -->
<!-- schema: integration-file -->
```yaml
format: lynewright-integration/1
id: lynewright.integration.fixture-mcp
name: Local MCP Fixture
version: 1.0.0
lynewright_version: ^0.1.0
runtime_kind: mcp
endpoint_url: http://127.0.0.1:8000/mcp
credentials: []
operations:
  - tool_name: echo
    description: Return the input from the local MCP stub.
```

## File passing

MCP operations can receive `lynewright-media` links. They cannot receive `lynewright-upload` multipart streams; line validation rejects that combination. If a tool returns a supported media URL, mark it with `lynewright-artifact` so Lynewright stores a project-scoped managed artifact. The standard managed-link lifetime is 15 minutes by default and 24 hours at most; run-bound links are revoked at run end.

## Review gate and tests

The integration cannot supply review items. Add a line `gate:review` after the MCP stage to show the mapped result or media refs in **Reviews**. The companion template's MCP starter uses `lynewright-testkit` to start a local server, discover and approve a tool, map it, and run it in a production line.

## Enable and approve

Create a project-scoped connection, configure its credential refs, grant the required capabilities, discover tools, approve the specific tools, and create the capability mapping. Enable the integration and connection only after checking what the tool can do. Use the generic Integration MCP registry; the removed legacy ad-platform connector is not the MCP integration path.
