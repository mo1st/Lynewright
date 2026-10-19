"""Durable API-to-runtime-worker handoff for integration callbacks.

The API and MCP processes intentionally cannot reach deployment-owned lifecycle
controls or private render networks.  Integrations that declare
``execution_plane=worker`` or ``execution_plane=container`` therefore cross
this small, database-backed seam.
The queue is provider-neutral: it carries a contribution id, operation
arguments, and the host-created capability context; the worker resolves the
same compiled contribution in its own process and executes it there.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any

from app.services.capability_context import CapabilityInvocationContext
from app.services.platform_services import PlatformOperationError, PlatformStore, PlatformValidationError
from app.services.tenant_schema import DEFAULT_TENANT_ID


MAX_ARGUMENT_BYTES = 256 * 1024
DEFAULT_LEASE_SECONDS = 180
DEFAULT_WAIT_SECONDS = 75.0
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
_WORKER_SCHEMA_LOCK = threading.RLock()
_WORKER_DB_LOCK = threading.RLock()


def _json(value: Any, *, label: str) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PlatformValidationError(f"Worker integration {label} must be JSON serializable.") from exc
    if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise PlatformValidationError(f"Worker integration {label} exceeds the 256 KiB limit.")
    return encoded


def _decode(value: str, *, label: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise PlatformOperationError(f"Worker integration {label} is malformed.") from exc


def _context_dict(context: CapabilityInvocationContext | None) -> dict[str, Any]:
    if context is None:
        return {}
    return asdict(context)


def _stable_context_json(value: str) -> str:
    """Ignore per-attempt tracing fields when checking idempotency reuse."""
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return value
    if isinstance(decoded, dict):
        decoded = dict(decoded)
        decoded.pop("trace_id", None)
        decoded.pop("parent_span_id", None)
    return json.dumps(decoded, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def context_from_dict(value: dict[str, Any]) -> CapabilityInvocationContext | None:
    if not value:
        return None
    allowed = {
        "tenant_id", "actor_type", "actor_id", "client_id", "project_id",
        "resource_type", "resource_id", "integration_id", "connection_id",
        "trace_id", "parent_span_id", "idempotency_key", "entry_point",
    }
    return CapabilityInvocationContext(**{key: value.get(key) for key in allowed})


class IntegrationWorkerInvocationStore:
    """SQLite queue with atomic claim, lease recovery, and idempotent enqueue."""

    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id

    @contextmanager
    def _locked_connection(self):
        """Serialize queue transactions when a store shares one SQLite handle."""
        with _WORKER_DB_LOCK:
            with self.store.connection() as db:
                yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        # The API and worker can initialize the same SQLite queue at the same
        # time.  Serialize the probe/migration on shared in-memory handles so
        # a stale PRAGMA result cannot race an ALTER TABLE.
        with _WORKER_SCHEMA_LOCK:
            IntegrationWorkerInvocationStore._ensure_schema_unlocked(db)

    @staticmethod
    def _ensure_schema_unlocked(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS integration_worker_invocations (
                tenant_id TEXT NOT NULL,
                id TEXT NOT NULL,
                contribution_id TEXT NOT NULL,
                integration_id TEXT NOT NULL DEFAULT '',
                execution_plane TEXT NOT NULL DEFAULT 'worker',
                operation TEXT NOT NULL,
                args_json TEXT NOT NULL,
                kwargs_json TEXT NOT NULL DEFAULT '{}',
                context_json TEXT NOT NULL DEFAULT '{}',
                idempotency_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                worker_id TEXT NOT NULL DEFAULT '',
                claim_token TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                result_json TEXT,
                error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, id)
            );
            CREATE INDEX IF NOT EXISTS integration_worker_invocations_ready
                ON integration_worker_invocations (tenant_id, status, lease_expires_at, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS integration_worker_invocations_idempotency
                ON integration_worker_invocations (tenant_id, contribution_id, operation, idempotency_key)
                WHERE idempotency_key <> '';
            """
        )
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(integration_worker_invocations)").fetchall()}
        for name, declaration in (
            ("integration_id", "TEXT NOT NULL DEFAULT ''"),
            ("execution_plane", "TEXT NOT NULL DEFAULT 'worker'"),
        ):
            if name not in columns:
                try:
                    db.execute(f"ALTER TABLE integration_worker_invocations ADD COLUMN {name} {declaration}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
        if "claim_token" not in columns:
            try:
                db.execute("ALTER TABLE integration_worker_invocations ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        db.execute(
            """CREATE INDEX IF NOT EXISTS integration_worker_invocations_runner
               ON integration_worker_invocations (tenant_id, execution_plane, integration_id, status, created_at)"""
        )

    def enqueue(
        self,
        *,
        contribution_id: str,
        integration_id: str = "",
        execution_plane: str = "worker",
        operation: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        invocation_context: CapabilityInvocationContext | None,
    ) -> dict[str, Any]:
        if not contribution_id or not operation:
            raise PlatformValidationError("Worker integration invocation identity is required.")
        if execution_plane not in {"worker", "container"}:
            raise PlatformValidationError("Integration invocation execution plane is invalid.")
        if execution_plane == "container" and not integration_id:
            raise PlatformValidationError("Container integration invocations require an integration id.")
        args_json = _json(list(args), label="arguments")
        kwargs_json = _json(kwargs, label="keyword arguments")
        context_json = _json(_context_dict(invocation_context), label="context")
        idempotency_key = str(getattr(invocation_context, "idempotency_key", "") or "").strip()
        request_id = f"iw_{secrets.token_urlsafe(18)}"
        with self._locked_connection() as db, db:
            self.ensure_schema(db)
            if idempotency_key:
                row = db.execute(
                    """SELECT * FROM integration_worker_invocations
                       WHERE tenant_id=? AND contribution_id=? AND operation=? AND idempotency_key=?""",
                    (self.tenant_id, contribution_id, operation, idempotency_key),
                ).fetchone()
                if row is not None:
                    if (
                        str(row["args_json"]) != args_json
                        or str(row["kwargs_json"]) != kwargs_json
                        or _stable_context_json(str(row["context_json"])) != _stable_context_json(context_json)
                        or str(row["integration_id"] or "") != integration_id
                        or str(row["execution_plane"] or "worker") != execution_plane
                    ):
                        raise PlatformValidationError(
                            "Worker integration idempotency key was reused with different arguments."
                        )
                    return self._public(row)
            try:
                db.execute(
                    """INSERT INTO integration_worker_invocations
                    (tenant_id,id,contribution_id,integration_id,execution_plane,operation,args_json,kwargs_json,context_json,idempotency_key)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (self.tenant_id, request_id, contribution_id, integration_id, execution_plane, operation, args_json, kwargs_json, context_json, idempotency_key),
                )
            except sqlite3.IntegrityError:
                if not idempotency_key:
                    raise
                row = db.execute(
                    """SELECT * FROM integration_worker_invocations
                       WHERE tenant_id=? AND contribution_id=? AND operation=? AND idempotency_key=?""",
                    (self.tenant_id, contribution_id, operation, idempotency_key),
                ).fetchone()
                if row is None:
                    raise
                if (
                    str(row["args_json"]) != args_json
                    or str(row["kwargs_json"]) != kwargs_json
                    or _stable_context_json(str(row["context_json"])) != _stable_context_json(context_json)
                    or str(row["integration_id"] or "") != integration_id
                    or str(row["execution_plane"] or "worker") != execution_plane
                ):
                    raise PlatformValidationError(
                        "Worker integration idempotency key was reused with different arguments."
                    )
                return self._public(row)
            return self._public(db.execute(
                "SELECT * FROM integration_worker_invocations WHERE tenant_id=? AND id=?",
                (self.tenant_id, request_id),
            ).fetchone())

    def get(self, request_id: str) -> dict[str, Any]:
        with self._locked_connection() as db:
            self.ensure_schema(db)
            row = db.execute(
                "SELECT * FROM integration_worker_invocations WHERE tenant_id=? AND id=?",
                (self.tenant_id, str(request_id)),
            ).fetchone()
        if row is None:
            raise PlatformOperationError("Worker integration invocation was not found.")
        return self._public(row)

    def claim_next(
        self, *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
        execution_plane: str = "worker", integration_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not worker_id:
            raise PlatformValidationError("Worker integration claims require a worker id.")
        if execution_plane not in {"worker", "container"}:
            raise PlatformValidationError("Integration invocation execution plane is invalid.")
        if execution_plane == "container" and not integration_id:
            raise PlatformValidationError("Container claims require an integration id.")
        lease = max(10, min(int(lease_seconds), 900))
        with self._locked_connection() as db, db:
            self.ensure_schema(db)
            db.execute("BEGIN IMMEDIATE")
            scope_sql = " AND execution_plane=?"
            scope_values: list[Any] = [self.tenant_id, execution_plane]
            if integration_id is not None:
                scope_sql += " AND integration_id=?"
                scope_values.append(str(integration_id))
            row = db.execute(
                """SELECT * FROM integration_worker_invocations
                   WHERE tenant_id=?""" + scope_sql + """ AND (
                     status='queued' OR
                     (status='running' AND lease_expires_at<>'' AND lease_expires_at<=CURRENT_TIMESTAMP)
                   ) ORDER BY created_at,id LIMIT 1""",
                scope_values,
            ).fetchone()
            if row is None:
                db.commit()
                return None
            request_id = str(row["id"])
            updated = db.execute(
                """UPDATE integration_worker_invocations
                   SET status='running',worker_id=?,claim_token=?,lease_expires_at=datetime('now',?),
                       attempts=attempts+1,updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND id=? AND execution_plane=? AND (status='queued' OR status='running')""",
                (worker_id, secrets.token_urlsafe(24), f"+{lease} seconds", self.tenant_id, request_id, execution_plane),
            ).rowcount
            if updated != 1:
                db.commit()
                return None
            db.commit()
            claimed = db.execute(
                "SELECT * FROM integration_worker_invocations WHERE tenant_id=? AND id=?",
                (self.tenant_id, request_id),
            ).fetchone()
            public = self._public(claimed)
            public["claim_token"] = str(claimed["claim_token"] or "")
            public.update(
                _args=_decode(str(claimed["args_json"]), label="arguments"),
                _kwargs=_decode(str(claimed["kwargs_json"]), label="keyword arguments"),
                _context=_decode(str(claimed["context_json"]), label="context"),
            )
            return public

    def get_for_runner(self, request_id: str, *, integration_id: str) -> dict[str, Any]:
        with self._locked_connection() as db:
            self.ensure_schema(db)
            row = db.execute(
                "SELECT * FROM integration_worker_invocations WHERE tenant_id=? AND id=?",
                (self.tenant_id, str(request_id)),
            ).fetchone()
        if row is None:
            raise PlatformOperationError("Worker integration invocation was not found.")
        value = self._public(row)
        value["_context"] = _decode(str(row["context_json"]), label="context")
        value["_args"] = _decode(str(row["args_json"]), label="arguments")
        value["_kwargs"] = _decode(str(row["kwargs_json"]), label="keyword arguments")
        if value["execution_plane"] != "container" or value["integration_id"] != str(integration_id):
            raise PlatformOperationError("Runner request is outside this token's integration scope.")
        return value

    def claim_for_runner(self, *, integration_id: str, runner_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any] | None:
        return self.claim_next(
            worker_id=runner_id, lease_seconds=lease_seconds,
            execution_plane="container", integration_id=integration_id,
        )

    def complete_for_runner(
        self, request_id: str, *, integration_id: str, runner_id: str,
        claim_token: str, result: Any,
    ) -> dict[str, Any]:
        self.get_for_runner(request_id, integration_id=integration_id)
        return self._finish(
            request_id, worker_id=runner_id, claim_token=claim_token,
            status="succeeded", result_json=_json(result, label="result"), error="",
            integration_id=integration_id,
        )

    def fail_for_runner(
        self, request_id: str, *, integration_id: str, runner_id: str,
        claim_token: str, error: str,
    ) -> dict[str, Any]:
        self.get_for_runner(request_id, integration_id=integration_id)
        return self._finish(
            request_id, worker_id=runner_id, claim_token=claim_token,
            status="failed", result_json=None, error=str(error)[:500],
            integration_id=integration_id,
        )

    def complete(self, request_id: str, *, worker_id: str, result: Any, claim_token: str | None = None) -> dict[str, Any]:
        encoded = _json(result, label="result")
        return self._finish(request_id, worker_id=worker_id, claim_token=claim_token, status="succeeded", result_json=encoded, error="")

    def fail(self, request_id: str, *, worker_id: str, error: str, claim_token: str | None = None) -> dict[str, Any]:
        return self._finish(request_id, worker_id=worker_id, claim_token=claim_token, status="failed", result_json=None, error=str(error)[:500])

    def recover_expired(self) -> int:
        with self._locked_connection() as db, db:
            self.ensure_schema(db)
            return int(db.execute(
                """UPDATE integration_worker_invocations
                   SET status='queued',worker_id='',claim_token='',lease_expires_at='',updated_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND status='running' AND lease_expires_at<>'' AND lease_expires_at<=CURRENT_TIMESTAMP""",
                (self.tenant_id,),
            ).rowcount)

    def wait(self, request_id: str, *, timeout_seconds: float = DEFAULT_WAIT_SECONDS, poll_seconds: float = 0.25) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            value = self.get(request_id)
            if value["status"] in TERMINAL_STATES:
                if value["status"] == "failed":
                    raise PlatformOperationError("Worker integration invocation failed.")
                return value
            if time.monotonic() >= deadline:
                return value
            time.sleep(min(max(0.05, poll_seconds), max(0.05, deadline - time.monotonic())))

    def _finish(
        self, request_id: str, *, worker_id: str, claim_token: str | None,
        status: str, result_json: str | None, error: str, integration_id: str | None = None,
    ) -> dict[str, Any]:
        with self._locked_connection() as db, db:
            self.ensure_schema(db)
            if claim_token:
                db.execute(
                    """UPDATE integration_worker_invocations
                       SET status=?,result_json=?,error=?,worker_id='',claim_token='',lease_expires_at='',updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=? AND id=? AND status='running' AND worker_id=? AND claim_token=?""" +
                    (" AND integration_id=? AND execution_plane='container'" if integration_id is not None else ""),
                    (status, result_json, error, self.tenant_id, str(request_id), worker_id, claim_token,
                     *([integration_id] if integration_id is not None else [])),
                )
            else:
                # Compatibility for older workers/tests. New workers always
                # fence completion with the claim token.
                db.execute(
                    """UPDATE integration_worker_invocations
                       SET status=?,result_json=?,error=?,worker_id='',claim_token='',lease_expires_at='',updated_at=CURRENT_TIMESTAMP
                       WHERE tenant_id=? AND id=? AND status='running' AND worker_id=?""",
                    (status, result_json, error, self.tenant_id, str(request_id), worker_id),
                )
            row = db.execute(
                "SELECT * FROM integration_worker_invocations WHERE tenant_id=? AND id=?",
                (self.tenant_id, str(request_id)),
            ).fetchone()
        if row is None:
            raise PlatformOperationError("Worker integration invocation was not found.")
        return self._public(row)

    @staticmethod
    def _public(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise PlatformOperationError("Worker integration invocation was not found.")
        return {
            "id": str(row["id"]),
            "contribution_id": str(row["contribution_id"]),
            "integration_id": str(row["integration_id"] or ""),
            "execution_plane": str(row["execution_plane"] or "worker"),
            "operation": str(row["operation"]),
            "status": str(row["status"]),
            "worker_id": str(row["worker_id"] or ""),
            "attempts": int(row["attempts"] or 0),
            "result": _decode(str(row["result_json"]), label="result") if row["result_json"] else None,
            "error": str(row["error"] or ""),
        }


class DatabaseWorkerInvocationBridge:
    """API-side callback adapter that waits briefly for worker completion."""

    def __init__(self, store: PlatformStore, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.queue = IntegrationWorkerInvocationStore(store, tenant_id=tenant_id)

    def invoke(
        self,
        contribution: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        invocation_context: CapabilityInvocationContext | None,
        timeout_seconds: float | None = None,
    ) -> Any:
        # ``sdk_scoped`` providers receive a request-scoped IntegrationSDK as
        # their first handler argument.  Worker queues are intentionally JSON
        # only, so never attempt to serialize that runtime facade.  Carry the
        # SDK caller as a safe, non-secret actor descriptor instead; the worker
        # reconstructs a fresh SDK with the original invocation context before
        # calling the provider.  This keeps the seam generic for every current
        # and future worker-bound sdk-scoped integration.
        queued_args = args
        queued_kwargs = dict(kwargs)
        if str(contribution.metadata.get("invocation") or "direct") == "sdk_scoped":
            if not args or not hasattr(args[0], "caller"):
                raise PlatformValidationError(
                    "Worker sdk-scoped integrations require a request-scoped SDK."
                )
            caller = args[0].caller
            queued_args = args[1:]
            queued_kwargs["_lynewright_sdk_actor"] = {
                "client_id": str(getattr(caller, "client_id", "")),
                "name": str(getattr(caller, "name", "")),
                "client_type": str(getattr(caller, "client_type", "")),
                "permission_mode": str(getattr(caller, "permission_mode", "")),
                "granted_capabilities": sorted(str(item) for item in (getattr(caller, "granted_capabilities", ()) or ())),
                "enabled": bool(getattr(caller, "enabled", True)),
                "internal": bool(getattr(caller, "internal", False)),
            }
        request = self.queue.enqueue(
            contribution_id=str(contribution.id),
            operation=str((args[1] if str(contribution.metadata.get("invocation") or "direct") == "sdk_scoped" and len(args) > 1 else args[0]) if args else ""),
            args=queued_args,
            kwargs=queued_kwargs,
            invocation_context=invocation_context,
        )
        result = self.queue.wait(
            request["id"],
            timeout_seconds=max(1.0, min(float(timeout_seconds or DEFAULT_WAIT_SECONDS) - 5.0, DEFAULT_WAIT_SECONDS)),
        )
        if result["status"] == "succeeded":
            return result["result"]
        # The caller receives a truthful in-progress acknowledgement when a
        # provider's accepted operation legitimately outlives the API wait.
        return {
            "accepted": True,
            "execution_id": result["id"],
            "state": result["status"],
        }


class DatabaseContainerInvocationBridge:
    """API-side durable handoff for a separately authenticated integration runner."""

    def __init__(self, store: PlatformStore) -> None:
        self.store = store

    def invoke(
        self,
        contribution: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        invocation_context: CapabilityInvocationContext | None,
        timeout_seconds: float | None = None,
    ) -> Any:
        from app.services.integration_runner import IntegrationRunnerService

        integration_id = str(contribution.integration_id)
        tenant_id = str(getattr(invocation_context, "tenant_id", None) or DEFAULT_TENANT_ID)
        runners = IntegrationRunnerService(self.store, tenant_id=tenant_id)
        if runners.status(integration_id)["status"] != "connected":
            raise PlatformOperationError("Container runner not connected.")

        queued_args = args
        queued_kwargs = dict(kwargs)
        sdk_scoped = str(contribution.metadata.get("invocation") or "direct") == "sdk_scoped"
        if sdk_scoped:
            if not args or not hasattr(args[0], "caller"):
                raise PlatformValidationError("Container sdk-scoped integrations require a request-scoped SDK.")
            caller = args[0].caller
            queued_args = args[1:]
            queued_kwargs["_lynewright_sdk_actor"] = {
                "client_id": str(getattr(caller, "client_id", "")),
                "name": str(getattr(caller, "name", "")),
                "client_type": str(getattr(caller, "client_type", "")),
                "permission_mode": str(getattr(caller, "permission_mode", "")),
                "granted_capabilities": sorted(str(item) for item in (getattr(caller, "granted_capabilities", ()) or ())),
                "enabled": bool(getattr(caller, "enabled", True)),
                "internal": bool(getattr(caller, "internal", False)),
            }
        operation = str(
            (args[1] if sdk_scoped and len(args) > 1 else args[0]) if args else ""
        )
        queue = IntegrationWorkerInvocationStore(self.store, tenant_id=tenant_id)
        request = queue.enqueue(
            contribution_id=str(contribution.id), integration_id=integration_id,
            execution_plane="container", operation=operation,
            args=queued_args, kwargs=queued_kwargs,
            invocation_context=invocation_context,
        )
        timeout = max(1.0, min(float(timeout_seconds or 30.0), 300.0))
        try:
            result = queue.wait(request["id"], timeout_seconds=timeout)
        except PlatformOperationError as exc:
            current = queue.get(request["id"])
            if current["status"] == "failed":
                raise PlatformOperationError("Container integration operation failed.") from exc
            raise
        if result["status"] == "succeeded":
            return result["result"]
        if runners.status(integration_id)["status"] != "connected":
            raise PlatformOperationError("Container runner not connected.")
        raise PlatformOperationError("Container integration operation timed out.")


__all__ = [
    "DatabaseWorkerInvocationBridge",
    "DEFAULT_LEASE_SECONDS",
    "DatabaseContainerInvocationBridge",
    "IntegrationWorkerInvocationStore",
    "context_from_dict",
]
