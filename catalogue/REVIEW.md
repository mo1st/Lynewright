# Catalogue review

## Reviewer checklist

For every published version, record evidence for each item before publishing. The thumbnail container remains an unpublished draft until its image is built and reviewed.

- [x] Read the exact pinned source and inspect each published file or wheel artifact.
- [x] Justify every requested capability and data contribution.
- [x] Confirm no secrets or secret-like literals are included.
- [x] Set resource limits for the published executable wheel; the draft container has limits declared.
- [x] Justify egress; use none unless the integration requires a named external service.
- [x] Run the published starter tests for their relevant integration kinds.
- [x] Record published artifact hashes and rebuild the files and wheel from pinned source.
- [x] Confirm deployment-specific endpoint refs and credential references are documented without values.
- [ ] Build and test the thumbnail image, record its real digest, and verify whether a clean rebuild reproduces it.

A release reviewer must replace any unchecked box with dated evidence. Files run through the Lynewright declarative importer; their operation calls still use configured connections and approvals. Package and container code runs in the per-integration runner, not in the API process.

## Per-entry record

For each entry/version, record: reviewer, review date, catalogue commit, pinned source commit/path, permission rationale, secret scan result, limits, egress rationale, tests, artifact hash/digest, and rebuild result.

## Seed review records

### OpenAPI example 1.0.0

- Source: openapi/lynewright-integration.yaml from template commit d6e17578deee53c6c885e2ab3d8840985999f021. Read in full.
- Permissions: network:use is needed to call the operator-configured internal example service. No integration egress, media, or credentials are requested.
- Secrets: no literal credentials or secret values; credentials list is empty.
- Limits/egress: Lynewright controls API operation timeouts; no runner egress is requested.
- Environment: operator must start the template OpenAPI service and configure its endpoint/ref.
- Test: the pinned starter suite passed (2 tests); the catalogue OpenAPI file was installed from the signed index and completed a line run against its local example service.
- Artifact SHA-256: `83981a6031b9b7001c934b3c7cc929307bbb6432c21a808665c65f6360135c02` (2,597 bytes; exact source file bytes).
- Build/rebuild: deterministic copy from the pinned source snapshot; the signed index hash matched two consecutive builds.

### MCP example 1.0.0

- Source: mcp/lynewright-integration.yaml from template commit d6e17578deee53c6c885e2ab3d8840985999f021. Read in full.
- Permissions: network:use is needed for the operator-configured MCP endpoint. No media or credentials are requested.
- Secrets: none.
- Limits/egress: Lynewright controls tool timeouts; no runner egress is requested.
- Environment: operator must start the template MCP server and approve the tool in Integrations.
- Test: the pinned starter suite passed (2 tests); the local signed index includes this exact source file.
- Artifact SHA-256: `a6f16b8c76ac0decab3e8c284d2b9dc0d841a47c83722db2c98f43957ecdc0e5` (300 bytes; exact source file bytes).
- Build/rebuild: deterministic copy from the pinned source snapshot; the signed index hash matched two consecutive builds.

### Native example 1.0.0

- Source: native/ from template commit d6e17578deee53c6c885e2ab3d8840985999f021. Read lynewright-manifest.json, provider, settings, and build metadata.
- Permissions: media:create and media write are required to produce the starter PNG artifact. No credentials or network egress are requested.
- Secrets: none.
- Limits: 1 CPU, 256 MiB memory, 64 PIDs, 60 seconds.
- Tests: the pinned native starter suite passed (2 tests), including its async render/status line. Lynewright's runner-subprocess proof uses the pinned starter provider code in a sentinel wheel, installs through the stored-wheel catalogue path with the API package allowlist empty, and completes `start → done` with a PNG. A separate signed-index test installs this exact wheel and confirms the API does not import it.
- Wheel SHA-256: `d005f2f22ef850aab2c264ae991cdc9800adbfaa862ca252b454d93c3c6a0e06` (4,485 bytes); rebuilt identically twice with `SOURCE_DATE_EPOCH=1792108800`.
- Storage: Lynewright stores the verified wheel under `LYNEWRIGHT_PACKAGES_DIR`; its runtime config references this wheel and hash without requiring it in the API image.

### CLI wrapper thumbnail 1.0.0

- Source: cli-wrapper/examples/thumbnail/Containerfile, command spec, and wrapper app from template commit d6e17578deee53c6c885e2ab3d8840985999f021. Read in full.
- Permissions: media read/write is needed to fetch the declared input and return the generated thumbnail; no external egress or credentials are requested.
- Limits: 1 CPU, 256 MiB memory, 64 PIDs, 30 seconds; output capped by the wrapper command.
- Tests: the template CLI starter suite passed (22 passed, 3 skipped). Lynewright's unit test verifies the container runtime config pins an image digest, but no real thumbnail image was built or installed.
- Image digest/reproducibility: pending. Docker, Podman, Buildah, and WSL container builders were unavailable in the authoring environment. The index builder refuses a fabricated digest; the starter base tag, apt packages, and pip requirements are mutable, so reproducibility is not claimed.

## Signed development index

- Serial: 3. Three entries are published (OpenAPI file, MCP file, native wheel); the thumbnail container remains a draft until a real OCI digest is available.
- Index SHA-256: `1e6817b20002baa74278915ea56e87359d65971c1cc7eceecb9d76feacf7f48f`.
- Minisign: signed with the real minisign 0.12 binary using a throwaway private key kept outside both repositories. `minisign -V` reported `Signature and comment signature verified`; the Lynewright verifier accepted key `dev:2b5dea8a528fefd1` (development key).
- Source/review commit: `165743fb484402df58b40c8bd8acff904a9c71f2` is the catalogue source baseline for this serial-3 identity release. Published starter source is pinned to template commit `d6e17578deee53c6c885e2ab3d8840985999f021`.

## K6.1b — Lynewright public identity

- Applied the operator-approved Lynewright name, identifiers, environment prefix, file formats, package names, entry-point group, and runner image name across the catalogue and its pinned template sources.
- Rebuilt the OpenAPI and MCP declarations and native wheel from the renamed template files. OpenAPI SHA-256 is `83981a6031b9b7001c934b3c7cc929307bbb6432c21a808665c65f6360135c02`, MCP SHA-256 is `a6f16b8c76ac0decab3e8c284d2b9dc0d841a47c83722db2c98f43957ecdc0e5`, and the native wheel SHA-256 is `d005f2f22ef850aab2c264ae991cdc9800adbfaa862ca252b454d93c3c6a0e06`.
- The serial-3 index uses the development signing key; it is not a production release signature. The public release-key slot remains empty until K6.3.
- The thumbnail image namespace is now ghcr.io/lynewright/template-thumbnail; the entry remains a draft until an immutable image digest is published.

## U1b.0 — Catalogue entry text

- Serial 4 adds optional `name`, `summary`, and `description` text to each signed entry. The values are factual summaries of the pinned OpenAPI, MCP, and native starter READMEs; the thumbnail entry remains a draft and is not in the signed index.
- Schema source: `backend/app/schemas/catalogue.schema.json` at source commit `c127bbb05088a2d7eb81b1bfa2ad11d5319e0bb7`. All three published artifact bytes and SHA-256 values are unchanged.
- Index SHA-256: `926e02c8d05b6fb30e1ed806e5e1f535bc23701eb147eabb55e225d0bd50c0fd`.
- Signature: development key `2b5dea8a528fefd1`; the seed stayed in the operator-chosen location outside both repositories. The real minisign verifier reported `Signature and comment signature verified`.
