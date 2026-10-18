# MCP starter

This example exposes one read-only `echo` tool over Streamable HTTP.

1. Install Python 3.12+, then `python -m pip install -r requirements-test.txt`.
2. From the Lynewright checkout, install `-e backend` and `-e testkit` into this environment.
3. Run `python -m pytest` from this directory.
4. Replace the tool and its integration declaration; keep approval explicit in Lynewright.

The test starts the local MCP server, imports the integration declaration,
discovers and approves the tool, maps it to the testkit contract, then runs it
through a production line.
