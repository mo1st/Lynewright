"""Durable ownership and lifecycle policy for integration-started jobs."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator, Literal

from app.services.platform_services import JobService, PlatformNotFoundError, PlatformOperationError, PlatformStore, PlatformValidationError
from app.services.tenant_schema import DEFAULT_TENANT_ID


ActiveJobPolicy = Literal["drain", "cancel", "detach"]
_ACTIVE_JOB_POLICIES = frozenset({"drain", "cancel", "detach"})


class IntegrationJobLifecycleService:
    """Records integration ownership without exposing job-control internals to adapters."""

    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id
        self.jobs = JobService(self.store, tenant_id=tenant_id)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_integration_jobs (
                tenant_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                connection_id TEXT NOT NULL DEFAULT '',
                contribution_id TEXT NOT NULL,
                active_job_policy TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL DEFAULT 'active',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, job_id)
            );
            CREATE INDEX IF NOT EXISTS idx_platform_integration_jobs_owner
                ON platform_integration_jobs(tenant_id, integration_id, lifecycle_state);
            """
        )

    def register(
        self,
        *,
        job_id: str,
        integration_id: str,
        connection_id: str | None,
        contribution_id: str,
        policy: str,
    ) -> None:
        if policy not in _ACTIVE_JOB_POLICIES:
            raise PlatformValidationError("Integration job policy must be drain, cancel, or detach.")
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO platform_integration_jobs
                (tenant_id,job_id,integration_id,connection_id,contribution_id,active_job_policy)
                VALUES (?,?,?,?,?,?)""",
                (self.tenant_id, job_id, integration_id, connection_id or "", contribution_id, policy),
            )

    def owner(self, job_id: str) -> dict[str, str]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM platform_integration_jobs WHERE tenant_id=? AND job_id=?",
                (self.tenant_id, job_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Integration job ownership was not found.")
        return {key: str(value) for key, value in dict(row).items()}

    def deactivate_integration(self, integration_id: str) -> dict[str, list[str]]:
        """Apply each active job's declared policy without touching other integrations."""
        result: dict[str, list[str]] = {"draining": [], "cancelled": [], "detached": []}
        with self._connection() as db:
            rows = db.execute(
                """SELECT job_id,active_job_policy FROM platform_integration_jobs
                WHERE tenant_id=? AND integration_id=? AND lifecycle_state='active'""",
                (self.tenant_id, integration_id),
            ).fetchall()
        for row in rows:
            job_id, policy = str(row["job_id"]), str(row["active_job_policy"])
            if policy == "cancel":
                try:
                    self.jobs.cancel(job_id)
                except PlatformOperationError:
                    # A remote completion that won the race is already terminal.
                    pass
                self._set_state(job_id, "cancelled")
                result["cancelled"].append(job_id)
            elif policy == "detach":
                self._set_state(job_id, "detached")
                result["detached"].append(job_id)
            else:
                self._set_state(job_id, "draining")
                result["draining"].append(job_id)
        return result

    def callback_allowed(self, *, job_id: str, integration_id: str, connection_id: str | None) -> bool:
        owner = self.owner(job_id)
        if owner["integration_id"] != integration_id or owner["connection_id"] != (connection_id or ""):
            raise PlatformOperationError("Integration callback does not own this job.")
        if owner["lifecycle_state"] == "cancelled":
            return False
        return self.jobs.get(job_id)["status"] in {"queued", "running"}

    def reopen(self, *, job_id: str, integration_id: str, connection_id: str | None) -> dict[str, str]:
        owner = self.owner(job_id)
        if owner["integration_id"] != integration_id or owner["connection_id"] != (connection_id or ""):
            raise PlatformOperationError("Integration retry does not own this job.")
        self._set_state(job_id, "active")
        return self.owner(job_id)

    def _set_state(self, job_id: str, state: str) -> None:
        with self._connection() as db, db:
            db.execute(
                "UPDATE platform_integration_jobs SET lifecycle_state=?,updated_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND job_id=?",
                (state, self.tenant_id, job_id),
            )
