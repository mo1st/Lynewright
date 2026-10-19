from __future__ import annotations

import sqlite3

import pytest

from app.services.platform_services import (
    CredentialService,
    EventService,
    HTTPService,
    JobService,
    MediaService,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
    ProjectService,
    SettingsService,
)
from app.services.platform_events import CoreEventBus


@pytest.fixture()
def store() -> PlatformStore:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE tenant_projects (
            project_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tenant_id, name)
        );
        INSERT INTO tenant_projects (project_id, tenant_id, name)
        VALUES ('demo-project', 'ten_default', 'Default Project');
        """
    )
    result = PlatformStore(lambda: connection)
    yield result
    connection.close()


def test_project_service_crud_and_explicit_delete(store: PlatformStore) -> None:
    service = ProjectService(store)

    created = service.create({"project_id": "project-one", "name": "Project One"})
    assert created["status"] == "active"
    assert service.get("project-one")["name"] == "Project One"

    updated = service.update("project-one", {"name": "Updated Project"})
    assert updated["name"] == "Updated Project"

    with pytest.raises(PlatformValidationError):
        service.delete("project-one", confirm_project_id="different")
    assert service.delete("project-one", confirm_project_id="project-one") == {
        "project_id": "project-one", "status": "archived"
    }
    assert [item["project_id"] for item in service.list()] == ["demo-project"]


def test_project_service_rejects_invalid_identifiers(store: PlatformStore) -> None:
    service = ProjectService(store)
    with pytest.raises(PlatformValidationError):
        service.get("../../database")
    with pytest.raises(PlatformValidationError):
        service.create({"project_id": "contains spaces", "name": "Invalid"})


def test_job_service_redacts_payload_and_enforces_transitions(store: PlatformStore) -> None:
    delivered: list[dict[str, object]] = []
    bus = CoreEventBus(EventService(store))
    bus.subscribe("job.*", lambda event: delivered.append(event))
    jobs = JobService(store, event_bus=bus)
    job = jobs.create({"type": "platform.test", "payload": {"api_key": "do-not-return", "safe": True}})
    assert job["payload"] == {"api_key": "[REDACTED]", "safe": True}
    cancelled = jobs.cancel(job["id"])
    assert cancelled["status"] == "cancelled"
    retried = jobs.retry(job["id"])
    assert retried["status"] == "queued"
    assert retried["attempts"] == 1
    with pytest.raises(PlatformOperationError):
        jobs.retry(job["id"])
    assert [event["type"] for event in delivered] == ["job.created", "job.cancelled", "job.created"]


def test_job_progress_completion_and_failure_are_persisted(store: PlatformStore) -> None:
    jobs = JobService(store)
    completed = jobs.complete(
        jobs.start(jobs.create({"type": "platform.long_task"})["id"])["id"],
        result={"token": "redact-this", "summary": "done"},
    )
    assert completed["progress"] == 100
    assert completed["result"] == {"token": "[REDACTED]", "summary": "done"}

    failed_job = jobs.start(jobs.create({"type": "platform.failure"})["id"])
    failed = jobs.fail(failed_job["id"], code="provider.timeout", message="provider timed out")
    assert failed["status"] == "failed"
    assert failed["error_code"] == "provider.timeout"


def test_credential_service_never_returns_raw_secret(store: PlatformStore) -> None:
    credentials = CredentialService(store)
    status = credentials.set({"name": "provider.token", "kind": "token", "secret": "actual-secret"})
    assert status["configured"] is True
    assert "secret" not in status
    assert credentials.list_metadata()[0]["name"] == "provider.token"

    with store.connection() as db:
        encrypted = db.execute("SELECT encrypted_secret FROM platform_credentials WHERE name='provider.token'").fetchone()["encrypted_secret"]
    assert encrypted != "actual-secret"
    assert credentials.use("provider.token", lambda value: {"length": len(value)}) == {"length": 13}
    with pytest.raises(PlatformOperationError):
        credentials.use("provider.token", lambda value: value)
    with pytest.raises(PlatformValidationError):
        credentials.delete("provider.token", confirm_name="other")
    assert credentials.delete("provider.token", confirm_name="provider.token")["deleted"] is True


def test_media_settings_events_and_ssrf_boundary(store: PlatformStore) -> None:
    media = MediaService(store)
    with pytest.raises(PlatformValidationError):
        media.create({"name": "private", "url": "http://127.0.0.1/private"})

    settings = SettingsService(store)
    assert settings.set({"key": "platform.ui.density", "value": "compact"})["value"] == "compact"
    with pytest.raises(PlatformValidationError):
        settings.set({"key": "database.path", "value": "forbidden"})

    event = EventService(store).publish({"type": "platform.project.created", "payload": {"token": "hidden"}})
    assert event["payload"] == {"token": "[REDACTED]"}
    assert EventService(store).get(event["id"])["type"] == "platform.project.created"

    with pytest.raises(PlatformValidationError):
        HTTPService().validate_public_url("http://localhost:8000")
    with pytest.raises(PlatformNotFoundError):
        JobService(store).get("job_missing")


def test_event_subscriber_failure_is_isolated(store: PlatformStore) -> None:
    events = EventService(store)
    bus = CoreEventBus(events)
    received: list[str] = []

    def failing(_: dict[str, object]) -> None:
        raise RuntimeError("subscriber failure")

    bus.subscribe("project.*", failing)
    bus.subscribe("project.*", lambda event: received.append(str(event["type"])))
    event = bus.publish({"type": "project.created", "payload": {"secret": "never persisted"}})

    assert event["payload"] == {"secret": "[REDACTED]"}
    assert received == ["project.created"]
