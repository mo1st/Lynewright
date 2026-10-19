"""Classification for generic runtime worker infrastructure failures."""
from __future__ import annotations

import sqlite3
from enum import Enum


class WorkerErrorClass(str, Enum):
    TRANSIENT = "transient"
    FATAL = "fatal"
    JOB_LOCAL = "job_local"


_TRANSIENT_MESSAGES = (
    "database is locked", "database is busy", "disk i/o error",
    "temporary disk i/o error", "unable to open database file",
    "temporarily unavailable", "connection reset", "connection refused",
)
_FATAL_MESSAGES = (
    "no such table", "no such column", "malformed database schema",
    "database disk image is malformed", "schema has changed", "syntax error",
)


def classify_worker_error(exc: Exception, *, operation: str) -> WorkerErrorClass:
    """Classify generic queue/worker failures without domain-specific errors."""
    del operation
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        if any(fragment in message for fragment in _TRANSIENT_MESSAGES):
            return WorkerErrorClass.TRANSIENT
        if any(fragment in message for fragment in _FATAL_MESSAGES):
            return WorkerErrorClass.FATAL
        return WorkerErrorClass.FATAL
    if isinstance(exc, sqlite3.DatabaseError):
        return WorkerErrorClass.FATAL
    if isinstance(exc, (TypeError, AttributeError, ImportError, ModuleNotFoundError, NotImplementedError)):
        return WorkerErrorClass.FATAL
    if isinstance(exc, (KeyError, ValueError, sqlite3.ProgrammingError)):
        return WorkerErrorClass.JOB_LOCAL
    return WorkerErrorClass.TRANSIENT
