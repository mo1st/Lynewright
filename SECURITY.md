# Security policy

Lynewright is an early release. Security reports are handled on a best-effort basis; there is no guaranteed response time.

## Private reporting

After this repository is created under `lynewright` and GitHub private vulnerability reporting is enabled, use its **Security → Advisories → Report a vulnerability** form. Do not post exploitable details in a public issue. The organisation name is a placeholder until the operator creates it.

## Scope

Reports are in scope when they affect the SSRF guard or internal endpoint references, runner isolation, catalogue signatures and the development-key gate, installation policy or the confirmation gate, authentication or tokens, or managed-media links and artifact transfers.

The security of user-written integration code itself is out of scope. Native packages are trusted code and must be reviewed before installation; the runner is a containment boundary, not a guarantee that arbitrary code is harmless.

## Catalogue release key

The release public key and fingerprint will be published here at K6.3 after the operator generates and verifies the offline signing key. The current `backend/app/resources/catalogue-release-key.pub` slot is empty; this tree contains no release key. The development key is not trusted by default and is not suitable for production.
