# Lynewright integration starters

Choose the starter that matches what you already have:

| What you have | Starter |
| --- | --- |
| An OpenAPI spec | [`openapi/`](openapi/) |
| An MCP server | [`mcp/`](mcp/) |
| A Python library you trust | [`native/`](native/) |
| A command-line tool only | [`cli-wrapper/`](cli-wrapper/) |

The OpenAPI and MCP starters keep provider code outside Lynewright. The native
starter installs a Python distribution through the `lynewright.integrations`
entry-point group. The CLI starter runs a bounded command in an isolated
container.

**Security:** package manifests are read as `lynewright-manifest.json` data; package modules are not imported by the API. Native implementations run in isolated per-integration runners by default. Use native packages only when you trust their code. See the [Lynewright runner guide](https://github.com/mo1st/Lynewright/blob/main/docs/integrations/runners.md) and [native integration guide](https://github.com/mo1st/Lynewright/blob/main/docs/integrations/native.md).

Each directory is self-contained for copying into a new integration project.
Its README has the local setup steps and its `.github/workflows/ci.yml` checks
the standalone starter tests. The line tests additionally use `lynewright-testkit`
from a local Lynewright checkout.

## Licence

This starter repository is licensed under [Apache-2.0](LICENSE). Generated integrations should declare and choose their own licence. See [CONTRIBUTING.md](CONTRIBUTING.md) for DCO and SPDX guidance.
