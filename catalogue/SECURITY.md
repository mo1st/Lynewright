# Security policy for the integration catalogue

This repository publishes catalogue metadata, source snapshots, and artifacts. Reports are handled on a best-effort basis; the catalogue is an early-release service.

## Private reporting

After this repository is created under `lynewright` and GitHub private vulnerability reporting is enabled, use its **Security → Advisories → Report a vulnerability** form. Do not post exploitable details in a public issue. The organisation name is a placeholder until the operator creates it.

## Scope

Reports are in scope for the signing and index-build pipeline, schema validation, artifact byte integrity, serial and yanking behavior, or catalogue records that misstate an artifact's source, permissions, or licence. Report Lynewright API vulnerabilities in the Lynewright repository.

Private signing keys must stay offline and outside this repository. The public key and fingerprint used by Lynewright will be published at K6.3; the key slot is in the Lynewright repository at `backend/app/resources/catalogue-release-key.pub`. No release key is stored here.
