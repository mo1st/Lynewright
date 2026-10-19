"""Durable, provider-neutral execution for stepped contract operations."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

from app.services.platform_services import PlatformOperationError, PlatformStore, PlatformValidationError
from app.services.tenant_schema import DEFAULT_TENANT_ID


MAX_ARGUMENT_BYTES = 256 * 1024
MAX_RETRIES = 3
MAX_STEPS = 200
DEFAULT_LEASE_SECONDS = 120
_SCHEMA_LOCK = threading.RLock()
_DB_LOCK = threading.RLock()
_TRANSIENT_CATEGORIES = frozenset({
    "RATE_LIMITED", "PROVIDER_UNAVAILABLE", "CONNECTION_FAILED", "TIMEOUT",
})
_InvokeFn = Callable[[str, str, dict[str, Any], str, Any], Any]


def _json(value: Any, *, label: str) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise PlatformValidationError(f"Contract step {label} must be JSON serializable.") from exc
    if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        raise PlatformValidationError(f"Contract step {label} exceeds the 256 KiB limit.")
    return encoded


def run_key_for(contract: str, operation: str, payload: dict[str, Any], *, scope: str = "") -> str:
    """Return a stable key for one stepped run and optional caller scope."""
    identity = _json([str(contract), str(operation), payload, str(scope)], label="identity")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _actor_dict(actor: Any) -> dict[str, Any]:
    if actor is None:
        return {}
    return {
        "client_id": str(getattr(actor, "client_id", "")),
        "name": str(getattr(actor, "name", "")),
        "client_type": str(getattr(actor, "client_type", "")),
        "permission_mode": str(getattr(actor, "permission_mode", "")),
        "granted_capabilities": sorted(str(item) for item in (getattr(actor, "granted_capabilities", ()) or ())),
        "enabled": bool(getattr(actor, "enabled", True)),
        "internal": bool(getattr(actor, "internal", False)),
    }


def _actor_from(value: dict[str, Any]) -> Any:
    if not value:
        return None
    from app.services.platform_capabilities import CapabilityActor

    return CapabilityActor(
        client_id=str(value.get("client_id") or ""),
        name=str(value.get("name") or ""),
        client_type=str(value.get("client_type") or ""),
        permission_mode=str(value.get("permission_mode") or ""),
        granted_capabilities=frozenset(str(item) for item in (value.get("granted_capabilities") or ())),
        enabled=bool(value.get("enabled", True)),
        internal=bool(value.get("internal", False)),
    )


def _result_json(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = "{}"
    if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        status = str(value.get("status") or "") if isinstance(value, dict) else ""
        return json.dumps({"status": status, "truncated": True}, separators=(",", ":"))
    return encoded


class ContractStepRunStore:
    """SQLite queue with one fenced, recoverable row per stepped run."""

    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = str(tenant_id or DEFAULT_TENANT_ID)
        with self._connection() as db:
            self.ensure_schema(db)

    @contextmanager
    def _connection(self):
        with _DB_LOCK:
            with self.store.connection() as db:
                yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        with _SCHEMA_LOCK:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS contract_step_runs (
                    tenant_id TEXT NOT NULL,
                    run_key TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    project_id TEXT NOT NULL DEFAULT '',
                    actor_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'queued',
                    worker_id TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at REAL NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0,
                    step_index INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    max_steps INTEGER NOT NULL DEFAULT 200,
                    resume_requested INTEGER NOT NULL DEFAULT 0,
                    notify_failure INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, run_key)
                );
                CREATE INDEX IF NOT EXISTS contract_step_runs_ready
                    ON contract_step_runs (tenant_id, status, available_at, created_at);
                """
            )
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(contract_step_runs)").fetchall()}
            if "metadata_json" not in columns:
                db.execute("ALTER TABLE contract_step_runs ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}' ")

    def schedule(
        self,
        *,
        contract: str,
        operation: str,
        payload: dict[str, Any],
        run_key: str,
        project_id: str,
        actor: Any,
        retry: bool = False,
        resume: bool = False,
        notify_failure: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        contract, operation = str(contract or "").strip(), str(operation or "").strip()
        run_key = str(run_key or "").strip()
        if not contract or not operation or not run_key:
            raise PlatformValidationError("Contract, operation, and run_key are required.")
        if len(run_key) > 256:
            raise PlatformValidationError("run_key exceeds 256 characters.")
        payload_json = _json(payload, label="payload")
        actor_json = _json(_actor_dict(actor), label="actor")
        metadata_json = _json(metadata or {}, label="metadata")
        project_id = str(project_id or "")
        now = time.time()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM contract_step_runs WHERE tenant_id=? AND run_key=?",
                (self.tenant_id, run_key),
            ).fetchone()
            matched_by_payload = False
            # A resume must reach the run that is actually waiting. The key it
            # names can belong to an earlier, already finished row for the same
            # payload (a review approval found a succeeded row and stopped, and
            # the waiting run never continued), so fall back to the payload.
            if resume and (row is None or str(row["status"]) not in {"waiting", "running"}):
                waiting = db.execute(
                    """SELECT * FROM contract_step_runs WHERE tenant_id=? AND contract=? AND operation=?
                       AND payload_json=? AND status IN ('waiting','running')
                       ORDER BY updated_at DESC,created_at DESC,run_key LIMIT 1""",
                    (self.tenant_id, contract, operation, payload_json),
                ).fetchone()
                if waiting is not None:
                    row = waiting
                    run_key = str(row["run_key"])
                    matched_by_payload = True
            if row is None and resume:
                row = db.execute(
                    """SELECT * FROM contract_step_runs WHERE tenant_id=? AND contract=? AND operation=?
                       AND payload_json=? AND status IN ('waiting','running')
                       ORDER BY updated_at DESC,created_at DESC,run_key LIMIT 1""",
                    (self.tenant_id, contract, operation, payload_json),
                ).fetchone()
                if row is not None:
                    run_key = str(row["run_key"])
                    matched_by_payload = True
            created = row is None
            if row is None:
                db.execute(
                    """INSERT INTO contract_step_runs
                    (tenant_id,run_key,contract,operation,payload_json,project_id,actor_json,
                     status,available_at,max_retries,max_steps,notify_failure,metadata_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?)""",
                    (self.tenant_id, run_key, contract, operation, payload_json, project_id, actor_json,
                     now, MAX_RETRIES, MAX_STEPS, int(notify_failure), metadata_json, now, now),
                )
            else:
                if (
                    str(row["contract"]) != contract
                    or str(row["operation"]) != operation
                    or str(row["payload_json"]) != payload_json
                    or (not matched_by_payload and str(row["project_id"] or "") != project_id)
                    or (not matched_by_payload and str(row["metadata_json"] or "{}") != metadata_json)
                ):
                    db.rollback()
                    raise PlatformValidationError("run_key was reused with different contract step data.")
                status = str(row["status"])
                if retry and status == "failed":
                    db.execute(
                        """UPDATE contract_step_runs SET status='queued',actor_json=?,worker_id='',claim_token='',
                           lease_expires_at=0,available_at=?,step_index=0,attempts=0,retry_count=0,resume_requested=0,
                           result_json=NULL,error='',notify_failure=?,updated_at=?
                           WHERE tenant_id=? AND run_key=? AND status='failed'""",
                        (actor_json, now, int(notify_failure), now, self.tenant_id, run_key),
                    )
                elif resume and status == "waiting":
                    db.execute(
                        """UPDATE contract_step_runs SET status='queued',actor_json=?,worker_id='',claim_token='',
                           lease_expires_at=0,available_at=?,step_index=step_index+1,attempts=0,retry_count=0,
                           resume_requested=0,result_json=NULL,error='',updated_at=?
                           WHERE tenant_id=? AND run_key=? AND status='waiting'""",
                        (actor_json, now, now, self.tenant_id, run_key),
                    )
                elif resume and status == "running":
                    db.execute(
                        """UPDATE contract_step_runs SET actor_json=?,resume_requested=1,updated_at=?
                           WHERE tenant_id=? AND run_key=? AND status='running'""",
                        (actor_json, now, self.tenant_id, run_key),
                    )
            db.commit()
        value = self.get(run_key)
        value["created"] = created
        return value

    def get(self, run_key: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM contract_step_runs WHERE tenant_id=? AND run_key=?",
                (self.tenant_id, str(run_key)),
            ).fetchone()
        if row is None:
            raise PlatformOperationError("Contract step run was not found.")
        return self._public(row)

    def claim_next(self, *, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any] | None:
        if not str(worker_id or "").strip():
            raise PlatformValidationError("Contract step claims require a worker id.")
        lease = max(10, min(int(lease_seconds), 900))
        now = time.time()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM contract_step_runs WHERE tenant_id=? AND (
                   (status='queued' AND available_at<=?) OR
                   (status='running' AND lease_expires_at>0 AND lease_expires_at<=?)
                   ) ORDER BY available_at,created_at,run_key LIMIT 1""",
                (self.tenant_id, now, now),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            run_key = str(row["run_key"])
            token = secrets.token_urlsafe(24)
            changed = db.execute(
                """UPDATE contract_step_runs SET status='running',worker_id=?,claim_token=?,lease_expires_at=?,
                   attempts=attempts+1,updated_at=? WHERE tenant_id=? AND run_key=? AND
                   ((status='queued' AND available_at<=?) OR (status='running' AND lease_expires_at<=?))""",
                (worker_id, token, now + lease, now, self.tenant_id, run_key, now, now),
            ).rowcount
            if changed != 1:
                db.rollback()
                return None
            db.commit()
            claimed = db.execute(
                "SELECT * FROM contract_step_runs WHERE tenant_id=? AND run_key=?",
                (self.tenant_id, run_key),
            ).fetchone()
        value = self._public(claimed)
        value.update(
            claim_token=token,
            payload=json.loads(str(claimed["payload_json"])),
            actor=_actor_from(json.loads(str(claimed["actor_json"] or "{}"))),
        )
        return value

    def heartbeat(self, run_key: str, *, worker_id: str, claim_token: str, lease_seconds: int) -> bool:
        now = time.time()
        lease = max(10, min(int(lease_seconds), 900))
        with self._connection() as db, db:
            updated = db.execute(
                """UPDATE contract_step_runs SET lease_expires_at=?,updated_at=?
                   WHERE tenant_id=? AND run_key=? AND status='running' AND worker_id=? AND claim_token=?""",
                (now + lease, now, self.tenant_id, str(run_key), str(worker_id), str(claim_token)),
            ).rowcount
        return updated == 1

    def complete_step(
        self, run: dict[str, Any], *, worker_id: str, result: Any, max_retries: int = MAX_RETRIES,
    ) -> dict[str, Any] | None:
        status = str(result.get("status") or "").strip().lower() if isinstance(result, dict) else ""
        result_json = _result_json(result)
        now = time.time()
        waiting = status in {"waiting", "awaiting_review", "awaiting_choice", "waiting_review", "waiting_external"}
        stop_after_one = bool((run.get("metadata") or {}).get("stop_after_one"))
        is_running = status == "running" and not stop_after_one
        explicit_failure = status in {"failed", "failure", "error"}
        message = ""
        next_status = "succeeded"
        step_index = int(run.get("step_index") or 0)
        retry_count = int(run.get("retry_count") or 0)
        if is_running and step_index + 1 >= int(run.get("max_steps") or MAX_STEPS):
            explicit_failure = True
            message = f"Step limit of {int(run.get('max_steps') or MAX_STEPS)} reached."
        if explicit_failure:
            result_retryable = isinstance(result, dict) and result.get("retryable") is True
            if result_retryable and retry_count < int(run.get("max_retries") or max_retries):
                retry_count += 1
                next_status = "queued"
            else:
                next_status = "failed"
                retry_count = retry_count + 1 if result_retryable else max(1, retry_count)
                if not message:
                    last_error = result.get("last_error") if isinstance(result, dict) else None
                    if isinstance(last_error, dict):
                        last_error = last_error.get("message")
                    message = str(last_error or (result.get("error") if isinstance(result, dict) else "") or status)[:500]
        elif waiting:
            next_status = "waiting"
        elif is_running:
            next_status = "queued"
        requeue_delay = 0.0
        if is_running and isinstance(result, dict):
            try:
                requeue_delay = max(0.0, min(float(result.get("requeue_after_seconds") or 0), 300.0))
            except (TypeError, ValueError):
                requeue_delay = 0.0
        delay = (
            min(300.0, 2.0 ** (retry_count - 1) * 5.0)
            if explicit_failure and next_status == "queued"
            else requeue_delay if is_running else 0.0
        )
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT resume_requested FROM contract_step_runs WHERE tenant_id=? AND run_key=? AND status='running' AND worker_id=? AND claim_token=?",
                (self.tenant_id, str(run["run_key"]), worker_id, str(run.get("claim_token") or "")),
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            resume_requested = bool(row["resume_requested"])
            if waiting and resume_requested:
                next_status = "queued"
            updated = db.execute(
                """UPDATE contract_step_runs SET status=?,worker_id='',claim_token='',lease_expires_at=0,
                   available_at=?,step_index=step_index+?,attempts=0,retry_count=?,resume_requested=0,
                   result_json=?,error=?,updated_at=? WHERE tenant_id=? AND run_key=? AND status='running'
                   AND worker_id=? AND claim_token=?""",
                (next_status, now + delay, int(next_status == "queued" and not explicit_failure),
                 retry_count if explicit_failure else 0, result_json, message, now,
                 self.tenant_id, str(run["run_key"]), worker_id, str(run.get("claim_token") or "")),
            ).rowcount
            db.commit()
        if updated != 1:
            return None
        return self.get(str(run["run_key"]))

    def cancel(self, run_key: str) -> bool:
        now = time.time()
        with self._connection() as db, db:
            changed = db.execute(
                """UPDATE contract_step_runs SET status='cancelled',worker_id='',claim_token='',
                   lease_expires_at=0,updated_at=? WHERE tenant_id=? AND run_key=?
                   AND status IN ('queued','running','waiting')""",
                (now, self.tenant_id, str(run_key)),
            ).rowcount
        return changed == 1

    def fail_or_retry(
        self, run: dict[str, Any], *, worker_id: str, error: BaseException,
    ) -> tuple[dict[str, Any] | None, bool]:
        retryable = _is_retryable(error)
        next_retry = int(run.get("retry_count") or 0) + 1
        failed = not retryable or next_retry > int(run.get("max_retries") or MAX_RETRIES)
        status = "failed" if failed else "queued"
        delay = min(300.0, 2.0 ** (next_retry - 1) * 5.0)
        now = time.time()
        with self._connection() as db, db:
            updated = db.execute(
                """UPDATE contract_step_runs SET status=?,worker_id='',claim_token='',lease_expires_at=0,
                   available_at=?,retry_count=?,attempts=0,error=?,updated_at=?
                   WHERE tenant_id=? AND run_key=? AND status='running' AND worker_id=? AND claim_token=?""",
                (status, now + (0 if failed else delay), next_retry, str(error)[:500], now,
                 self.tenant_id, str(run["run_key"]), worker_id, str(run.get("claim_token") or "")),
            ).rowcount
        if updated != 1:
            return None, False
        return self.get(str(run["run_key"])), failed

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        try:
            result = json.loads(str(row["result_json"])) if row["result_json"] else None
        except (TypeError, ValueError):
            result = None
        return {
            "tenant_id": str(row["tenant_id"]),
            "run_key": str(row["run_key"]),
            "contract": str(row["contract"]),
            "operation": str(row["operation"]),
            "project_id": str(row["project_id"] or ""),
            "status": str(row["status"]),
            "worker_id": str(row["worker_id"] or ""),
            "step_index": int(row["step_index"] or 0),
            "attempts": int(row["attempts"] or 0),
            "retry_count": int(row["retry_count"] or 0),
            "max_retries": int(row["max_retries"] or MAX_RETRIES),
            "notify_failure": bool(row["notify_failure"]),
            "metadata": json.loads(str(row["metadata_json"] or "{}")),
            "result": result,
            "error": str(row["error"] or ""),
        }


def _is_retryable(exc: BaseException) -> bool:
    if getattr(exc, "retryable", False) is True:
        return True
    category = getattr(exc, "category", "")
    return str(getattr(category, "value", category) or "").upper() in _TRANSIENT_CATEGORIES


def _default_invoke(contract: str, operation: str, payload: dict[str, Any], project_id: str, actor: Any) -> Any:
    from app.services.operator_dispatch import _contract_registry

    return _contract_registry().invoke(contract, operation, payload, project_id=project_id, actor=actor)


def _tenant_invoke(run: dict[str, Any]) -> _InvokeFn:
    tenant_id = str(run.get("tenant_id") or DEFAULT_TENANT_ID)
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}

    def invoke(contract: str, operation: str, payload: dict[str, Any], project_id: str, actor: Any) -> Any:
        if metadata.get("line_run_id"):
            from app.services.integration_operations import build_tenant_operation_catalog

            catalogue = build_tenant_operation_catalog(tenant_id)
            resolved = catalogue.resolve_operation(
                actor, project_id=project_id,
                operation_id=str(metadata.get("operation_catalog_id") or ""),
                contract=contract, operation=operation,
            )
            # Every transport enters through this one invocation path. Native
            # service operations still reach IntegrationServiceContractRegistry
            # inside the catalogue, preserving provider selection and async
            # decoration while sharing the same prepared-execution lifecycle.
            descriptor = resolved["descriptor"]
            arguments = dict(payload)
            schema = descriptor.get("input_schema") if isinstance(descriptor.get("input_schema"), dict) else {}
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            if "project_id" in properties:
                arguments.setdefault("project_id", project_id)
            if not descriptor.get("read_only") and "idempotency_key" in properties:
                identity_parts = [str(metadata.get(key) or "") for key in ("line_run_id", "stage_id", "phase")]
                if metadata.get("item_index") is not None:
                    identity_parts.append(str(metadata["item_index"]))
                identity = ":".join(identity_parts)
                arguments.setdefault("idempotency_key", "line-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48])
            execution = catalogue.invoke(
                actor, project_id=project_id, operation_id=resolved["operation_id"], arguments=arguments,
                invocation_key=str(run.get("run_key") or ""),
            )
            if isinstance(execution, dict) and "execution_id" in execution and "result" in execution:
                return execution["result"]
            return execution

        from app.services.capability_dispatch import get_contract_registry
        return get_contract_registry(tenant_id).invoke(
            contract, operation, payload, project_id=project_id, actor=actor,
        )

    return invoke


def _notify_line_update(run: dict[str, Any] | None) -> None:
    if not run or not (run.get("metadata") or {}).get("line_run_id"):
        return
    try:
        from app.services.line_engine import handle_runner_update

        handle_runner_update(run)
    except Exception as exc:
        from app.services.application_logging import log_event

        log_event(level="error", category="production_line", source="line_runner",
                  message="Could not apply a durable operation update to its line stage.",
                  details={"run_key": str(run.get("run_key") or ""), "error": type(exc).__name__})


def _notify_failure(run: dict[str, Any]) -> None:
    if not run.get("notify_failure") or not run.get("project_id"):
        return
    try:
        import asyncio

        from app.services.notification_gateway import send_notification

        step_index = max(1, int(run.get("step_index") or 0))
        error = str(run.get("error") or "")[:300].strip()
        if error:
            diagnostic = f"Error: {error}"
        else:
            diagnostic = f"No error text was stored. Run key: {str(run.get('run_key') or 'unknown')}"
        asyncio.run(send_notification(
            tenant_id=str(run.get("tenant_id") or DEFAULT_TENANT_ID),
            project_id=str(run["project_id"]),
            event_class="integration_failure",
            title="Automated operation failed",
            body=(f"{run['contract']}.{run['operation']} failed at step {step_index} after "
                  f"{max(1, int(run.get('retry_count') or 0))} attempts. {diagnostic} "
                  "In the dashboard: Retry failed step."),
            intent="IMMEDIATE",
            dedupe_key=f"contract-step:{run['run_key']}",
            actor="system",
            data={"run_key": run["run_key"], "status": run.get("status")},
        ))
    except Exception:
        pass


def process_claimed_step(
    run: dict[str, Any],
    worker_id: str,
    *,
    store: ContractStepRunStore | None = None,
    invoke: _InvokeFn | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict[str, Any] | None:
    """Execute one claimed operation step and persist its next state."""
    active = store or ContractStepRunStore(tenant_id=str(run.get("tenant_id") or DEFAULT_TENANT_ID))
    stop = threading.Event()
    lease_lost = threading.Event()

    def renew() -> None:
        interval = max(1.0, min(float(lease_seconds) / 3.0, 30.0))
        while not stop.wait(interval):
            try:
                if not active.heartbeat(
                    str(run["run_key"]), worker_id=worker_id,
                    claim_token=str(run.get("claim_token") or ""), lease_seconds=lease_seconds,
                ):
                    lease_lost.set()
                    return
            except Exception:
                # A temporary heartbeat error is retried on the next interval.
                continue

    renewal = threading.Thread(target=renew, daemon=True, name="contract-step-lease")
    renewal.start()
    try:
        result = (invoke or _tenant_invoke(run))(
            str(run["contract"]), str(run["operation"]), dict(run.get("payload") or {}),
            str(run.get("project_id") or ""), run.get("actor"),
        )
    except Exception as exc:
        updated, failed = active.fail_or_retry(run, worker_id=worker_id, error=exc)
        if failed and updated is not None:
            _notify_failure(updated)
        _notify_line_update(updated)
        return updated
    finally:
        stop.set()
        renewal.join(timeout=1.0)
    if lease_lost.is_set():
        return None
    updated = active.complete_step(run, worker_id=worker_id, result=result)
    if updated is not None and updated["status"] == "failed":
        failure_result = updated.get("result")
        if not (isinstance(failure_result, dict) and failure_result.get("notified") is True):
            _notify_failure(updated)
    _notify_line_update(updated)
    return updated


def _worker_is_configured() -> bool:
    from app.config import get_settings

    settings = get_settings()
    return bool(settings.runtime_worker_enabled and settings.runtime_worker_mode != "disabled")


def schedule_step(
    contract: str,
    operation: str,
    payload: dict[str, Any],
    *,
    run_key: str,
    project_id: str = "",
    actor: Any = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    store: PlatformStore | None = None,
    invoke: _InvokeFn | None = None,
    on_failure: Callable[[BaseException], None] | None = None,
    retry: bool = False,
    resume: bool = False,
    notify_failure: bool = True,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Schedule a durable step, using a local thread only without a worker."""
    if _worker_is_configured() and invoke is None:
        return ContractStepRunStore(store, tenant_id=tenant_id).schedule(
            contract=contract, operation=operation, payload=payload, run_key=run_key,
            project_id=project_id, actor=actor, retry=retry, resume=resume,
            notify_failure=notify_failure, metadata=metadata,
        )
    from app.services import contract_driver

    kwargs: dict[str, Any] = {
        "contract": contract, "operation": operation, "payload": payload,
        "project_id": project_id, "actor": actor,
    }
    if invoke is not None:
        kwargs["invoke"] = invoke
    if on_failure is not None:
        kwargs["on_failure"] = on_failure
    if invoke is None and metadata and metadata.get("line_run_id"):
        kwargs["invoke"] = _tenant_invoke({
            "tenant_id": tenant_id, "metadata": metadata, "run_key": run_key,
        })
    if metadata and metadata.get("line_run_id"):
        # A polling stage can legitimately repeat the same operation and
        # payload after a pending result. Use the durable line-step identity
        # to avoid coalescing the next poll with the callback still unwinding
        # for the previous one.
        kwargs["dedupe_key"] = run_key

        def on_result(result: Any) -> bool:
            status = str(result.get("status") or "").lower() if isinstance(result, dict) else ""
            stop_after_one = bool(metadata.get("stop_after_one"))
            queue_status = "succeeded" if status == "running" and stop_after_one else "queued" if status == "running" else "waiting" if status in {"waiting", "awaiting_review", "awaiting_choice", "waiting_review", "waiting_external"} else "failed" if status in {"failed", "failure", "error"} else "succeeded"
            _notify_line_update({"tenant_id": tenant_id, "run_key": run_key, "status": queue_status,
                                 "result": result, "metadata": metadata, "error": str((result or {}).get("error") or "")})
            return not (status == "running" and stop_after_one)
        kwargs["on_result"] = on_result
        existing_failure = on_failure
        def on_line_failure(exc: BaseException) -> None:
            if existing_failure is not None:
                try:
                    existing_failure(exc)
                except Exception:
                    pass
            _notify_line_update({"tenant_id": tenant_id, "run_key": run_key, "status": "failed",
                                 "result": {}, "metadata": metadata, "error": str(exc)[:500]})
        kwargs["on_failure"] = on_line_failure
    thread = contract_driver.drive_in_background(**kwargs)
    return {"run_key": run_key, "status": "fallback", "started": thread is not None}


def get_run(
    run_key: str, *, tenant_id: str = DEFAULT_TENANT_ID, store: PlatformStore | None = None,
) -> dict[str, Any]:
    return ContractStepRunStore(store, tenant_id=tenant_id).get(run_key)


def cancel_run(
    run_key: str, *, tenant_id: str = DEFAULT_TENANT_ID, store: PlatformStore | None = None,
) -> bool:
    return ContractStepRunStore(store, tenant_id=tenant_id).cancel(run_key)


__all__ = [
    "ContractStepRunStore", "DEFAULT_LEASE_SECONDS", "MAX_RETRIES", "MAX_STEPS",
    "cancel_run", "get_run", "process_claimed_step", "run_key_for", "schedule_step",
]
