# Native Python starter

Use this only for Python code you trust. The sample declares a setting, an
asynchronous operation, and a managed image output.

1. Install Python 3.12+ and the local Lynewright backend plus `lynewright-testkit`.
2. From this directory, install the example with `python -m pip install -e .`.
3. Run `python -m pytest` from this directory.
4. Replace the manifest, provider, schemas, and operation tests with your integration.

The declaration lives in `example_native/lynewright-manifest.json` and is included
as package data in the wheel. Lynewright reads it without importing the package in
the API. `manifest.py` is the runner entry point; provider code is loaded in
the runner, unless an operator explicitly opts into trusted in-process
execution with `LYNEWRIGHT_ALLOW_INPROCESS_PACKAGES=1`.

The tests first prove the package is invisible while unlisted, then allowlist
it and run its async operation through a production line. The provider uses
`sdk.media.upload_artifact(...)` for output bytes in both execution modes;
providers must not write to Lynewright's managed-media directory. Changing the
declared setting changes the output.
