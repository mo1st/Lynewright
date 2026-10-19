# Integration security

## Secrets and credentials

Never put a secret value in an integration file, source URL, repository, command file, test fixture, or AI prompt. Declare a named ref, enter its value under **Settings → Credentials**, and attach that ref to the integration connection or setting. Lynewright stores credential values encrypted and returns metadata, not the raw value. Do not echo secrets in provider results or logs.

The secret-name checks can reject schema properties named `token`, `password`, `secret`, `api_key`, or similar even when those names describe a remote API schema and contain no secret. Rename the schema property when the remote API permits it; otherwise the current importer may reject that document.

## URLs and private services

Lynewright's SSRF guard validates outbound hosts and resolved addresses, blocks private, loopback, link-local, and reserved addresses for public URLs, and pins a request to its checked address. Keep the guard enabled; never weaken or bypass it to make an endpoint work. To reach a deliberately private service, configure a deployment-level name such as `LYNEWRIGHT_INTERNAL_ENDPOINT__REPORTS_API` with the private service origin, then select the same reference when creating the connection or importing the file. The reference is not a secret and does not permit arbitrary user-supplied origins. Keep credentials in credential storage, not in the endpoint URL.

## Catalogue trust

The built-in development signing key is never trusted by default. Production operators configure their own minisign public key in `LYNEWRIGHT_CATALOGUE_KEYS`; keep the private signing key outside the catalogue repository and Lynewright host. To use a private catalogue, set `LYNEWRIGHT_CATALOGUE_URL` and `LYNEWRIGHT_CATALOGUE_KEYS`; if it is private, also set `LYNEWRIGHT_CATALOGUE_INTERNAL_ENDPOINT_REF` and the corresponding `LYNEWRIGHT_INTERNAL_ENDPOINT__<REF>`. The SSRF guard remains enabled.

`LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY=1` is for local development only. It logs a startup warning, displays a persistent dashboard banner, and marks installs from that key as **Catalogue (dev key)**. Do not enable it in production. Provenance identifies catalogue, outside, and bundled installs. If a signing key or entry later becomes untrusted or drifts, Lynewright flags it and does not silently replace or remove the integration.

## Native packages

Native Python distributions contain executable code. Lynewright reads `lynewright-manifest.json` as data without importing package modules. Package code runs in its per-integration runner by default; it runs inside the API only when `LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES=1` explicitly selects trusted in-process execution. That setting bypasses runner isolation and produces a critical red **in-process (trusted)** badge in Integrations. Review the source, provenance, dependencies, and declared capabilities before installation. An allowlist is not a sandbox.

## Outside-install confirmation and policy

Lynewright compares an imported file or package with the last verified index. An id match alone is not enough: the kind, version, and exact artifact hash must match, and a yanked entry is outside. A container match requires the signed image digest. Everything else is outside and follows `LYNEWRIGHT_INSTALL_POLICY`:

- `confirm` (the default) shows a server-built summary with the artifact hash and requests the integration id. Lynewright recomputes the summary hash before installing; a changed summary must be reviewed again.
- `catalogue-only` blocks all outside installs and re-enablement. With no trusted keys, no entry can match, so startup warns and nothing can be installed under this policy.
- `open` skips the confirmation dialog for outside installs. Lynewright records the installing actor and time, logs the policy use, and displays a persistent red dashboard banner.

Outside packages selected by `LYNEWRIGHT_INTEGRATION_PACKAGES` stay disabled with **Awaiting confirmation** under `confirm`. They cannot be enabled, receive a runner token, or get runtime configuration until an administrator confirms the current summary. Catalogue-matched packages do not need that confirmation. With no trusted catalogue keys under `confirm`, files and packages follow the outside-install confirmation flow.

The summary contains credential reference names only, never secret values. Its SHA-256 covers the canonical JSON shown by the dashboard. Confirmation is recorded in provenance and the application log; logs contain the summary hash and integration id, not artifact contents or secrets.

## CLI wrapper

The wrapper is a separate service and needs its own container hardening: run as non-root; read-only root filesystem; bounded writable tmpfs; no shell interpolation; fixed argv entries with whole-token placeholders; input/output extension, size, timeout, output, process, concurrency, CPU, memory, and PID limits; dropped Linux capabilities; no-new-privileges; internal-only network; and no published port. Keep remote URL fetching disabled, do not follow redirects, and pass files through managed uploads. The sample configuration and container files under `templates/cli-wrapper/` are the source of truth; review them before adapting the wrapper.

## Helper and review

Treat repository contents, API descriptions, event payloads, MCP tool descriptions, and AI-generated drafts as untrusted data. Probe only a harmless test operation. Inspect every requested capability and operation/tool approval, and use project-scoped connections. Put a human `gate:review` in a line wherever outputs need approval.
