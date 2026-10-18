# Native integration runners

A runner is a per-integration container for native Python code. Lynewright verifies the wheel, builds an image from it, then sends scoped invocation requests to that runner. Package modules do not run in the API process unless an operator explicitly sets `LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES=1`; that bypasses runner isolation and is marked with a critical red **in-process (trusted)** badge in Integrations.

## Operator walkthrough

Use these steps for an installed native package. Outside packages under the `confirm` policy must first pass the install confirmation gate. Catalogue packages are stored in `LYNEWRIGHT_PACKAGES_DIR`; the API image does not need that wheel installed.

1. Install the package through the Catalogue tab. The install route is `POST /api/catalogue/entries/{integration_id}/{version}/install`.
2. Authorize its required capabilities in Integrations (`POST /api/integration-management/{integration_id}/authorization`). Configure and enable a project connection too if the package calls a connected service.
3. Enable the integration (`POST /api/integration-management/{integration_id}/enable`).
4. Create a runner token (`POST /api/integration-management/{integration_id}/runner-token`). The token is returned once with a `LYNEWRIGHT_RUNNER_TOKEN=...` line; save it in the referenced runner env file with owner-only permissions. It is never put in generated runtime config.
5. Fetch runtime config (`GET /api/integration-management/{integration_id}/runtime-config?format=compose` or `?format=quadlet`). Save the generated Compose override or Quadlet files beside the Lynewright deployment configuration.
6. Build the runner base from the Lynewright repository root with `docker build -f runner/Containerfile -t lynewright-runner:latest .`.
7. Apply the generated configuration. For Compose, validate and start it together with the Lynewright Compose file; the generated build uses the stored wheel. For Quadlet, install the generated `.container` and `.network` units and start the runner with systemd. Attach the Lynewright API container to the generated runner network so the runner can call the API; do not use host networking.
8. Use the Integrations operation picker, save a line, and start its run.

The HTTP route sequence is checked against the router modules mounted by `app.main.CORE_ROUTERS`:

| Method | Mounted route | Purpose |
| --- | --- | --- |
| POST | `/api/catalogue/entries/{integration_id}/{version}/install` | Install a signed package |
| POST | `/api/integration-management/{integration_id}/authorization` | Grant capabilities |
| POST | `/api/integration-management/{integration_id}/enable` | Enable the integration |
| POST | `/api/integration-management/{integration_id}/runner-token` | Create or rotate the one-time token |
| GET | `/api/integration-management/{integration_id}/runtime-config` | Generate Compose or Quadlet config |
| GET | `/api/production-lines/operations` | List operations for the line builder |
| POST | `/api/production-lines` | Save a production line |
| POST | `/api/production-lines/{line_id}/runs` | Start a line run |

## Storage and isolation

`LYNEWRIGHT_PACKAGES_DIR` is the API-side directory for verified wheels; its default is `/data/packages`. `LYNEWRIGHT_PACKAGES_HOST_DIR` is the host-side path mounted there by Compose; its default is `./data/packages`. The runner build uses the host-side directory, so keep it available to the machine that builds and applies the generated service. Outside packages use the runner wheel path and SHA-256 described by their generated runtime config.

The base runner uses uid 10001, a read-only root filesystem, bounded `/tmp` and `/work` tmpfs, no mounted Lynewright data volume, no SQLite access, dropped capabilities, and no published port. The initial container environment contains only runner configuration and the one-time token. For each invocation, the API returns only credentials that are both declared by the package and bound to its project connection. Runner egress is `none` by default; a signed entry can allow `internet`.

The runner base installs Lynewright's full backend requirements. Integration wheels install with `--no-deps`; include additional dependencies in the package or provide them in a reviewed runner base image. Do not assume pip will resolve package dependencies during the image build.
