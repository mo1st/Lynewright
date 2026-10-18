# OpenAPI starter

This example has a synchronous health check and an asynchronous job endpoint.

1. Install Python 3.12+, then `python -m pip install -r requirements-test.txt`.
2. From the Lynewright checkout, install `-e backend` and `-e testkit` into this environment.
3. Run `python -m pytest` from this directory.
4. Replace the example routes, schemas, and operation ids with your service's OpenAPI contract.

The test starts the local FastAPI service and runs both operations through a
production line using `lynewright-testkit`. The first job poll is pending; the
next is done.
