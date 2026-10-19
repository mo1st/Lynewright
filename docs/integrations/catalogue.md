# Signed integration catalogue

A Lynewright catalogue is a JSON index and minisign signature that names reviewed integration versions and the exact artifact bytes or image digest. Lynewright verifies the signature with configured trusted keys before it shows entries or treats an install as catalogue-backed. An id by itself never establishes trust.

## Bundled development example

The bundled catalogue is a development example. Its index is signed with a development key and references placeholder artifact URLs under `catalogue.example.invalid`. These URLs do not provide downloadable integrations, and this release includes no built-in production catalogue signing key.

For production, host your own catalogue and artifacts at real URLs, sign the index with your own private key, and configure the catalogue URL and trusted public key as described below. Keep development-key trust disabled in production. Lynewright still starts without a trusted catalogue; the default confirmation policy supports integrations imported outside a catalogue.

## Provenance and drift

Integrations show one provenance badge:

- **Catalogue ✓** — the installed artifact exactly matches a trusted signed entry.
- **Catalogue (dev key)** — it matches an index signed with the built-in development key. This is a warning badge, not production trust.
- **Outside ⚠** — it was not an exact match for a trusted catalogue entry.
- **Bundled** — it ships with Lynewright.

The details include the artifact hash and, for catalogue installs, the entry version, review, catalogue serial, and signing key id. If the entry is yanked, missing, changed, or signed by a key that is no longer trusted, Lynewright marks the existing install **Catalogue drift**. Drift is a warning: Lynewright does not auto-update, remove, or disable the installation.

## Configure a catalogue

Set `LYNEWRIGHT_CATALOGUE_URL` to the index URL and `LYNEWRIGHT_CATALOGUE_KEYS` to one or more comma-separated minisign public keys. Keep the matching private signing key outside the repository and server. A private catalogue can use a deployment-owned internal endpoint: set `LYNEWRIGHT_CATALOGUE_INTERNAL_ENDPOINT_REF` to a reference such as `CATALOGUE` and configure `LYNEWRIGHT_INTERNAL_ENDPOINT__CATALOGUE` to the service origin. The catalogue and artifact requests continue through the SSRF guard; never disable or broaden it to reach a private host.

The built-in development public key is never trusted by default, even if operator keys are configured. `LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY=1` opts into that key for local testing only. Startup logs a warning, the dashboard keeps a red development-key banner visible, and provenance shows **Catalogue (dev key)** for installs made from its signatures. Never set this flag in production.

With no trusted keys, catalogue status is **untrusted** and refresh refuses with `no catalogue keys configured`; Lynewright still starts. Under the default `LYNEWRIGHT_INSTALL_POLICY=confirm`, an imported artifact is treated as outside and goes through the install confirmation dialog. Under `catalogue-only`, Lynewright warns at startup and no artifact can be installed until a trusted key is configured.

## Install an entry

The Catalogue tab shows any `requires_operator_setup` notes before installation. Follow those notes first; they explain required local services or endpoint references and do not relax the SSRF guard. Once an artifact is installed, authorize only the capabilities it needs, configure its connection if required, and approve its operations in Integrations.

If a catalogue package is marked **Installation incomplete**, use **Repair** to fetch the signed wheel and runner build file again. Lynewright checks the wheel hash before restoring either file. A normal Install on an already installed entry returns a conflict instead of downloading or overwriting it.

- **File** — a declarative OpenAPI or MCP integration file. Lynewright checks the SHA-256 of the exact downloaded bytes before import. The default and maximum file size is 1 MiB (`LYNEWRIGHT_CATALOGUE_FILE_MAX_BYTES`).
- **Package** — a native Python wheel. Lynewright checks the exact wheel SHA-256, stores it in `LYNEWRIGHT_PACKAGES_DIR`, and builds its isolated runner image from that stored artifact. The default and maximum wheel size is 100 MiB (`LYNEWRIGHT_CATALOGUE_WHEEL_MAX_BYTES`).
- **Container** — an immutable image reference with a published `sha256` digest. The thumbnail container is still a draft; GHCR publishing is decided at K6, so current signed releases publish files and packages only.

The index is capped at 5 MiB and its signature at 64 KiB. File and wheel caps are clamped to their stated maxima. For catalogue build, signing, exact-byte handling, and serial bumps, see `catalogue/README.md` in this repository.
