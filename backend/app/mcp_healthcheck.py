"""Container health probe for the authenticated MCP transport.

The streamable HTTP endpoint correctly rejects an unauthenticated probe.  A
401 therefore proves that the ASGI server and its authentication middleware are
running, without needing a production client token in a container command.
"""

from __future__ import annotations

import sys
from urllib.error import HTTPError
from urllib.request import urlopen


HEALTH_URL = "http://127.0.0.1:8001/mcp"
TIMEOUT_SECONDS = 3


def main() -> int:
    try:
        with urlopen(HEALTH_URL, timeout=TIMEOUT_SECONDS) as response:
            return 0 if response.status in {200, 401, 405} else 1
    except HTTPError as error:
        return 0 if error.code in {401, 405} else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
