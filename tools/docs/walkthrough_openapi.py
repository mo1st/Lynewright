"""Run the documented OpenAPI import, approval, line, and review flow locally."""
from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
TESTKIT = ROOT / "testkit"
DEFAULT_TEMPLATE = ROOT / "templates/starters"


def run(template_root: Path) -> None:
    openapi_root = template_root / "openapi"
    declaration = openapi_root / "lynewright-integration.yaml"
    if not declaration.is_file():
        raise FileNotFoundError(f"OpenAPI template declaration not found: {declaration}")
    sys.path[:0] = [str(BACKEND), str(TESTKIT), str(openapi_root)]

    from _pytest.monkeypatch import MonkeyPatch
    import uvicorn
    from lynewright_testkit.harness import LynewrightHarness

    from example_openapi.app import app as example_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    endpoint = f"http://{host}:{port}"
    server = uvicorn.Server(uvicorn.Config(example_app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    monkeypatch = MonkeyPatch()
    harness = None
    try:
        thread.start()
        deadline = time.monotonic() + 8
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not server.started:
            raise RuntimeError("The template OpenAPI example service did not start.")

        monkeypatch.setenv("LYNEWRIGHT_ENV", "development")
        monkeypatch.setenv("LYNEWRIGHT_ADMIN_TOKEN", "")
        with tempfile.TemporaryDirectory(prefix="lynewright-k47-walkthrough-") as temporary:
            data_dir = Path(temporary) / "fresh-data"
            harness = LynewrightHarness(data_dir, monkeypatch=monkeypatch)
            integration_id = harness.import_file(declaration, endpoint_url=endpoint)
            connection_id = harness.connect(
                integration_id,
                connection_id="openapi-walkthrough",
                internal_endpoint="openapi_example",
                endpoint_url=endpoint,
            )
            harness.authorize(integration_id, ["network:use"])
            harness.openapi_approve(integration_id, connection_id, ["health"])
            harness.enable(integration_id, activate_native=False)

            health = next(
                item for item in harness.operations(integration_id=integration_id)
                if item["operation"] == "health"
            )
            line_id = "docs-openapi-review-walkthrough"
            stages = [
                {
                    "id": "health",
                    "kind": "operation",
                    "operation_id": health["operation_id"],
                    "contract": health["contract"],
                    "operation": health["operation"],
                    "args_template": {},
                },
                {
                    "id": "review",
                    "kind": "gate:review",
                    "items_template": [{
                        "id": "service-health",
                        "label": "OpenAPI service health",
                        "caption": "{{stages.health.output.status}}",
                    }],
                },
            ]
            harness.engine.db.put_line(
                line_id,
                name="OpenAPI walkthrough with review",
                project_id=harness.project_id,
                definition={"stages": stages},
                enabled=True,
                actor_id="docs-walkthrough",
                operation_directory=harness.catalog,
                operation_actor=harness.actor,
                create_only=True,
            )
            run_record = harness.engine.start(
                line_id,
                variables={},
                project_id=harness.project_id,
                actor={"user_id": "docs-walkthrough"},
            )
            deadline = time.monotonic() + 12
            while run_record["status"] not in {"waiting", "done", "failed", "cancelled", "rejected"} and time.monotonic() < deadline:
                time.sleep(0.025)
                run_record = harness.engine.get_run(run_record["id"])
            if run_record["status"] != "waiting" or run_record.get("current_stage") != "review":
                raise AssertionError(f"Expected line to pause at review; got {run_record['status']}: {run_record}")

            print(f"Example service: started at {endpoint}")
            print(f"Imported integration: {integration_id}")
            print("Approved OpenAPI operation: health")
            print("Production line: saved with health operation and gate:review")
            print("Run status before approval: waiting at review gate")
            run_record = harness.approve_review(run_record)
            if run_record["status"] != "done":
                raise AssertionError(f"Expected approved run to finish; got {run_record['status']}: {run_record}")
            health_stage = next(item for item in run_record["stages"] if item["stage_id"] == "health")
            print("Review: approved")
            print(f"Run status: {run_record['status']}")
            print(f"Final operation output: {json.dumps(health_stage['output'], sort_keys=True)}")
            print("Fresh Lynewright data dir: created for this run and removed afterward")
    finally:
        if harness is not None:
            harness.close()
        monkeypatch.undo()
        server.should_exit = True
        if thread.is_alive():
            thread.join(timeout=3)
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template-root", type=Path, default=DEFAULT_TEMPLATE,
        help="path to the included templates/starters directory",
    )
    args = parser.parse_args()
    try:
        run(args.template_root.resolve())
    except Exception as exc:
        print(f"OpenAPI walkthrough failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
