# Security policy for the integration starters

This repository contains starter code and examples, not the Lynewright API. Reports are handled on a best-effort basis; the starters are provided as early-release examples.

## Private reporting

Report suspected security vulnerabilities through [GitHub's private vulnerability reporting form](https://github.com/mo1st/Lynewright/security/advisories/new). Do not include exploitable details, credentials, or personal data in public issues. If the private reporting form is unavailable, open a [GitHub issue](https://github.com/mo1st/Lynewright/issues/new) requesting that private reporting be enabled, without describing the vulnerability.

Use [GitHub Issues](https://github.com/mo1st/Lynewright/issues) for ordinary bugs, feature requests, and general questions. No personal email contact is required.

## Scope

Reports are in scope for defects in starter code that could cause unsafe command execution, path escape, unintended credential exposure, insecure example defaults, or incorrect package manifests and media handling. The Lynewright API's SSRF guard, runner service, catalogue trust, installation policy, authentication, and media-link implementation are maintained in the Lynewright repository; report those there.

The security of user-written integrations built from these starters is out of scope. Native integration code is trusted code and should only be installed after review. Lynewright's runner limits exposure but does not make arbitrary code safe.

## Bundled development catalogue

The bundled catalogue is a development example, signed with a development key and containing placeholder artifact URLs under `catalogue.example.invalid`. It is not a hosted production catalogue. The `backend/app/resources/catalogue-release-key.pub` file is intentionally empty; this release provides no built-in production signing key.

For production, host a catalogue with real artifact URLs, sign its index with your own private key, and configure `LYNEWRIGHT_CATALOGUE_URL` and `LYNEWRIGHT_CATALOGUE_KEYS` with its URL and trusted public key. Keep the private key offline and outside the repository and server. Never enable `LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY` in production. See the [catalogue guide](../../docs/integrations/catalogue.md).
