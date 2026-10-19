<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/wordmark-dark.svg">
    <img src="docs/assets/wordmark-light.svg" alt="Lynewright" width="360">
  </picture>
</p>

# Lynewright

**Self-hosted, open-source automation for production lines: chain open-source tools, AI models and APIs into durable pipelines with human review gates, and let your AI assistant run them safely.**

[![Licence: AGPL v3+](https://img.shields.io/badge/licence-AGPL--3.0--or--later-blue)](LICENSE)
![Self-hosted](https://img.shields.io/badge/self--hosted-yes-success)
![MCP](https://img.shields.io/badge/MCP-server-purple)
![Status](https://img.shields.io/badge/status-early%20release-orange)

Lynewright turns repeatable work into **production lines**: ordered stages that call integrations, pass data from one tool to the next, wait for a person where it matters, and survive restarts. You install the tools you trust (OpenAPI services, MCP servers, command-line tools, Python packages) from a signed catalogue or your own files. Lynewright runs them in isolation and keeps a full audit trail of every run.

It is **free and open source** (AGPL-3.0-or-later), with no paid tier and no cloud lock-in. It runs on your own laptop or server with one command.

### Install in one line

```sh
curl -fsSL https://raw.githubusercontent.com/mo1st/Lynewright/main/install.sh | sh
```

Works on Linux, macOS and Windows (WSL) with Docker or Podman. It creates a private admin password, starts everything and prints the address. Details and the manual install are in [Quick start](#quick-start).

> **Early release.** Lynewright is under active development; interfaces and deployment guidance may change.

## Why Lynewright

- **Human-in-the-loop by design.** Review gates pause a run until someone approves or rejects, with images, video, PDFs and text previewed in place. Telegram tells you the moment a review is waiting.
- **Safe hands for AI assistants.** A built-in **MCP server** lets Gemini, Claude, ChatGPT or any MCP client list lines, start runs, follow progress and read reviews. Each line decides whether an assistant may approve, and every action is recorded with who did it and for whom.
- **Install integrations safely.** Signed catalogue (ed25519 / minisign), provenance on every install, an install policy (`catalogue-only`, `confirm`, `open`), and native packages running in **isolated runner containers** with scoped tokens, never inside the API.
- **Pipelines that scale out.** `for_each` stages run an operation for every item in a list, with concurrency limits, per-item results and **retry of only the failed items**. `map` stages filter, rename and sort data without code. `condition` and `choice` stages branch.
- **Durable and observable.** Runs survive restarts, every stage keeps its output and timeline, and notifications tell you when a review is waiting, a run fails or a run finishes.
- **Secure defaults.** Per-install encryption key for stored credentials, SSRF protection with pinned outbound connections, strict Content-Security-Policy, rate-limited sign-in, loopback-only by default.
- **Easy to extend.** Add a tool from an OpenAPI spec, an MCP server, a small declarative YAML file, a CLI wrapper or a Python package, with starter templates and a testkit.

## What you can build

A production line is any repeatable process with clear steps. Some examples:

| Use case | Example line |
|---|---|
| **Website analysis and SEO audits** | crawl pages → `for_each` page: performance and accessibility checks → summarise findings → review → publish the report |
| **Monitoring and alerting** | check endpoints or feeds → compare with the last run → notify on Telegram when something changes (start runs from your assistant, the API or any scheduler) |
| **Data processing and ETL** | fetch from an API or file → `map` to clean and reshape → validate → load into your database → report the counts |
| **Video and media production** | script → voice-over → render with open-source tools → human review of the cut → thumbnails → publish |
| **Document processing** | collect invoices or contracts → extract the fields (OCR or AI) → validate totals and dates → a person reviews the exceptions → export to your accounting system |
| **Retail and product catalogues** | research trending products → `for_each` product: fetch details and images → review the drafts → add the approved ones to your store |
| **Inspection, analysis and reporting** | collect documents, images or logs → run analysis tools → flag issues for a person → generate the final report |
| **Managing open-source tools** | bring many open-source projects behind one dashboard → install, update and remove them safely → combine them into lines |
| **AI-assistant operations** | ask your assistant to start a line → it follows the run → Telegram tells you a review is waiting → you approve it in the dashboard |

Each line is built from the integrations you install; Lynewright supplies the engine, the review gates, the safety model and the dashboard.

## Quick start

Read the [installer](install.sh), then run:

```sh
curl -fsSL https://raw.githubusercontent.com/mo1st/Lynewright/main/install.sh | sh
```

The installer checks for Git and Docker or Podman, downloads Lynewright, creates a private `.env` with a random admin password and encryption key, starts the stack, and prints the address and password once. It binds to this machine only by default; to expose it, set `LYNEWRIGHT_BIND=0.0.0.0` in `.env` and put a TLS proxy in front. Run it again to upgrade, and `sh uninstall.sh` to remove everything.

### Manual install

1. Copy `.env.example` to `.env` and set `LYNEWRIGHT_BOOTSTRAP_EMAIL`, a `LYNEWRIGHT_BOOTSTRAP_PASSWORD` of at least 12 characters, and a random `LYNEWRIGHT_SECRET_KEY` of at least 32 characters (for example `openssl rand -hex 32`).
2. Run `docker compose up --build` from this directory. Compose refuses to start without the password and key.
3. Open [http://localhost:8080](http://localhost:8080) and sign in.

Manual Compose binds to `127.0.0.1` by default. Keep the real `.env` private; the tracked example contains placeholders only.

## Try the demo

With `LYNEWRIGHT_DEMO=1` (the default), open **Lines**, choose **Demo: write → review → format → notify**, and start a run with a topic. Open **Reviews** to approve the draft, then return to the run to see the result. Other demo lines show media review and `for_each` with per-item retry.

<!-- Demo capture placeholder: follow tools/release/capture_demo.md and save the approved capture as docs/assets/demo-line.gif. -->

## Guides

- [Production lines](docs/lines.md): stages, passing data between integrations, `for_each`, `map` and reviews.
- [AI assistants (MCP)](docs/assistants.md): connect Gemini, Claude, ChatGPT or any MCP client.
- [Telegram](docs/telegram.md): get reviews and alerts on your phone in about a minute.
- [Users and sign-in](docs/users-and-sign-in.md).

## Background

Lynewright began as the core engine of a larger private system, where its production lines, review gates and integrations ran real workloads for months. It has now been extracted into a standalone, general-purpose project and released as free, open source software, so that others can use it, share their experience, and help it grow.

The standalone version is covered by automated tests, a one-line install check on every change, and real installs on our own servers, but it is new as an independent product. Expect rough edges: please [open an issue](https://github.com/mo1st/Lynewright/issues) when something breaks, and share the lines and integrations you build. Contributions are very welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## How it compares

[n8n](https://n8n.io/), [Activepieces](https://www.activepieces.com/), [Windmill](https://www.windmill.dev/) and [Dify](https://dify.ai/) are excellent general workflow and AI-app builders with large connector libraries. Lynewright is narrower on purpose: **durable production lines, first-class human review, signed and isolated integration installs, and assistant control through MCP**, all self-hosted and free.

## Integrations

Start with the [integration how-to](docs/integrations-howto.md). The [catalogue guide](docs/integrations/catalogue.md) covers signed catalogue installs and provenance; the [runner guide](docs/integrations/runners.md) covers native package isolation; [security](docs/integrations/security.md) explains trust boundaries. The starter projects are available in [`templates/starters/`](templates/starters/README.md). Catalogue tooling is in [`catalogue/`](catalogue/README.md).

The bundled catalogue is a **development example**, signed with a development key and using placeholder artifact URLs. Production use requires a separately hosted catalogue with real artifact URLs and trusted signing keys; see the [catalogue guide](docs/integrations/catalogue.md). Lynewright can also use integrations imported through the default confirmation flow.

### Native Python integrations

Native packages ship `lynewright-manifest.json` as data. Lynewright reads that manifest without importing package code in the API; implementation code runs in a per-integration runner by default. Catalogue wheels are stored under `LYNEWRIGHT_PACKAGES_DIR`. Outside packages used for discovery must be installed in the API environment and listed in `LYNEWRIGHT_INTEGRATION_PACKAGES`; keep entry-point modules unimported unless in-process execution is explicitly enabled. See the [native guide](docs/integrations/native.md) and [runner guide](docs/integrations/runners.md).

## Known limits

See [Known limits](docs/integrations/known-limits.md) for current declarative-file, catalogue, runner, and helper constraints.

## Development and tests

Backend, from `backend/`:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest app/tests
```

Frontend, from `frontend/`:

```powershell
npm ci
npm run build
npm test
```

The backend suite includes the production-line and review flows plus closure, word, schema, provenance, install-policy, and documentation-snippet guards. See [CONTRIBUTING.md](CONTRIBUTING.md) for all test commands.

## Licence

Lynewright is licensed under [GNU AGPL v3 or later](LICENSE). Contributions are accepted under the DCO; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Licences by folder

The core application at the repository root is licensed under AGPL-3.0-or-later. The integration starters in `templates/starters/` and catalogue tooling in `catalogue/` are each licensed under Apache-2.0; every catalogue artifact keeps the licence stated by its own entry.
