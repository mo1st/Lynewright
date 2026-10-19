# Contributing to Lynewright

## Development setup

Use Python 3.12+ and Node.js 22+. Clone the repository, then install backend test dependencies with `python -m pip install -r backend/requirements-dev.txt`. Install dashboard dependencies from `frontend/` with `npm ci`. Docker Compose provides the local API and dashboard; copy `.env.example` to `.env` and use local-only credentials.

## Test commands

Backend and guards, from `backend/`:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest app/tests
```

Dashboard, from `frontend/`:

```powershell
npm ci
npm run build
npm test
```

Testkit, from `testkit/` after installing it from the repository root with `python -m pip install -e ./testkit`:

```powershell
python -m pytest
```

Catalogue tooling (run these commands from `catalogue/`):

```powershell
Set-Location catalogue
python -m pip install -r requirements-test.txt
python tools/verify.py
python -m pytest -q --ignore=sources
Set-Location ..
```

Template starters (all four, including Lynewright-marked tests):

```powershell
python -m pip install ./backend -e ./testkit
Push-Location templates/starters/openapi
python -m pip install -r requirements-test.txt
python -m pytest tests
Pop-Location
Push-Location templates/starters/mcp
python -m pip install -r requirements-test.txt
python -m pytest tests
Pop-Location
Push-Location templates/starters/native
python -m pip install -r requirements-test.txt
python -m pip install -e .
python -m pytest tests
Pop-Location
Push-Location templates/starters/cli-wrapper
python -m pip install -r requirements-test.txt
python -m pytest tests
Pop-Location
```

The root CI runs every starter test, including the `lynewright`-marked tests, after installing the backend and testkit from this repository.

Keep these guards green: `test_core_closure.py`, `test_no_business_words.py`, `test_docs_snippets.py`, the provenance guard, and the install-policy tests. The full backend suite includes them. Regenerate notices with `python tools/release/notices.py` and verify with `python tools/release/notices.py --check` after dependency changes.
## Contributions and sensitive data

Use [GitHub Issues](https://github.com/mo1st/Lynewright/issues) for bugs, feature requests, and questions, and pull requests for proposed changes. Report suspected vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

We use the Developer Certificate of Origin (DCO), not a Contributor Licence Agreement. Sign each commit with `git commit -s`. The sign-off must match the commit author. You can use your GitHub-provided noreply email address to keep your personal email private.

Never commit secrets, private hostnames, personal data, local identity lists, private keys, or temporary credentials. The project repository is [mo1st/Lynewright](https://github.com/mo1st/Lynewright).

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

For new source files that need a copyright/license notice, use `SPDX-License-Identifier: AGPL-3.0-or-later`. Do not mass-edit existing or generated files just to add headers.
