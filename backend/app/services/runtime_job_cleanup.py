"""Bounded retention for resolved runtime jobs and inactive worker records."""
from contextlib import closing
from app.config import get_settings
from app.database import get_connection, init_db


def cleanup_runtime_jobs() -> None:
    init_db(); settings = get_settings()
    with closing(get_connection()) as db, db:
        for status, days in (("completed", settings.runtime_completed_job_retention_days), ("cancelled", settings.runtime_cancelled_job_retention_days), ("failed", settings.runtime_failed_job_retention_days)):
            db.execute("DELETE FROM runtime_jobs WHERE status=? AND completed_at<datetime('now', ?)", (status, f"-{days} days"))
        db.execute("DELETE FROM runtime_workers WHERE last_heartbeat_at<datetime('now', ?)", (f"-{settings.runtime_worker_retention_days} days",))
