# Security policy for the integration starters

This repository contains starter code and examples, not the Lynewright API. Reports are handled on a best-effort basis; the starters are provided as early-release examples.

## Private reporting

After this repository is created under `lynewright` and GitHub private vulnerability reporting is enabled, use its **Security → Advisories → Report a vulnerability** form. Do not post exploitable details in a public issue. The organisation name is a placeholder until the operator creates it.

## Scope

Reports are in scope for defects in starter code that could cause unsafe command execution, path escape, unintended credential exposure, insecure example defaults, or incorrect package manifests and media handling. The Lynewright API's SSRF guard, runner service, catalogue trust, installation policy, authentication, and media-link implementation are maintained in the Lynewright repository; report those there.

The security of user-written integrations built from these starters is out of scope. Native integration code is trusted code and should only be installed after review. Lynewright's runner limits exposure but does not make arbitrary code safe.

## Catalogue release key

This repository does not contain a release key. Lynewright will publish the catalogue release public key and fingerprint at K6.3; its empty key slot is `backend/app/resources/catalogue-release-key.pub` in the Lynewright repository. The development key is not trusted by default and is not suitable for production.
