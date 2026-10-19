# Security policy for the integration catalogue

This repository publishes catalogue metadata, source snapshots, and artifacts. Reports are handled on a best-effort basis; the catalogue is an early-release service.

## Private reporting

Report suspected security vulnerabilities through [GitHub's private vulnerability reporting form](https://github.com/mo1st/Lynewright/security/advisories/new). Do not include exploitable details, credentials, or personal data in public issues. If the private reporting form is unavailable, open a [GitHub issue](https://github.com/mo1st/Lynewright/issues/new) requesting that private reporting be enabled, without describing the vulnerability.

Use [GitHub Issues](https://github.com/mo1st/Lynewright/issues) for ordinary bugs, feature requests, and general questions. No personal email contact is required.

## Scope

Reports are in scope for the signing and index-build pipeline, schema validation, artifact byte integrity, serial and yanking behavior, or catalogue records that misstate an artifact's source, permissions, or licence. Report Lynewright API vulnerabilities in the Lynewright repository.


## Bundled development catalogue

The bundled catalogue is a development example, signed with a development key and containing placeholder artifact URLs under `catalogue.example.invalid`. It is not a hosted production catalogue. The `backend/app/resources/catalogue-release-key.pub` file is intentionally empty; this release provides no built-in production signing key.

For production, host a catalogue with real artifact URLs, sign its index with your own private key, and configure `LYNEWRIGHT_CATALOGUE_URL` and `LYNEWRIGHT_CATALOGUE_KEYS` with its URL and trusted public key. Keep the private key offline and outside the repository and server. Never enable `LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY` in production. See the [catalogue guide](../docs/integrations/catalogue.md).
