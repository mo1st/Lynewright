# Integrations

Use an existing service or tool as an integration, then add its approved operations to a production line. Start by choosing the kind that matches what you already have.

| What you have | Choose |
| --- | --- |
| An OpenAPI 3.x spec | [OpenAPI](integrations/openapi.md) |
| A running MCP server | [MCP](integrations/mcp.md) |
| Trusted Python code | [Native Python](integrations/native.md) |
| A command-line tool only | [CLI wrapper](integrations/cli-wrapper.md) |
| Only a link or documentation | [Add from link or docs](integrations/add-from-link-or-docs.md) |
| An inbound event callback | [Webhook](integrations/webhook.md) |

Read the [catalogue guide](integrations/catalogue.md) for signed installs and provenance, the [runner guide](integrations/runners.md) for native package execution, and the [security guide](integrations/security.md) before connecting a private service or installing code. [Known limits](integrations/known-limits.md) records current gaps.

Import or add an integration in **Integrations**, enter secret values under **Settings → Credentials**, and approve only the capabilities and operations the line needs. Add a `gate:review` stage wherever a person should inspect output, then test the integration and line with `lynewright-testkit`.

Declarative examples are synchronized from real fixtures and templates by `python tools/docs/sync_snippets.py --write`. Their schema and freshness checks run in `backend/app/tests/test_docs_snippets.py`.
