from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading
import time

import app.database as database
from app.services.integration_registry import IntegrationRegistry
from app.services.integration_runner import IntegrationRunnerService
from app.services.platform_services import PlatformStore


def test_runner_claim_and_schema_are_thread_safe_and_cached(
    tmp_path: Path, monkeypatch,
) -> None:
    database_path = tmp_path / "runner-claims.sqlite3"
    monkeypatch.setattr(database, "get_db_path", lambda: database_path)

    integration_id = "lynewright.integration.thread-stress"
    token = "crn_thread_stress_token"
    runner_id = "rtn_thread_stress"
    with sqlite3.connect(database_path) as db:
        IntegrationRunnerService.ensure_schema(db)
        db.execute(
            "INSERT INTO integration_runner_tokens (id,token_hash,tenant_id,integration_id,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                runner_id,
                IntegrationRunnerService.token_digest(token),
                "ten_default",
                integration_id,
                "2026-10-18T00:00:00+00:00",
            ),
        )

    store = PlatformStore()
    service = IntegrationRunnerService(store, tenant_id="ten_default")
    identity = {
        "runner_id": runner_id,
        "tenant_id": "ten_default",
        "integration_id": integration_id,
    }
    monkeypatch.setattr(
        IntegrationRegistry,
        "get",
        lambda _self, _integration_id: {"id": integration_id, "enabled": True, "manifest": {}},
    )

    schema_calls = 0
    schema_count_lock = threading.Lock()
    ensure_schema_unlocked = PlatformStore._ensure_schema_unlocked

    def count_schema_calls(connection: sqlite3.Connection) -> None:
        nonlocal schema_calls
        with schema_count_lock:
            schema_calls += 1
        ensure_schema_unlocked(connection)

    monkeypatch.setattr(PlatformStore, "_ensure_schema_unlocked", staticmethod(count_schema_calls))

    worker_count = 6
    claims_per_worker = 200
    start = threading.Barrier(worker_count)

    def claim_many() -> int:
        start.wait(timeout=10)
        for _ in range(claims_per_worker):
            assert service.claim(identity, wait_seconds=0) is None
        return claims_per_worker

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(lambda _index: claim_many(), range(worker_count)))

    assert sum(results) == worker_count * claims_per_worker
    assert schema_calls == 1


def test_platform_store_serializes_reused_factory_connection() -> None:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = PlatformStore(lambda: db)
    worker_count = 6
    iterations = 30
    start = threading.Barrier(worker_count)
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def exercise_shared_handle() -> int:
        nonlocal active, maximum_active
        start.wait(timeout=10)
        for _ in range(iterations):
            with store.connection() as connection:
                with state_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.001)
                    assert connection.execute("SELECT 1").fetchone()[0] == 1
                finally:
                    with state_lock:
                        active -= 1
        return iterations

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(lambda _index: exercise_shared_handle(), range(worker_count)))

    assert sum(results) == worker_count * iterations
    assert maximum_active == 1
    db.close()
