"""Isolated subprocess driver for the demo's in-process HTTP end-to-end tests."""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from starlette.testclient import TestClient

from app.integrations.demo.bootstrap import (
    DEMO_FOREACH_LINE_ID, DEMO_LINE_ID, DEMO_MEDIA_LINE_ID, DEMO_MEDIA_PROJECT_ID, DEMO_WRITER_ID,
)
from app.main import app


EMAIL = "demo-admin@example.test"
PASSWORD = "test-only-placeholder-password"


class _MediaFetchHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        from app.services.media_links import MediaLinksService
        from app.services.platform_services import PlatformNotFoundError, PlatformStore

        parsed = urlsplit(self.path)
        token = parsed.path.rsplit("/", 1)[-1]
        tenant_id = parse_qs(parsed.query).get("tenant_id", [""])[0]
        try:
            link = MediaLinksService(PlatformStore(), tenant_id=tenant_id).lookup(
                token, expected_tenant_id=tenant_id,
            )
            body = Path(link["path"]).read_bytes()
        except (PlatformNotFoundError, OSError):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", str(link.get("content_type") or "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


@contextmanager
def _media_fetch_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MediaFetchHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _check(response, status: int, label: str):
    if response.status_code != status:
        raise AssertionError(f"{label}: expected HTTP {status}, got {response.status_code}: {response.text[:500]}")
    return response.json() if response.content else None


def _run_until(client: TestClient, token: str, run_id: str, predicate, label: str):
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + 20
    latest = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/production-lines/runs/{run_id}", headers=headers)
        _check(response, 200, "read run")
        latest = response.json()
        if predicate(latest):
            return latest
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {label}: {json.dumps(latest, default=str)[:1200]}")


def run_scenario(name: str) -> None:
    if name == "foreach":
        _run_foreach_scenario()
        return
    if name == "media":
        with _media_fetch_stub() as internal_media_url:
            os.environ["LYNEWRIGHT_INTERNAL_MEDIA_URL"] = internal_media_url
            _run_media_scenario()
        return
    with TestClient(app) as client:
        login = _check(client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}), 200, "login")
        token = login["token"]
        headers = {"Authorization": f"Bearer {token}"}

        if name == "disabled":
            integrations = _check(client.get("/api/integration-management", headers=headers), 200, "list integrations")["items"]
            lines = _check(client.get("/api/production-lines", headers=headers), 200, "list lines")["items"]
            assert not any("demo" in str(item.get("id", "")) for item in integrations), integrations
            assert lines == [], lines
            print("demo disabled: no demo integrations or line")
            return

        lines = _check(client.get("/api/production-lines", headers=headers), 200, "list lines")["items"]
        assert any(item["id"] == DEMO_LINE_ID for item in lines), lines

        if name == "rejected":
            settings_change = None
        else:
            settings_change = _check(
                client.put(
                    f"/api/integration-management/{DEMO_WRITER_ID}/settings",
                    headers=headers,
                    json={"values": {"tone": "friendly"}},
                ),
                200,
                "save demo writer settings",
            )
            assert settings_change["values"]["tone"] == "friendly", settings_change

        started = _check(
            client.post(
                f"/api/production-lines/{DEMO_LINE_ID}/runs",
                headers=headers,
                json={"vars": {"topic": "reliable local workflows"}},
            ),
            202,
            "start demo run",
        )
        run_id = started["id"]
        waiting = _run_until(
            client, token, run_id,
            lambda run: run["status"] in {"waiting", "failed", "rejected"},
            "review gate",
        )
        assert waiting["status"] == "waiting" and waiting["current_stage"] == "review", waiting
        review = next(item for item in waiting["stages"] if item["stage_id"] == "review")
        assert review["output"]["items"][0]["label"] == "Draft note", review
        writer_result = next(item for item in waiting["stages"] if item["stage_id"] == "write")["output"]

        if name == "rejected":
            value = {"approved": False, "items": [{"id": "draft", "decision": "refused", "reason": "Reject demo draft"}]}
            response = client.post(f"/api/production-lines/runs/{run_id}/continue", headers=headers, json={"value": value})
            rejected = _check(response, 202, "reject review")
            assert rejected["status"] == "rejected", rejected
            assert not any(item["stage_id"] == "format" for item in rejected["stages"]), rejected["stages"]
            print("rejected run: HTTP 202, status=rejected, formatter skipped")
            return

        assert writer_result["tone"] == "friendly", writer_result
        assert "friendly note" in writer_result["text"], writer_result
        approved_value = {"approved": True, "items": [{"id": "draft", "decision": "approved", "reason": ""}]}
        _check(
            client.post(f"/api/production-lines/runs/{run_id}/continue", headers=headers, json={"value": approved_value}),
            202,
            "approve review",
        )
        completed = _run_until(
            client, token, run_id,
            lambda run: run["status"] in {"done", "failed", "rejected"},
            "formatted completion",
        )
        assert completed["status"] == "done", completed
        formatted = next(item for item in completed["stages"] if item["stage_id"] == "format")["output"]
        assert formatted["status"] == "done", formatted
        assert "# Reliable Local Workflows" in formatted["formatted"], formatted
        poll_phases = [
            event.get("detail", {}).get("phase")
            for event in completed["timeline"]
            if event.get("type") == "stage.queued" and event.get("stage_id") == "format"
        ]
        assert "poll" in poll_phases and "poll-1" in poll_phases, poll_phases
        log_list = _check(
            client.get("/api/logs", headers=headers, params={"category": "line", "limit": 100}),
            200,
            "read notification log",
        )
        assert any(item["details"].get("run_id") == run_id for item in log_list["items"]), log_list
        print("approved run: HTTP 202, status=done, formatter polled pending then done, application log recorded")


def _run_foreach_scenario() -> None:
    with TestClient(app) as client:
        login = _check(client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}), 200, "login")
        token = login["token"]
        headers = {"Authorization": f"Bearer {token}"}
        lines = _check(client.get("/api/production-lines", headers=headers), 200, "list lines")["items"]
        assert any(item["id"] == DEMO_FOREACH_LINE_ID for item in lines), lines
        started = _check(client.post(
            f"/api/production-lines/{DEMO_FOREACH_LINE_ID}/runs", headers=headers, json={"vars": {}},
        ), 202, "start for_each demo")
        run_id = started["id"]
        waiting = _run_until(
            client, token, run_id,
            lambda run: run["status"] in {"waiting", "failed", "done", "done_with_errors"}, "per-item review",
        )
        assert waiting["status"] == "waiting" and waiting["current_stage"] == "review", waiting
        item_list = _check(
            client.get(f"/api/production-lines/runs/{run_id}/stages/each/items", headers=headers),
            200, "read per-item results",
        )
        assert (item_list["total"], item_list["succeeded"], item_list["failed"], item_list["skipped"]) == (3, 2, 1, 0), item_list
        assert item_list["items"][1]["error"], item_list["items"]
        review = next(stage for stage in waiting["stages"] if stage["stage_id"] == "review")["output"]["items"]
        assert len(review) == 2, review
        decisions = [{"id": item["id"], "decision": "approved", "reason": ""} for item in review]
        _check(client.post(
            f"/api/production-lines/runs/{run_id}/continue", headers=headers,
            json={"value": {"approved": True, "items": decisions}},
        ), 202, "approve item review")
        completed = _run_until(
            client, token, run_id,
            lambda run: run["status"] in {"done", "failed", "done_with_errors"}, "notify stage",
        )
        assert completed["status"] == "done_with_errors", completed
        assert any(stage["stage_id"] == "notify" and stage["status"] == "done" for stage in completed["stages"]), completed
        _check(
            client.post(f"/api/production-lines/runs/{run_id}/stages/each/items/retry", headers=headers),
            202, "retry failed item after notify",
        )
        repaired = _run_until(
            client, token, run_id,
            lambda run: run["status"] in {"done", "failed", "done_with_errors"}, "failed item retry",
        )
        assert repaired["status"] == "done", repaired
        repaired_items = _check(
            client.get(f"/api/production-lines/runs/{run_id}/stages/each/items", headers=headers),
            200, "read retried item results",
        )
        assert (repaired_items["total"], repaired_items["succeeded"], repaired_items["failed"]) == (3, 3, 0), repaired_items
        assert any(stage["stage_id"] == "notify" and stage["status"] == "done" for stage in repaired["stages"]), repaired
        print("for_each demo: partial item failure; review approved two succeeded items; notify completed with done_with_errors; retry repaired the failed item")


def _run_media_scenario() -> None:
    import sqlite3

    from app.services.platform_services import PlatformStore

    with TestClient(app) as client:
        login = _check(client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD}), 200, "login")
        token = login["token"]
        headers = {"Authorization": f"Bearer {token}"}
        lines = _check(client.get("/api/production-lines", headers=headers), 200, "list lines")["items"]
        assert any(item["id"] == DEMO_MEDIA_LINE_ID for item in lines), lines
        started = _check(client.post(
            f"/api/production-lines/{DEMO_MEDIA_LINE_ID}/runs",
            headers=headers, json={"project_id": DEMO_MEDIA_PROJECT_ID, "vars": {}},
        ), 202, "start media demo run")
        run_id = started["id"]
        waiting = _run_until(
            client, token, run_id,
            lambda run: run["status"] in {"waiting", "failed", "rejected"}, "media review gate",
        )
        assert waiting["status"] == "waiting" and waiting["current_stage"] == "review", waiting
        review = next(stage for stage in waiting["stages"] if stage["stage_id"] == "review")
        item = review["output"]["items"][0]
        preview = item["media_previews"][0]
        assert preview["kind"] == "preview" and preview["content_type"] == "image/png", preview
        preview_parts = urlsplit(preview["url"])
        preview_response = client.get(preview_parts.path + "?" + preview_parts.query)
        assert preview_response.status_code == 200 and preview_response.content.startswith(b"\x89PNG"), (
            preview_response.status_code, preview_response.headers,
        )
        _check(client.post(
            f"/api/production-lines/runs/{run_id}/continue", headers=headers,
            json={"value": {"approved": True}},
        ), 202, "approve media review")
        completed = _run_until(
            client, token, run_id,
            lambda run: run["status"] in {"done", "failed", "rejected"}, "media demo completion",
        )
        assert completed["status"] == "done", completed
        image_result = next(stage for stage in completed["stages"] if stage["stage_id"] == "imager")["output"]
        link_result = next(stage for stage in completed["stages"] if stage["stage_id"] == "inspect_link")["output"]
        upload_result = next(stage for stage in completed["stages"] if stage["stage_id"] == "inspect_upload")["output"]
        expected = {"bytes": image_result["bytes"], "sha256": image_result["sha256"], "content_type": "image/png"}
        assert link_result == upload_result == expected, (link_result, upload_result, expected)
        with PlatformStore().connection() as db:
            rows = db.execute(
                "SELECT revoked_at FROM platform_media_links WHERE tenant_id=? AND run_id=?",
                ("ten_default", run_id),
            ).fetchall()
        assert rows and all(row["revoked_at"] is not None for row in rows), rows
        revoked = client.get(preview_parts.path + "?" + preview_parts.query)
        assert revoked.status_code == 404, revoked.status_code
        media_root = Path(os.environ["LYNEWRIGHT_MANAGED_MEDIA_DIR"])
        assert not list(media_root.rglob("*.part")), list(media_root.rglob("*.part"))
        print("media demo: review PNG preview HTTP 200; link and upload bytes/hash match; run-end link revocation HTTP 404; no .part files")


if __name__ == "__main__":
    run_scenario(sys.argv[1])
