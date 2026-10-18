"""Durable, fenced SQLite queue for Lynewright runtime jobs."""
from __future__ import annotations
from contextlib import closing

import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.database import get_connection, init_db


def ensure_schema(db: sqlite3.Connection) -> None:
    """Create only the generic queue and worker-health tables."""
    db.executescript("""
    CREATE TABLE IF NOT EXISTS runtime_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_type TEXT NOT NULL,
        deduplication_key TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        priority INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 4,
        claim_version INTEGER NOT NULL DEFAULT 0,
        last_claimed_at TEXT,
        available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        locked_at TEXT,
        lease_expires_at TEXT,
        locked_by TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT,
        UNIQUE(job_type, deduplication_key)
    );
    CREATE INDEX IF NOT EXISTS idx_runtime_jobs_claim
        ON runtime_jobs(status,available_at,priority,created_at);
    CREATE INDEX IF NOT EXISTS idx_runtime_jobs_lease
        ON runtime_jobs(status,lease_expires_at);
    CREATE TABLE IF NOT EXISTS runtime_workers (
        worker_id TEXT PRIMARY KEY,
        worker_kind TEXT NOT NULL DEFAULT 'worker',
        instance_group TEXT NOT NULL DEFAULT '',
        application_version TEXT NOT NULL DEFAULT '',
        generation_id TEXT NOT NULL DEFAULT '',
        generation_started_at TEXT,
        terminal_state_acknowledged_at TEXT,
        supervisor_task_status TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        parent_generation_id TEXT NOT NULL DEFAULT '',
        child_generation_id TEXT NOT NULL DEFAULT '',
        replacement_blocked INTEGER NOT NULL DEFAULT 0,
        orphan_worker_possible INTEGER NOT NULL DEFAULT 0,
        failure_reason TEXT NOT NULL DEFAULT '',
        last_heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        current_job_id INTEGER,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        worker_status TEXT NOT NULL DEFAULT 'offline',
        last_error_at TEXT,
        last_error_operation TEXT NOT NULL DEFAULT '',
        last_error_message TEXT NOT NULL DEFAULT '',
        consecutive_errors INTEGER NOT NULL DEFAULT 0,
        current_backoff_seconds REAL NOT NULL DEFAULT 0,
        restart_count_last_hour INTEGER NOT NULL DEFAULT 0,
        restart_limit_exceeded INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_runtime_workers_heartbeat
        ON runtime_workers(last_heartbeat_at,worker_status);
    """)

def owns_claim(db: sqlite3.Connection, *, job_id: int, worker_id: str, claim_version: int) -> bool:
    return db.execute(
        """SELECT 1 FROM runtime_jobs WHERE id=? AND status='running' AND locked_by=?
           AND claim_version=? AND lease_expires_at>CURRENT_TIMESTAMP""",
        (job_id, worker_id, claim_version),
    ).fetchone() is not None


def complete_claimed_job_in_connection(db: sqlite3.Connection, *, job_id: int, worker_id: str, claim_version: int) -> bool:
    return db.execute(
        """UPDATE runtime_jobs SET status='completed',completed_at=CURRENT_TIMESTAMP,locked_at=NULL,
           lease_expires_at=NULL,locked_by='',updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND status='running' AND locked_by=? AND claim_version=?
           AND lease_expires_at IS NOT NULL AND lease_expires_at>CURRENT_TIMESTAMP""",
        (job_id, worker_id, claim_version),
    ).rowcount == 1


def cancel_claimed_job_in_connection(db: sqlite3.Connection, *, job_id: int, worker_id: str, claim_version: int, reason: str) -> bool:
    return db.execute(
        """UPDATE runtime_jobs SET status='cancelled',last_error=?,completed_at=CURRENT_TIMESTAMP,
           locked_at=NULL,lease_expires_at=NULL,locked_by='',updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND status='running' AND locked_by=? AND claim_version=?
           AND lease_expires_at IS NOT NULL AND lease_expires_at>CURRENT_TIMESTAMP""",
        (reason[:1000], job_id, worker_id, claim_version),
    ).rowcount == 1


def fail_claimed_job_in_connection(db: sqlite3.Connection, *, job_id: int, worker_id: str, claim_version: int, error: str, retry_at: str | None) -> bool:
    status = "pending" if retry_at else "failed"
    return db.execute(
        """UPDATE runtime_jobs SET status=?,
           available_at=COALESCE(?,available_at),last_error=?,locked_at=NULL,
           lease_expires_at=NULL,locked_by='',completed_at=CASE WHEN ?='failed' THEN CURRENT_TIMESTAMP ELSE NULL END,
           updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='running' AND locked_by=? AND claim_version=?
           AND lease_expires_at IS NOT NULL AND lease_expires_at>CURRENT_TIMESTAMP""",
        (status, retry_at, error[:1000], status, job_id, worker_id, claim_version),
    ).rowcount == 1


class DatabaseRuntimeJobQueue:
    def claim_next(
        self, *, worker_id: str, lease_seconds: int, job_types: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        if job_types is not None and not job_types:
            return None
        init_db()
        lease = max(1, int(lease_seconds))
        with closing(get_connection()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            where_types = ""
            params: list[Any] = []
            if job_types is not None:
                where_types = f" AND job_type IN ({','.join('?' for _ in job_types)})"
                params.extend(job_types)
            row = db.execute(
                f"""SELECT * FROM runtime_jobs WHERE attempts<max_attempts
                    AND ((status='pending' AND available_at<=CURRENT_TIMESTAMP) OR
                         (status='running' AND lease_expires_at<=CURRENT_TIMESTAMP))
                    {where_types}
                    ORDER BY priority DESC,created_at ASC LIMIT 1""",
                tuple(params),
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            job_id = int(row["id"])
            old_version = int(row["claim_version"] or 0)
            changed = db.execute(
                """UPDATE runtime_jobs SET status='running',locked_at=CURRENT_TIMESTAMP,
                   last_claimed_at=CURRENT_TIMESTAMP,lease_expires_at=datetime('now','+' || ? || ' seconds'),
                   locked_by=?,attempts=attempts+1,claim_version=claim_version+1,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND claim_version=? AND (
                     (status='pending' AND available_at<=CURRENT_TIMESTAMP) OR
                     (status='running' AND lease_expires_at<=CURRENT_TIMESTAMP))""",
                (lease, worker_id, job_id, old_version),
            ).rowcount
            if changed != 1:
                db.rollback()
                return None
            claimed = dict(db.execute("SELECT * FROM runtime_jobs WHERE id=?", (job_id,)).fetchone())
            db.commit()
            return claimed

    def renew_lease(self, *, job_id: int, worker_id: str, claim_version: int, lease_seconds: int) -> bool:
        init_db()
        lease = max(1, lease_seconds)
        with closing(get_connection()) as db, db:
            return db.execute(
                """UPDATE runtime_jobs SET lease_expires_at=datetime('now','+' || ? || ' seconds'),
                   updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='running' AND locked_by=? AND claim_version=?
                     AND lease_expires_at IS NOT NULL AND lease_expires_at>CURRENT_TIMESTAMP""",
                (lease, job_id, worker_id, claim_version),
            ).rowcount == 1

    def fail(self, *, job_id: int, worker_id: str, claim_version: int, error: str, retry_at: str | None = None) -> None:
        init_db()
        with closing(get_connection()) as db, db:
            fail_claimed_job_in_connection(db, job_id=job_id, worker_id=worker_id, claim_version=claim_version, error=error, retry_at=retry_at)

    def mark_worker_healthy(
        self,
        *,
        worker_id: str,
        current_job_id: int | None = None,
        clear_restart_limit: bool = True,
        instance_group: str = "",
        parent_generation_id: str = "",
        child_generation_id: str = "",
    ) -> None:
        """Record a real worker's successful recovery without erasing diagnostics."""
        init_db()
        with closing(get_connection()) as db, db:
            db.execute(
                """INSERT INTO runtime_workers(
                       worker_id,worker_kind,instance_group,application_version,parent_generation_id,child_generation_id,started_at,last_heartbeat_at,current_job_id,
                       worker_status,consecutive_errors,current_backoff_seconds,restart_limit_exceeded,updated_at
                   ) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?,'online',0,0,0,CURRENT_TIMESTAMP)
                   ON CONFLICT(worker_id) DO UPDATE SET
                       worker_kind='worker',
                       instance_group=CASE WHEN excluded.instance_group<>'' THEN excluded.instance_group ELSE runtime_workers.instance_group END,
                       application_version=excluded.application_version,
                       parent_generation_id=CASE WHEN excluded.parent_generation_id<>'' THEN excluded.parent_generation_id ELSE runtime_workers.parent_generation_id END,
                       child_generation_id=CASE WHEN excluded.child_generation_id<>'' THEN excluded.child_generation_id ELSE runtime_workers.child_generation_id END,
                       last_heartbeat_at=CURRENT_TIMESTAMP,
                       current_job_id=excluded.current_job_id,
                       worker_status='online',
                       consecutive_errors=0,
                       current_backoff_seconds=0,
                       restart_limit_exceeded=CASE WHEN ? THEN 0 ELSE runtime_workers.restart_limit_exceeded END,
                       updated_at=CURRENT_TIMESTAMP""",
                (worker_id, "worker", instance_group[:120], get_settings().application_version, parent_generation_id[:160], child_generation_id[:160], current_job_id, 1 if clear_restart_limit else 0),
            )

    def heartbeat(
        self,
        *,
        worker_id: str,
        current_job_id: int | None = None,
        instance_group: str = "",
        parent_generation_id: str = "",
        child_generation_id: str = "",
    ) -> None:
        """A successful heartbeat is an explicit healthy-state transition."""
        self.mark_worker_healthy(
            worker_id=worker_id,
            current_job_id=current_job_id,
            instance_group=instance_group,
            parent_generation_id=parent_generation_id,
            child_generation_id=child_generation_id,
        )

    def record_worker_error(
        self,
        *,
        worker_id: str,
        operation: str,
        message: str,
        backoff_seconds: float,
        status: str = "degraded",
        instance_group: str = "",
    ) -> None:
        init_db()
        with closing(get_connection()) as db, db:
            db.execute(
                """INSERT INTO runtime_workers(
                       worker_id,worker_kind,instance_group,application_version,started_at,last_heartbeat_at,worker_status,
                       last_error_at,last_error_operation,last_error_message,consecutive_errors,
                       current_backoff_seconds,updated_at
                   ) VALUES(?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?,CURRENT_TIMESTAMP,?,?,1,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(worker_id) DO UPDATE SET
                       worker_kind='worker',
                       instance_group=CASE WHEN excluded.instance_group<>'' THEN excluded.instance_group ELSE runtime_workers.instance_group END,
                       application_version=excluded.application_version,
                       last_heartbeat_at=CURRENT_TIMESTAMP,
                       worker_status=excluded.worker_status,
                       last_error_at=CURRENT_TIMESTAMP,
                       last_error_operation=excluded.last_error_operation,
                       last_error_message=excluded.last_error_message,
                       consecutive_errors=runtime_workers.consecutive_errors+1,
                       current_backoff_seconds=excluded.current_backoff_seconds,
                       updated_at=CURRENT_TIMESTAMP""",
                (worker_id, "worker", instance_group[:120], get_settings().application_version, status, operation[:80], message[:500], max(0.0, backoff_seconds)),
            )

    def set_worker_status(
        self,
        *,
        worker_id: str,
        status: str,
        worker_kind: str = "worker",
        instance_group: str = "",
        generation_id: str = "",
        generation_started_at: str = "",
        supervisor_task_status: str = "",
        parent_generation_id: str = "",
        child_generation_id: str = "",
        replacement_blocked: bool = False,
        orphan_worker_possible: bool = False,
        failure_reason: str = "",
        current_backoff_seconds: float = 0,
        restart_count: int = 0,
        restart_limit_exceeded: bool = False,
        error_operation: str = "",
        error_message: str = "",
    ) -> None:
        """Update a worker or supervisor state while retaining historical errors."""
        if worker_kind not in {"worker", "supervisor"}:
            raise ValueError("worker_kind must be 'worker' or 'supervisor'")
        init_db()
        with closing(get_connection()) as db, db:
            db.execute(
                """INSERT INTO runtime_workers(
                       worker_id,worker_kind,instance_group,application_version,generation_id,generation_started_at,supervisor_task_status,
                       parent_generation_id,child_generation_id,replacement_blocked,orphan_worker_possible,failure_reason,
                       started_at,last_heartbeat_at,worker_status,
                       last_error_at,last_error_operation,last_error_message,current_backoff_seconds,
                       restart_count_last_hour,restart_limit_exceeded,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?,
                       CASE WHEN ?<>'' THEN CURRENT_TIMESTAMP ELSE NULL END,?,?,?, ?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(worker_id) DO UPDATE SET
                       worker_kind=excluded.worker_kind,
                       instance_group=CASE WHEN excluded.instance_group<>'' THEN excluded.instance_group ELSE runtime_workers.instance_group END,
                       application_version=excluded.application_version,
                       generation_id=CASE WHEN excluded.worker_kind='supervisor' AND excluded.generation_id<>'' THEN excluded.generation_id ELSE runtime_workers.generation_id END,
                       generation_started_at=CASE WHEN excluded.worker_kind='supervisor' AND excluded.generation_started_at<>'' THEN excluded.generation_started_at ELSE runtime_workers.generation_started_at END,
                       supervisor_task_status=CASE WHEN excluded.worker_kind='supervisor' AND excluded.supervisor_task_status<>'' THEN excluded.supervisor_task_status ELSE runtime_workers.supervisor_task_status END,
                       parent_generation_id=CASE WHEN excluded.parent_generation_id<>'' THEN excluded.parent_generation_id ELSE runtime_workers.parent_generation_id END,
                       child_generation_id=CASE WHEN excluded.child_generation_id<>'' THEN excluded.child_generation_id ELSE runtime_workers.child_generation_id END,
                       replacement_blocked=CASE WHEN excluded.worker_kind='supervisor' THEN excluded.replacement_blocked ELSE runtime_workers.replacement_blocked END,
                       orphan_worker_possible=CASE WHEN excluded.worker_kind='supervisor' THEN excluded.orphan_worker_possible ELSE runtime_workers.orphan_worker_possible END,
                       failure_reason=CASE WHEN excluded.worker_kind='supervisor' AND excluded.failure_reason<>'' THEN excluded.failure_reason ELSE runtime_workers.failure_reason END,
                       last_heartbeat_at=CURRENT_TIMESTAMP,
                       worker_status=excluded.worker_status,
                       last_error_at=CASE WHEN excluded.last_error_operation<>'' THEN CURRENT_TIMESTAMP ELSE runtime_workers.last_error_at END,
                       last_error_operation=CASE WHEN excluded.last_error_operation<>'' THEN excluded.last_error_operation ELSE runtime_workers.last_error_operation END,
                       last_error_message=CASE WHEN excluded.last_error_operation<>'' THEN excluded.last_error_message ELSE runtime_workers.last_error_message END,
                       current_backoff_seconds=excluded.current_backoff_seconds,
                       restart_count_last_hour=excluded.restart_count_last_hour,
                       restart_limit_exceeded=excluded.restart_limit_exceeded,
                       updated_at=CURRENT_TIMESTAMP""",
                (
                    worker_id,
                    worker_kind,
                    instance_group[:120],
                    get_settings().application_version,
                    generation_id[:160],
                    generation_started_at[:40],
                    supervisor_task_status[:24],
                    parent_generation_id[:160],
                    child_generation_id[:160],
                    1 if replacement_blocked else 0,
                    1 if orphan_worker_possible else 0,
                    failure_reason[:120],
                    status,
                    error_operation[:80],
                    error_operation[:80],
                    error_message[:500],
                    max(0.0, current_backoff_seconds),
                    max(0, restart_count),
                    1 if restart_limit_exceeded else 0,
                ),
            )

    def recover(self) -> None:
        init_db()
        with closing(get_connection()) as db, db:
            db.execute("BEGIN IMMEDIATE"); self._recover_in_connection(db); db.commit()

    def _recover_in_connection(self, db: sqlite3.Connection) -> None:
        db.execute(
            """UPDATE runtime_jobs SET status='failed',last_error='worker_execution_attempts_exhausted',
               completed_at=CURRENT_TIMESTAMP,locked_at=NULL,lease_expires_at=NULL,locked_by='',
               updated_at=CURRENT_TIMESTAMP
               WHERE (status='pending' AND attempts>=max_attempts) OR
                     (status='running' AND attempts>=max_attempts AND lease_expires_at IS NOT NULL
                      AND lease_expires_at<=CURRENT_TIMESTAMP)"""
        )

    def is_supervisor_generation_acknowledged(self, *, supervisor_id: str, generation_id: str) -> bool:
        init_db()
        with closing(get_connection()) as db, db:
            row = db.execute(
                """SELECT terminal_state_acknowledged_at FROM runtime_workers
                   WHERE worker_id=? AND worker_kind='supervisor' AND generation_id=?""",
                (supervisor_id, generation_id),
            ).fetchone()
        return row is not None and bool(row["terminal_state_acknowledged_at"])

    def reset_terminal_supervisor_failure(
        self,
        *,
        instance_group: str = "",
        generation_id: str = "",
        force_replacement: bool = False,
    ) -> dict[str, Any]:
        """Acknowledge one current terminal generation without deleting history."""
        init_db()
        settings = get_settings()
        if settings.runtime_worker_mode != "embedded" or not settings.runtime_worker_enabled:
            raise ValueError("Embedded runtime supervision is not enabled.")
        freshness_seconds = max(1, settings.runtime_worker_heartbeat_seconds * 3)
        with closing(get_connection()) as db, db:
            rows = [
                dict(row)
                for row in db.execute("SELECT * FROM runtime_workers WHERE worker_kind='supervisor'").fetchall()
            ]
            current_rows = _latest_supervisor_generations(rows)
            terminal_rows = [row for row in current_rows if _is_terminal_supervisor_row(row)]
            if not instance_group:
                groups = {_supervisor_group(row) for row in terminal_rows}
                if len(groups) != 1:
                    raise ValueError("instance_group is required when more than one supervisor instance exists.")
                instance_group = next(iter(groups), "")
            selected = [row for row in current_rows if _supervisor_group(row) == instance_group]
            if not selected:
                raise ValueError("Supervisor instance group was not found.")
            current = selected[0]
            if generation_id and str(current.get("generation_id") or "") != generation_id:
                raise ValueError("Supervisor generation is not current for this instance group.")
            if _is_recent(str(current.get("last_heartbeat_at") or ""), freshness_seconds) and _worker_status(current) in {"online", "degraded"}:
                raise ValueError("A healthy supervisor generation is already active.")
            if not _is_terminal_supervisor_row(current):
                raise ValueError("Supervisor generation is not in a terminal failed state.")
            if bool(current.get("replacement_blocked")) and not force_replacement:
                raise ValueError("Replacement is blocked until child shutdown is confirmed; an explicit admin override is required.")
            db.execute(
                """UPDATE runtime_workers
                   SET terminal_state_acknowledged_at=CURRENT_TIMESTAMP,
                       restart_limit_exceeded=0,
                       replacement_blocked=CASE WHEN ? THEN 0 ELSE replacement_blocked END,
                       supervisor_task_status='stopped',
                       updated_at=CURRENT_TIMESTAMP
                   WHERE worker_id=? AND generation_id=? AND worker_kind='supervisor'""",
                (1 if force_replacement else 0, current["worker_id"], current.get("generation_id") or ""),
            )
        return {"acknowledged_generations": 1, "instance_group": instance_group, "generation_id": str(current.get("generation_id") or "")}

    def health(self) -> dict[str, Any]:
        init_db()
        settings = get_settings()
        with closing(get_connection()) as db, db:
            counts = {
                str(row["status"]): int(row["count"])
                for row in db.execute("SELECT status,COUNT(*) count FROM runtime_jobs GROUP BY status").fetchall()
            }
            records = [dict(row) for row in db.execute("SELECT * FROM runtime_workers").fetchall()]
            oldest = db.execute(
                "SELECT CAST((julianday('now')-julianday(MIN(created_at)))*86400 AS INTEGER) seconds "
                "FROM runtime_jobs WHERE status='pending'"
            ).fetchone()
            expired = db.execute(
                "SELECT COUNT(*) count FROM runtime_jobs WHERE status='running' AND lease_expires_at<=CURRENT_TIMESTAMP"
            ).fetchone()
            completed = db.execute(
                "SELECT COUNT(*) count FROM runtime_jobs WHERE status='completed' AND completed_at>=datetime('now','-24 hours')"
            ).fetchone()
        freshness = max(1, settings.runtime_worker_heartbeat_seconds * 3)
        worker_rows = [row for row in records if str(row.get("worker_kind") or "worker") == "worker"]
        supervisor_rows = [row for row in records if str(row.get("worker_kind") or "worker") == "supervisor"] if settings.runtime_worker_mode == "embedded" else []
        recent_workers = [row for row in worker_rows if _is_recent(str(row.get("last_heartbeat_at") or ""), freshness)]
        active_workers = [row for row in recent_workers if _worker_status(row) in {"online", "degraded"}]
        current_supervisors = _latest_supervisor_generations(supervisor_rows) if settings.runtime_supervisor_generation_health_enabled else supervisor_rows
        recent_supervisors = [row for row in current_supervisors if _is_recent(str(row.get("last_heartbeat_at") or ""), freshness)]
        supervisor = _supervisor_health(current_supervisors, recent_supervisors)
        workers = _workers_health(worker_rows, recent_workers, active_workers)
        instances = _instance_health(
            worker_rows=worker_rows, supervisor_rows=supervisor_rows,
            freshness_seconds=freshness, worker_mode=settings.runtime_worker_mode,
            generation_health_enabled=settings.runtime_supervisor_generation_health_enabled,
        )
        if settings.runtime_instance_health_enabled:
            overall = _aggregate_instance_status(instances, worker_mode=settings.runtime_worker_mode)
        elif settings.runtime_worker_mode == "disabled":
            overall = "disabled"
        elif supervisor["status"] == "failed":
            overall = "failed"
        elif supervisor["status"] == "restarting" and not active_workers:
            overall = "restarting"
        elif active_workers:
            overall = "degraded" if any(_worker_status(row) == "degraded" for row in active_workers) or supervisor["status"] == "restarting" else "online"
        else:
            overall = "restarting" if supervisor["status"] == "restarting" else "offline"
        latest_error = _latest_error(records)
        latest_worker = _latest_row(worker_rows, "last_heartbeat_at")
        worker_backoff = max((float(row.get("current_backoff_seconds") or 0) for row in recent_workers), default=0.0)
        return {
            "application_version": settings.application_version,
            "environment": settings.environment,
            "worker_mode": settings.runtime_worker_mode,
            "overall_status": overall,
            "supervisor": supervisor,
            "workers": workers,
            "instances": instances,
            "capacity_available": any(bool(item["active_workers"]) for item in instances),
            "failed_instance_count": sum(item["overall_status"] == "failed" for item in instances),
            "pending_jobs": counts.get("pending", 0),
            "running_jobs": counts.get("running", 0),
            "failed_jobs": counts.get("failed", 0),
            "expired_leases": int(expired["count"]),
            "oldest_pending_seconds": oldest["seconds"],
            "completed_jobs_last_24_hours": int(completed["count"]),
            "worker_seen_recently": bool(active_workers),
            "last_heartbeat_at": str(latest_worker["last_heartbeat_at"]) if latest_worker else None,
            "latest_error": latest_error,
            "last_error_at": latest_error["at"] if latest_error else None,
            "last_error_operation": latest_error["operation"] if latest_error else "",
            "last_error_message": latest_error["message"] if latest_error else "",
            "current_backoff_seconds": max(float(supervisor["current_backoff_seconds"]), worker_backoff),
            "restart_count_last_hour": int(supervisor["restart_count_last_hour"]),
            "restart_limit_exceeded": bool(supervisor["restart_limit_exceeded"]),
        }



def _worker_status(row: dict[str, Any]) -> str:
    status = str(row.get("worker_status") or "offline")
    return status if status in {"online", "degraded", "restarting", "offline", "failed"} else "offline"


def _timestamp(value: str) -> datetime | None:
    try:
        normalized = value.strip().replace(" ", "T")
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        parsed = datetime.fromisoformat(normalized)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_recent(value: str, seconds: int) -> bool:
    timestamp = _timestamp(value)
    return timestamp is not None and (datetime.now(timezone.utc) - timestamp).total_seconds() <= seconds


def _latest_row(rows: list[dict[str, Any]], timestamp_field: str) -> dict[str, Any] | None:
    candidates = [row for row in rows if _timestamp(str(row.get(timestamp_field) or "")) is not None]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            _timestamp(str(row.get(timestamp_field) or "")) or datetime.min.replace(tzinfo=timezone.utc),
            str(row.get("worker_id") or ""),
        ),
    )


def _latest_error(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    row = _latest_row(
        [item for item in rows if str(item.get("last_error_at") or "") and str(item.get("last_error_message") or "")],
        "last_error_at",
    )
    if row is None:
        return None
    return {
        "at": str(row["last_error_at"]),
        "operation": str(row.get("last_error_operation") or ""),
        "message": str(row.get("last_error_message") or ""),
        "worker_id": str(row.get("worker_id") or ""),
        "worker_kind": str(row.get("worker_kind") or "worker"),
    }


def _supervisor_group(row: dict[str, Any]) -> str:
    group = str(row.get("instance_group") or "")
    # Empty groups are legacy records. Keep their history available without
    # allowing it to outrank a named generation from a current deployment.
    return group or f"legacy:{row.get('worker_id') or ''}"


def _generation_sort_key(row: dict[str, Any]) -> tuple[datetime, datetime, str]:
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    return (
        _timestamp(str(row.get("generation_started_at") or "")) or minimum,
        _timestamp(str(row.get("updated_at") or "")) or minimum,
        str(row.get("worker_id") or ""),
    )


def _latest_supervisor_generations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    named_groups_exist = any(str(row.get("instance_group") or "") for row in rows)
    candidates = [row for row in rows if str(row.get("instance_group") or "")] if named_groups_exist else rows
    latest_by_group: dict[str, dict[str, Any]] = {}
    for row in candidates:
        group = _supervisor_group(row)
        current = latest_by_group.get(group)
        if current is None or _generation_sort_key(row) > _generation_sort_key(current):
            latest_by_group[group] = row
    return list(latest_by_group.values())


def _is_terminal_supervisor_row(row: dict[str, Any]) -> bool:
    return (
        not row.get("terminal_state_acknowledged_at")
        and (bool(row.get("restart_limit_exceeded")) or _worker_status(row) == "failed")
    )


def _supervisor_health(
    rows: list[dict[str, Any]],
    recent_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    terminal_rows = [row for row in rows if _is_terminal_supervisor_row(row)]
    statuses = {_worker_status(row) for row in recent_rows}
    if terminal_rows:
        status = "failed"
    elif "online" in statuses:
        status = "online"
    elif "restarting" in statuses:
        status = "restarting"
    elif "degraded" in statuses:
        status = "degraded"
    else:
        status = "offline"
    current_rows = terminal_rows or recent_rows or rows
    latest = _latest_row(current_rows, "last_heartbeat_at")
    task_states = {str(row.get("supervisor_task_status") or "") for row in recent_rows}
    if terminal_rows:
        task_status = "failed"
    elif "running" in task_states:
        task_status = "running"
    elif "restarting" in task_states:
        task_status = "restarting"
    elif "stopped" in task_states:
        task_status = "stopped"
    else:
        task_status = "stopped"
    generation = _latest_row(rows, "generation_started_at")
    return {
        "status": status,
        "task_status": task_status,
        "terminal": bool(terminal_rows),
        "generation_id": str(generation.get("generation_id") or "") if generation else "",
        "generation_count": len(rows),
        "active_count": len(recent_rows),
        "last_heartbeat_at": str(latest["last_heartbeat_at"]) if latest else None,
        "restart_count_last_hour": max(
            (int(row.get("restart_count_last_hour") or 0) for row in current_rows),
            default=0,
        ),
        "restart_limit_exceeded": any(bool(row.get("restart_limit_exceeded")) for row in terminal_rows),
        "current_backoff_seconds": max(
            (float(row.get("current_backoff_seconds") or 0) for row in recent_rows),
            default=0.0,
        ),
        "latest_error": _latest_error(current_rows),
    }


def _runtime_instance_group(row: dict[str, Any]) -> str:
    group = str(row.get("instance_group") or "")
    return group or f"legacy:{row.get('worker_id') or ''}"


def _instance_health(
    *,
    worker_rows: list[dict[str, Any]],
    supervisor_rows: list[dict[str, Any]],
    freshness_seconds: int,
    worker_mode: str,
    generation_health_enabled: bool,
) -> list[dict[str, Any]]:
    groups = {_runtime_instance_group(row) for row in worker_rows + supervisor_rows}
    instances: list[dict[str, Any]] = []
    for group in sorted(groups):
        group_workers = [row for row in worker_rows if _runtime_instance_group(row) == group]
        group_supervisors = [row for row in supervisor_rows if _runtime_instance_group(row) == group]
        current_supervisors = (
            _latest_supervisor_generations(group_supervisors)
            if generation_health_enabled else group_supervisors
        )
        recent_supervisors = [row for row in current_supervisors if _is_recent(str(row.get("last_heartbeat_at") or ""), freshness_seconds)]
        recent_workers = [row for row in group_workers if _is_recent(str(row.get("last_heartbeat_at") or ""), freshness_seconds)]
        active_workers = [row for row in recent_workers if _worker_status(row) in {"online", "degraded"}]
        supervisor = _supervisor_health(current_supervisors, recent_supervisors)
        worker_summary = _workers_health(group_workers, recent_workers, active_workers)
        if active_workers:
            instance_status = "degraded" if (
                supervisor["status"] in {"failed", "degraded", "restarting"}
                or any(_worker_status(row) == "degraded" for row in active_workers)
            ) else "online"
        elif supervisor["status"] == "failed":
            instance_status = "failed"
        elif supervisor["status"] == "restarting":
            instance_status = "restarting"
        else:
            instance_status = "offline"
        instances.append({
            "instance_group": "" if group.startswith("legacy:") else group,
            "worker_mode": worker_mode,
            "overall_status": instance_status,
            "supervisor_status": supervisor["status"],
            "supervisor_task_status": supervisor["task_status"],
            "generation_id": supervisor["generation_id"],
            "terminal": supervisor["terminal"],
            "restart_limit_exceeded": supervisor["restart_limit_exceeded"],
            "replacement_blocked": any(bool(row.get("replacement_blocked")) for row in current_supervisors),
            "orphan_worker_possible": any(bool(row.get("orphan_worker_possible")) for row in current_supervisors),
            "failure_reason": next((str(row.get("failure_reason") or "") for row in current_supervisors if row.get("failure_reason")), ""),
            "active_workers": worker_summary["active_count"],
            "workers": worker_summary,
        })
    return instances


def _aggregate_instance_status(instances: list[dict[str, Any]], *, worker_mode: str) -> str:
    if worker_mode == "disabled":
        return "disabled"
    relevant_instances = [
        instance for instance in instances
        if int(instance["active_workers"]) or str(instance["supervisor_status"]) != "offline"
    ]
    active = sum(int(instance["active_workers"]) for instance in relevant_instances)
    statuses = {str(instance["overall_status"]) for instance in relevant_instances}
    if active:
        return "degraded" if statuses - {"online"} else "online"
    if relevant_instances and statuses == {"failed"}:
        return "failed"
    if "restarting" in statuses:
        return "restarting"
    return "offline"


def _workers_health(
    rows: list[dict[str, Any]],
    recent_rows: list[dict[str, Any]],
    active_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = _latest_row(rows, "last_heartbeat_at")
    return {
        "active_count": len(active_rows),
        "recent_count": len(recent_rows),
        "stale_count": max(0, len(rows) - len(recent_rows)),
        "online_count": sum(_worker_status(row) == "online" for row in active_rows),
        "degraded_count": sum(_worker_status(row) == "degraded" for row in active_rows),
        "offline_count": sum(_worker_status(row) == "offline" for row in recent_rows),
        "restarting_count": sum(_worker_status(row) == "restarting" for row in recent_rows),
        "failed_count": sum(_worker_status(row) == "failed" for row in recent_rows),
        "latest_heartbeat_at": str(latest["last_heartbeat_at"]) if latest else None,
        "latest_error": _latest_error(rows),
    }


def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
