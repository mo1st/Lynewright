from __future__ import annotations

import sqlite3
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.media_links as media_router
from app.middleware.request_logging import _safe_log_path
from app.services.integration_media_transfer import IntegrationMediaTransferService
from app.services.integration_artifacts import extract_media_refs, materialize_artifact_bytes
from app.services.line_engine import validate_definition
from app.services.media_links import MAX_TTL_SECONDS, MediaLinksService
from app.services.platform_services import MediaService, PlatformStore, PlatformValidationError


class _Limiter:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[dict[str, object]] = []

    def consume(self, **kwargs: object) -> bool:
        self.calls.append(dict(kwargs))
        return self.allow

    def consume_many(self, **kwargs: object) -> bool:
        del kwargs
        return self.allow


@pytest.fixture()
def media_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database = tmp_path / "media-links.sqlite3"
    root = tmp_path / "managed-media"
    root.mkdir()
    monkeypatch.setenv("LYNEWRIGHT_MANAGED_MEDIA_DIR", str(root))
    monkeypatch.setenv("LYNEWRIGHT_PUBLIC_URL", "https://lynewright.test")
    monkeypatch.setenv("LYNEWRIGHT_INTERNAL_MEDIA_URL", "http://lynewright-api:8000")
    opened: list[sqlite3.Connection] = []

    def connect() -> sqlite3.Connection:
        db = sqlite3.connect(database, timeout=20, check_same_thread=False)
        db.row_factory = sqlite3.Row
        opened.append(db)
        return db

    store = PlatformStore(connect)
    with store.connection() as db:
        PlatformStore.ensure_schema(db)
        MediaLinksService.ensure_schema(db)
    body = b"0123456789abcdef"
    (root / "sample.stored").write_bytes(body)
    media = MediaService(store, tenant_id="tenant-a").create_managed(
        name="sample.png", content_type="image/png",
        metadata={"stored_as": "sample.stored", "storage_kind": "managed_file"},
        project_id="project-a",
    )
    yield store, root, media, body
    for db in opened:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _client(monkeypatch: pytest.MonkeyPatch, store: PlatformStore, limiter: _Limiter | None = None) -> TestClient:
    monkeypatch.setattr(media_router, "PlatformStore", lambda: store)
    if limiter is None:
        limiter = _Limiter()
    import app.services.rate_limiter as rate_limiter

    monkeypatch.setattr(rate_limiter, "get_rate_limiter", lambda: limiter)
    app = FastAPI()
    app.include_router(media_router.router)
    return TestClient(app)


def _path(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + ("?" + parts.query if parts.query else "")


def _link(store: PlatformStore, media: dict[str, object], *, now=None) -> tuple[MediaLinksService, str, str]:
    service = MediaLinksService(store, tenant_id="tenant-a", now=now) if now else MediaLinksService(store, tenant_id="tenant-a")
    token = service.issue(
        str(media["id"]), purpose="test", project_id="project-a", actor={"user_id": "test-user"},
    )
    return service, token, service.public_url(token, tenant_id="tenant-a")


def test_media_link_get_head_range_and_trace_redaction(
    media_store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _root, media, body = media_store
    events: list[dict[str, object]] = []
    import app.services.application_logging as application_logging

    monkeypatch.setattr(application_logging, "log_event", lambda **kwargs: events.append(kwargs))
    service, token, url = _link(store, media)
    assert len(token) >= 43
    with store.connection() as db:
        saved = db.execute("SELECT token_hash,access_count FROM platform_media_links").fetchone()
        assert saved["token_hash"] == service.token_hash(token)
        assert token not in saved["token_hash"]

    with _client(monkeypatch, store) as client:
        get = client.get(_path(url))
        assert get.status_code == 200
        assert get.content == body
        assert get.headers["content-type"] == "image/png"
        assert get.headers["content-length"] == str(len(body))
        assert get.headers["accept-ranges"] == "bytes"
        assert get.headers["content-disposition"].startswith("inline;")
        assert get.headers["cache-control"] == "private, no-store"
        assert get.headers["x-content-type-options"] == "nosniff"

        head = client.head(_path(url))
        assert head.status_code == 200 and head.content == b""
        assert head.headers["content-length"] == str(len(body))

        ranged = client.get(_path(url), headers={"Range": "bytes=3-7"})
        assert ranged.status_code == 206
        assert ranged.content == body[3:8]
        assert ranged.headers["content-range"] == f"bytes 3-7/{len(body)}"
    with store.connection() as db:
        row = db.execute("SELECT access_count FROM platform_media_links").fetchone()
        assert row["access_count"] == 3
        serialized_trace = " ".join(str(row[0]) for row in db.execute(
            "SELECT metadata_json FROM platform_operation_trace_events",
        ).fetchall())
        assert service.token_hash(token) in serialized_trace
        assert token not in serialized_trace
    assert events and all(token not in str(event) for event in events)
    assert all(event.get("details", {}).get("link_hash_sha256") == service.token_hash(token) for event in events)
    safe_path = _safe_log_path(f"/api/media/l/{token}")
    assert token not in safe_path and service.token_hash(token) in safe_path


def test_expired_revoked_wrong_tenant_and_deleted_links_share_404(
    media_store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root, media, _body = media_store
    service, token, url = _link(store, media)
    with store.connection() as db, db:
        db.execute("UPDATE platform_media_links SET expires_at=0 WHERE token_hash=?", (service.token_hash(token),))
    revoked = service.issue(str(media["id"]), purpose="test", project_id="project-a")
    service.revoke(token=revoked)
    (root / "other-tenant.stored").write_bytes(b"other tenant")
    with store.connection() as db, db:
        db.execute("CREATE TABLE tenants (id TEXT PRIMARY KEY, slug TEXT UNIQUE, name TEXT NOT NULL)")
        db.execute("CREATE TABLE tenant_projects (project_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL)")
        db.execute("INSERT INTO tenants (id,slug,name) VALUES ('tenant-b','tenant-b','Tenant B')")
        db.execute("INSERT INTO tenant_projects (project_id,tenant_id,name) VALUES ('project-b','tenant-b','Project B')")
    other_media = MediaService(store, tenant_id="tenant-b").create_managed(
        name="other-tenant.bin", content_type="application/octet-stream",
        metadata={"stored_as": "other-tenant.stored", "storage_kind": "managed_file"},
        project_id="project-b",
    )
    other_tenant_service = MediaLinksService(store, tenant_id="tenant-b")
    other_tenant_token = other_tenant_service.issue(
        str(other_media["id"]), purpose="other-tenant", project_id="project-b",
    )
    deleted = service.issue(str(media["id"]), purpose="test", project_id="project-a")
    MediaService(store, tenant_id="tenant-a").delete(str(media["id"]), confirm_media_id=str(media["id"]))

    with _client(monkeypatch, store) as client:
        wrong_tenant = url.replace("tenant_id=tenant-a", "tenant_id=tenant-b")
        another_tenants_token = other_tenant_service.public_url(other_tenant_token, tenant_id="tenant-a")
        responses = [
            client.get(_path(url)),
            client.get(_path(service.public_url(revoked, tenant_id="tenant-a"))),
            client.get(_path(wrong_tenant)),
            client.get(_path(another_tenants_token)),
            client.get(_path(service.public_url(deleted, tenant_id="tenant-a"))),
        ]
    assert [response.status_code for response in responses] == [404, 404, 404, 404, 404]
    assert len({response.content for response in responses}) == 1


def test_media_link_ttl_limit_expiry_sweep_and_rate_limit(
    media_store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _root, media, _body = media_store
    with pytest.raises(PlatformValidationError, match="24 hours"):
        MediaLinksService(store, tenant_id="tenant-a").issue(
            str(media["id"]), ttl_seconds=MAX_TTL_SECONDS + 1, purpose="too-long", project_id="project-a",
        )

    now = [10.0]
    service = MediaLinksService(store, tenant_id="tenant-a", now=lambda: now[0])
    token = service.issue(
        str(media["id"]), ttl_seconds=1, purpose="expires", project_id="project-a",
    )
    now[0] = 12.0
    assert service.sweep_expired() == 1
    with pytest.raises(Exception, match="Media link not found"):
        service.lookup(token)

    live = MediaLinksService(store, tenant_id="tenant-a").issue(
        str(media["id"]), purpose="rate", project_id="project-a",
    )
    limiter = _Limiter(allow=False)
    with _client(monkeypatch, store, limiter) as client:
        response = client.get(_path(MediaLinksService.public_url(live, tenant_id="tenant-a")))
    assert response.status_code == 429
    assert {str(call["namespace"]) for call in limiter.calls} == {"media_link_token", "media_link_ip"}


def test_media_link_normalizes_untrusted_content_type_before_headers(
    media_store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root, _media, _body = media_store
    (root / "untrusted.stored").write_bytes(b"safe bytes")
    media = MediaService(store, tenant_id="tenant-a").create_managed(
        name="download.bin", content_type="image/png\r\nX-Injected: yes",
        metadata={"stored_as": "untrusted.stored", "storage_kind": "managed_file"},
        project_id="project-a",
    )
    service = MediaLinksService(store, tenant_id="tenant-a")
    token = service.issue(str(media["id"]), purpose="header-test", project_id="project-a")
    with _client(monkeypatch, store) as client:
        response = client.get(_path(service.public_url(token, tenant_id="tenant-a")))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "x-injected" not in response.headers


def test_managed_path_traversal_escape_is_refused(media_store) -> None:
    _store, root, _media, _body = media_store
    transfer = IntegrationMediaTransferService(temp_dir=root)
    with pytest.raises(PlatformValidationError, match="confined"):
        transfer.resolve_managed_path("../outside.stored")


def test_managed_path_symlink_escape_is_refused(media_store) -> None:
    store, root, _media, _body = media_store
    outside = root.parent / "outside.stored"
    outside.write_bytes(b"outside")
    link = root / "escape.stored"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"This Windows host does not permit symlink creation: {exc}")
    escaped = MediaService(store, tenant_id="tenant-a").create_managed(
        name="escape.png", content_type="image/png",
        metadata={"stored_as": "escape.stored"}, project_id="project-a",
    )
    with pytest.raises(PlatformValidationError, match="confined"):
        MediaLinksService(store, tenant_id="tenant-a").issue(
            str(escaped["id"]), purpose="escape", project_id="project-a",
        )
    transfer = IntegrationMediaTransferService(temp_dir=root)
    with pytest.raises(PlatformValidationError, match="confined"):
        transfer.resolve_managed_path("escape.stored")


def test_artifact_bytes_materialize_once_as_managed_media(media_store, monkeypatch: pytest.MonkeyPatch) -> None:
    store, root, _media, _body = media_store
    import app.services.integration_artifacts as artifacts

    monkeypatch.setattr(artifacts, "PlatformStore", lambda: store)
    body = b"materialized artifact bytes"
    first = materialize_artifact_bytes(
        tenant_id="tenant-a", project_id="project-a", origin_url="https://artifact.example/file.bin",
        body=body, name="artifact.bin", content_type="application/octet-stream",
    )
    second = materialize_artifact_bytes(
        tenant_id="tenant-a", project_id="project-a", origin_url="https://artifact.example/file.bin",
        body=b"different repeated poll content", name="artifact.bin", content_type="application/octet-stream",
    )
    assert first["id"] == second["id"]
    assert first["metadata"]["sha256"] == hashlib.sha256(body).hexdigest()
    stored = IntegrationMediaTransferService(temp_dir=root).resolve_managed_path(first["metadata"]["stored_as"])
    assert stored.read_bytes() == body
    refs = extract_media_refs({
        "artifact_url": first["url"], "content_type": first["content_type"], "name": first["name"],
    })
    assert refs == [{"media_id": first["id"], "url": first["url"], "content_type": "application/octet-stream", "name": "artifact.bin"}]


def test_mcp_upload_format_is_rejected_when_saving_a_line() -> None:
    class _McpUploadDirectory:
        def resolve_operation(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"descriptor": {
                "operation_id": "lynewright.integrations.operation.fixture",
                "kind": "mcp", "input_schema": {
                    "type": "object", "properties": {
                        "file": {"type": "string", "format": "lynewright-upload"},
                    }, "required": ["file"],
                },
            }}

    with pytest.raises(PlatformValidationError, match="MCP tools do not accept uploads"):
        validate_definition({"stages": [{
            "id": "upload", "kind": "operation", "contract": "lynewright.fixture",
            "operation": "upload", "args_template": {},
        }]}, operation_directory=_McpUploadDirectory(), actor=object(), project_id="project-a")
