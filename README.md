# Lynewright

Install open-source tools, then chain them into durable production lines with human approval in Telegram or the dashboard.

> **Early release.** Lynewright is under active development; interfaces, installation policy, and deployment guidance may change.

## Five-minute quickstart

1. Copy `.env.example` to `.env`; choose `LYNEWRIGHT_BOOTSTRAP_EMAIL` and a strong `LYNEWRIGHT_BOOTSTRAP_PASSWORD`.
2. From this directory, run `docker compose up --build` and wait for the dashboard at port 8080 to load.
3. Open [http://localhost:8080](http://localhost:8080) and sign in with those bootstrap values.
4. Open **Lines**, choose **Demo: write → review → format → notify**, and start a run with a topic.
5. Open **Reviews**, approve the draft, then return to the run to see the completed Markdown and its application-log notification.

Compose enables the demo integrations with `LYNEWRIGHT_DEMO=1`. Set it to `0` to start without the demo line. The job worker runs in the API process, so the local stack has an API and dashboard service. The administrator is created once when both bootstrap values are configured. Keep the real `.env` local; the tracked example contains placeholders only.

After first sign-in, manage accounts in [Settings → Users](docs/users-and-sign-in.md) and update your own email or password from **Account** in the account menu.

## Demo

<!-- Demo capture placeholder: follow tools/release/capture_demo.md and save the approved capture as docs/assets/demo-line.gif. -->

## What Lynewright is for

Lynewright focuses on installing open-source integrations and chaining their operations into durable production lines, with review gates, signed-catalogue installs, and isolated runners for native packages. It is not a general-purpose workflow canvas, a broad internal-developer platform, an AI application builder, or a desktop package launcher. For those different goals, compare [n8n](https://n8n.io/), [Activepieces](https://www.activepieces.com/solutions/open-source-developers), [Windmill](https://www.windmill.dev/docs/intro), [Dify](https://www.dify.ai/dify-cloud), and [Pinokio](https://desktop.pinokio.co/docs/).

## Integrations

Start with the [integration how-to](docs/integrations-howto.md). The [catalogue guide](docs/integrations/catalogue.md) covers signed catalogue installs and provenance; the [runner guide](docs/integrations/runners.md) covers native package isolation; [security](docs/integrations/security.md) explains trust boundaries. The starter projects are available in [`templates/starters/`](templates/starters/README.md). Catalogue tooling is in [`catalogue/`](catalogue/README.md).

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
