"""Small local OpenAPI service used by the U3b dashboard screenshot run."""
from __future__ import annotations

from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "U3b local screenshot service", "version": "1.0.0"},
    "paths": {
        "/health": {
            "get": {
                "operationId": "health",
                "summary": "Read service health",
                "responses": {"200": {"description": "Healthy"}},
            },
        },
    },
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler interface
        if self.path == "/openapi.json":
            payload = SPEC
        elif self.path == "/health":
            payload = {"status": "ok"}
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    arguments = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", arguments.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
