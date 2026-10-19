# Lynewright integration catalogue

> **Development example.** The bundled index uses a development signing key and placeholder artifact URLs under `catalogue.example.invalid`. It is not a hosted production catalogue. Production operators must host their own catalogue and artifacts, sign the index, and configure trusted public keys. See the [catalogue guide](../docs/integrations/catalogue.md).

Starter integration sources are pinned to template commit `d6e17578deee53c6c885e2ab3d8840985999f021`. This repository contains reviewed entry declarations, pinned source snapshots, deterministic artifacts, the signed index, and per-entry review records. The signing key is supplied by the publisher and must remain outside this repository.

## Build, verify, and sign a release

Use Python 3.12 or newer and set `SOURCE_DATE_EPOCH` when rebuilding wheels. Preserve `sources/` and `dist/` byte-for-byte: do not re-encode, normalize text, or change line endings. `.gitattributes` marks `/dist/**` and `/sources/**` as `-text`, preventing Git line-ending conversion; copy artifacts as bytes and verify their indexed SHA-256 before signing.

From the repository root, build with `python tools/build_index.py --refresh-entry-hashes --allow-missing-container`. The container draft is omitted unless its OCI image has a real immutable digest. If any entry, source, or artifact input changes after a previous release, include `--bump-serial`; the builder refuses changed inputs without it and increments the serial in `state.json`. The first release initializes serial 1.

Run `python tools/verify.py` before signing. It checks the schema, pinned source commit, exact artifact bytes and hashes, deterministic rebuild outputs, and business-word scan. It does not verify the minisign signature. Sign with `python tools/sign_index.py --minisign <verified-minisign-binary> --key <external-private-key> --public-key <trusted-public-key>`; providing `--public-key` also runs the real `minisign -V` verification. Keep the private key outside the repo and never commit it. Verify the index and `.minisig` together before publishing.

Serial bumps apply to the signed index, not to a changed artifact name alone. Rebuild and verify the bytes first, bump the serial when the signed inputs change, sign the resulting index, then publish the index, signature, and referenced artifacts as one release. Never edit an artifact after its hash is signed.

## Container entry

The thumbnail image is still a draft because it has no published image digest. GHCR publication is decided at K6. A release that includes it must use the real `@sha256:` digest recorded by the image builder and verified from the registry; do not substitute a tag or invent a digest. The current development index omits this draft.

## Private catalogue walkthrough

Keep the signing private key outside both repositories and configure the public key in Lynewright's `LYNEWRIGHT_CATALOGUE_KEYS`. Serve the `dist/` directory with `python -m http.server 8080 --directory dist` for a local test. Point `LYNEWRIGHT_CATALOGUE_URL` at the index over HTTPS, or configure `LYNEWRIGHT_CATALOGUE_INTERNAL_ENDPOINT_REF` with the matching `LYNEWRIGHT_INTERNAL_ENDPOINT__<REF>` for a private service. Keep the SSRF guard enabled; signed artifact URLs must retain the configured catalogue origin.

An administrator refreshes `POST /api/catalogue/refresh`, installs a published entry through `POST /api/catalogue/entries/{id}/{version}/install`, authorizes and enables it in Integrations, then exercises its operation in a production line. The current signed index publishes OpenAPI and MCP files plus the native wheel. The container thumbnail remains unpublished pending its digest.

## Review state

See `REVIEW.md` for the required security checklist and per-entry decisions. Before publishing, update the reviewer, date, source commit, permission rationale, tests, artifact hash or digest, and rebuild evidence.

The current development index is serial 4. It adds optional catalogue display names, summaries, and plain-text descriptions; the three published artifact byte sequences and their hashes are unchanged. Serial 4 is signed with the development key and is for local testing, not production use.

## Licence

Catalogue tooling in this repository is licensed under [Apache-2.0](LICENSE). Every catalogue entry has a required SPDX `licence` field describing the referenced artifact; that field does not change the artifact's own licence.
