"""Coalesced, non-authoritative health persistence for runtime workers."""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.services.job_queue import DatabaseRuntimeJobQueue


@dataclass(frozen=True)
class WorkerHealthSnapshot:
    status: str = "online"
    current_job_id: int | None = None
    consecutive_errors: int = 0
    backoff_seconds: float = 0.0


class WorkerHealthPublisher:
    """Persists worker health on transitions and bounded heartbeat intervals."""

    def __init__(
        self,
        queue: DatabaseRuntimeJobQueue,
        *,
        worker_id: str,
        instance_group: str,
        heartbeat_seconds: int,
        parent_generation_id: str = "",
        child_generation_id: str = "",
    ) -> None:
        self.queue = queue
        self.worker_id = worker_id
        self.instance_group = instance_group
        self.heartbeat_seconds = max(1.0, float(heartbeat_seconds))
        self.parent_generation_id = parent_generation_id
        self.child_generation_id = child_generation_id
        self.last_persisted: WorkerHealthSnapshot | None = None
        self.last_heartbeat_monotonic = 0.0
        self.next_retry_monotonic = 0.0
        self.retry_seconds = 1.0

    def _persist(self, snapshot: WorkerHealthSnapshot) -> bool:
        try:
            self.queue.mark_worker_healthy(
                worker_id=self.worker_id,
                current_job_id=snapshot.current_job_id,
                instance_group=self.instance_group,
                parent_generation_id=self.parent_generation_id,
                child_generation_id=self.child_generation_id,
            )
        except Exception:
            # Worker health is metadata. Its failure cannot delay claiming,
            # alter a lease, or change an observation result.
            self.next_retry_monotonic = time.monotonic() + self.retry_seconds
            self.retry_seconds = min(30.0, self.retry_seconds * 2)
            return False
        self.last_persisted = snapshot
        self.last_heartbeat_monotonic = time.monotonic()
        self.next_retry_monotonic = 0.0
        self.retry_seconds = 1.0
        return True

    def publish_transition(self, *, current_job_id: int | None = None, force: bool = False) -> bool:
        snapshot = WorkerHealthSnapshot(current_job_id=current_job_id)
        if not force and snapshot == self.last_persisted:
            return False
        return self._persist(snapshot)

    def heartbeat_if_due(self) -> bool:
        now = time.monotonic()
        if now < self.next_retry_monotonic:
            return False
        if self.last_persisted is None or now - self.last_heartbeat_monotonic >= self.heartbeat_seconds:
            return self._persist(self.last_persisted or WorkerHealthSnapshot())
        return False

    def set_current_job(self, job_id: int) -> None:
        self._persist(WorkerHealthSnapshot(current_job_id=job_id))

    def clear_current_job(self) -> None:
        self._persist(WorkerHealthSnapshot())
