# Lynewright testkit

`lynewright-testkit` is a pytest plugin for integration authors. It builds a
temporary SQLite-backed runtime and exercises the production operation
catalogue and line runner without importing `app.main`, starting a dashboard,
or starting background workers.

Install the Lynewright backend first, then the plugin:

```powershell
py -m pip install -e ..\backend
py -m pip install -e .
```

The plugin registers `lynewright` and `lynewright_app` automatically. The first is a
direct-service harness with a private SQLite schema; the second starts the API,
bootstraps an admin, and returns an authenticated test client. Helper fixtures
include `install_integration_file(path)`, `install_native_package(dist_name)`,
`run_line(stages, vars)`, `approve_review(run)`, and
`poll_until(run, statuses)`. The direct harness also exposes `connect`,
`authorize`, `openapi_approve`, `discover_mcp`, `mcp_approve_and_map`,
`update_settings`, and `operations`. Bind a local stub through a named internal
endpoint with `lynewright.connect(..., internal_endpoint="stub", endpoint_url=url)`.
Line helpers wait for terminal states and raise `TimeoutError` on expiry.
Package installation remains operator-authorized: pass only the test
capabilities the manifest requests. Native package discovery reads the wheel's
`<package>/lynewright-manifest.json` data file and does not import the package's
entry-point module; the runner imports trusted implementation code only when
the operation executes. Provider output media should use
`sdk.media.upload_artifact(...)`, which works both in-process and through the
runner's artifact-upload API; providers must not write to Lynewright's managed
media directory.

Run the plugin checks from the Lynewright backend directory with
`py -m pytest ..\testkit\tests`.
