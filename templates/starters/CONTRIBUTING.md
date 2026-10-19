# Contributing to the integration starters

## Development setup and tests

Use Python 3.12+. Install each starter's `requirements-test.txt`, then run `python -m pytest tests` from `openapi/`, `mcp/`, `native/`, and `cli-wrapper/`. The OpenAPI, MCP, and native starter suites skip their `lynewright`-marked tests unless Lynewright is installed. For the native starter, install its package with `python -m pip install -e native` before running the Lynewright-dependent tests. Keep the starter CI matrix green.

For cross-project checks, install `lynewright-testkit` from a Lynewright checkout and run the marked tests from each starter directory. `tools/release/notices.py` regenerates the dependency inventory.

## Contributions and sensitive data

Use [GitHub Issues](https://github.com/mo1st/Lynewright/issues) for bugs, feature requests, and questions, and pull requests for proposed changes. Report suspected vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

We use the Developer Certificate of Origin (DCO), not a Contributor Licence Agreement. Sign each commit with `git commit -s`. The sign-off must match the commit author. You can use your GitHub-provided noreply email address to keep your personal email private.

Never commit secrets, private hostnames, personal data, local identity lists, private keys, or temporary credentials. The project repository is [mo1st/Lynewright](https://github.com/mo1st/Lynewright).

## Optional private identity scan list

`tools/release/identity_scan.py` reads `LYNEWRIGHT_IDENTITY_SCAN_LIST` only when it is set. Keep this UTF-8 file outside the repository; the scan reports categories and counts, not the matched values. This fake example uses literal tab separators:

```text
identity	Example Person
domain	example.invalid
email	developer@example.invalid
vps-host	vps.example.invalid
vps-ip	192.0.2.10
```

The sample values are reserved examples, not project configuration.

## SPDX headers

For new source files that need a copyright/license notice, use `SPDX-License-Identifier: Apache-2.0`. Do not mass-edit existing or generated files to add headers.
