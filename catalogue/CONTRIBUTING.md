# Contributing to the integration catalogue

## Development setup and tests

Use Python 3.12+. Install `requirements-test.txt`, then run `python tools/verify.py` and `python -m pytest -q` from the repository root. Keep artifacts byte-exact; after any signed input changes, rebuild with a serial bump and obtain a new signature from the offline release signer. Never place private signing material in this repository.

## Contributions and sensitive data

We use the Developer Certificate of Origin (DCO), not a Contributor Licence Agreement. Sign each commit with `git commit -s`; the `Signed-off-by` line must match the commit author. Example only: `Signed-off-by: PUBLIC_NAME <PUBLIC_EMAIL>`.

Never commit secrets, private hostnames, personal data, local identity lists, private keys, or temporary credentials. Use `lynewright` until the public organisation is selected.

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
