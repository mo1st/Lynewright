"""Fault-tolerant durable worker for runtime verification jobs."""
from __future__ import annotations
from contextlib import closing

import os
import signal
import socket
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.database import get_connection, init_db
from app.services.application_logging import log_event
from app.services.platform_services import PlatformOperationError
from app.services.job_queue import DatabaseRuntimeJobQueue, complete_claimed_job_in_connection
from app.services.runtime_job_cleanup import cleanup_runtime_jobs
from app.services.integration_worker_invocations import (
    IntegrationWorkerInvocationStore,
    context_from_dict,
)
from app.services.line_runner import ContractStepRunStore, process_claimed_step
from app.workers.worker_backoff import WorkerBackoff
from app.workers.worker_errors import WorkerErrorClass, classify_worker_error
from app.workers.worker_health_publisher import WorkerHealthPublisher

RuntimeJobHandler = Any
_RUNTIME_JOB_HANDLERS: dict[str, RuntimeJobHandler] = {}


def register_runtime_job_handler(job_type: str, handler: RuntimeJobHandler) -> Any:
    """Register an optional runtime job type; the core registry starts empty."""
    key = str(job_type or "").strip()
    if not key or not callable(handler):
        raise ValueError("A job type and callable handler are required.")
    previous = _RUNTIME_JOB_HANDLERS.get(key)
    _RUNTIME_JOB_HANDLERS[key] = handler
    def unregister() -> bool:
        if _RUNTIME_JOB_HANDLERS.get(key) is not handler:
            return False
        if previous is None:
            _RUNTIME_JOB_HANDLERS.pop(key, None)
        else:
            _RUNTIME_JOB_HANDLERS[key] = previous
        return True
    return unregister

_stopping = False
_REVIEW_DELIVERY_SWEEP_SECONDS = 60
# The error class already decides retry behaviour; this turns it into the next
# action for whoever is reading the log.
_WORKER_ERROR_HINTS = {
    WorkerErrorClass.TRANSIENT: "Retried automatically after the backoff; only a sustained streak needs attention.",
    WorkerErrorClass.JOB_LOCAL: "One job is bad, not the worker — inspect that job's payload.",
    WorkerErrorClass.FATAL: "The worker cannot continue and exits; the supervisor restarts it.",
}


def is_transient_worker_error(exc: Exception) -> bool:
    """Compatibility helper used by diagnostics and existing tests."""
    return classify_worker_error(exc, operation="worker") is WorkerErrorClass.TRANSIENT


def is_fatal_worker_error(exc: Exception) -> bool:
    """Compatibility helper used by diagnostics and existing tests."""
    return classify_worker_error(exc, operation="worker") is WorkerErrorClass.FATAL


def _stop(*_: object) -> None:
    global _stopping
    _stopping = True


def _worker_id() -> str:
    return os.getenv("LYNEWRIGHT_RUNTIME_WORKER_ID", "").strip() or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _log(
    worker_id: str,
    operation: str,
    exc: Exception,
    delay: float,
    *,
    critical: bool = False,
    error_class: WorkerErrorClass | None = None,
    record_health: bool = True,
    instance_group: str = "",
) -> None:
    error_class = error_class or classify_worker_error(exc, operation=operation)
    fatal = critical or error_class is WorkerErrorClass.FATAL
    log_event(
        level="critical" if fatal else ("warning" if error_class is WorkerErrorClass.TRANSIENT else "error"),
        category="runtime_worker",
        source="runtime_job_worker",
        message=f"{operation} failed: {type(exc).__name__}: {str(exc)[:200]}",
        details={
            "worker_id": worker_id,
            "operation": operation,
            "error_class": error_class.value,
            "retry_delay_seconds": delay,
            "fatal": fatal,
            "instance_group": instance_group,
            "hint": _WORKER_ERROR_HINTS.get(error_class, ""),
        },
        # Carries type, cause chain and traceback — a worker failure is the one
        # place nobody can reproduce by hand from a message alone.
        exc=exc,
    )
    if not record_health:
        return
    try:
        DatabaseRuntimeJobQueue().record_worker_error(
            worker_id=worker_id,
            operation=operation,
            message=str(exc),
            backoff_seconds=delay,
            status="failed" if fatal else "degraded",
            instance_group=instance_group,
        )
    except Exception:
        # The original worker error remains the authoritative failure.  A broken
        # health write must not obscure it or turn a temporary DB error fatal.
        pass


def _wait(stop: threading.Event | None, delay: float) -> None:
    if stop:
        stop.wait(delay)
    else:
        time.sleep(delay)


def _sweep_stale_review_deliveries_safely(worker_id: str, *, instance_group: str = "") -> dict[str, int]:
    """Recover Telegram review and choice messages without blocking job polling."""
    from app.services import choice_channel, image_review_channel

    recovered: dict[str, int] = {}
    for operation, recover in (
        ("image_review_delivery_sweep", image_review_channel.recover_stale_deliveries),
        ("choice_delivery_sweep", choice_channel.recover_stale_deliveries),
    ):
        try:
            recovered[operation] = len(recover())
        except Exception as exc:
            recovered[operation] = 0
            _log(
                worker_id, operation, exc, _REVIEW_DELIVERY_SWEEP_SECONDS,
                error_class=classify_worker_error(exc, operation=operation),
                record_health=False, instance_group=instance_group,
            )
    try:
        from app.services.media_links import MediaLinksService

        recovered["media_link_expiry_sweep"] = MediaLinksService().sweep_expired_all()
    except Exception as exc:
        recovered["media_link_expiry_sweep"] = 0
        _log(
            worker_id, "media_link_expiry_sweep", exc, _REVIEW_DELIVERY_SWEEP_SECONDS,
            error_class=classify_worker_error(exc, operation="media_link_expiry_sweep"),
            record_health=False, instance_group=instance_group,
        )
    return recovered


def _main_loop_delay_or_raise(
    *,
    worker_id: str,
    instance_group: str,
    operation: str,
    exc: Exception,
    backoff: WorkerBackoff,
) -> float:
    error_class = classify_worker_error(exc, operation=operation)
    if error_class is WorkerErrorClass.FATAL:
        _log(
            worker_id,
            operation,
            exc,
            0,
            critical=True,
            error_class=error_class,
            instance_group=instance_group,
        )
        raise exc
    delay = backoff.next_delay()
    _log(
        worker_id,
        operation,
        exc,
        delay,
        error_class=error_class,
        instance_group=instance_group,
    )
    return delay


def _bootstrap_worker_integrations(*, worker_id: str, instance_group: str) -> dict[str, list[str]]:
    """Register and activate this process's own integration definitions.

    The worker, not the API, is the process attached to the renderer's network,
    so a provider that reaches an internal container has to be registered here
    as well. Runtime definitions are deliberately process-local, so this is a
    second registration rather than a duplicate one.

    This activates **only** the integrations this process built a definition
    for. It must not call `recover_enabled()`, which is what the API startup
    uses: that walks every enabled integration and writes a durable
    `status="error"` for any whose definition is missing from the calling
    process. Run here, where the API's source and social definitions do not
    exist, it would mark those integrations failed in the shared database and
    clobber the API's healthy state.

    Nothing in here is allowed to stop the worker. Job processing is the
    worker's actual contract; an unconfigured or misconfigured provider is
    reported and skipped.
    """
    outcome: dict[str, list[str]] = {"registered": [], "failed": [], "activated": []}
    try:
        from app.routers import integration_gateway
        from app.services.production_integrations import (
            configured_runtime_definition_factories,
            ensure_discovered_integrations_installed,
        )

        # Idempotent, and disabled on creation, so whichever process starts
        # first creates the record and the other finds it already present.
        ensure_discovered_integrations_installed(integration_gateway.runtime.integrations)
        factories = configured_runtime_definition_factories(integration_gateway.runtime.integrations)
    except Exception as exc:
        log_event(
            level="warning", category="runtime_worker", source="runtime_job_worker",
            message="Integration bootstrap is unavailable; rendering stays inactive.",
            details={"worker_id": worker_id, "instance_group": instance_group},
            exc=exc,
        )
        return outcome
    if not factories:
        # The normal state for a deployment that does not render.
        return outcome
    try:
        result = integration_gateway.bootstrap_runtime_definitions(factories)
    except Exception as exc:
        log_event(
            level="warning", category="runtime_worker", source="runtime_job_worker",
            message="Integration runtime definitions could not be registered in the worker.",
            details={"worker_id": worker_id, "instance_group": instance_group},
            exc=exc,
        )
        return outcome
    outcome["registered"] = list(result.get("registered", []))
    outcome["failed"] = list(result.get("failed", []))
    runtime = integration_gateway.runtime
    for integration_id in outcome["registered"]:
        try:
            if runtime.is_active(integration_id):
                continue
            record = runtime.integrations.get(integration_id)
            if not record.get("enabled"):
                # Installed but switched off, or not installed at all: the
                # definition stays registered so a later enable can activate it.
                continue
            runtime.activate(integration_id)
            outcome["activated"].append(integration_id)
        except Exception as exc:
            outcome["failed"].append(f"{integration_id}:{type(exc).__name__}")
    if outcome["failed"]:
        log_event(
            level="warning", category="runtime_worker", source="runtime_job_worker",
            message="Some worker integration definitions did not activate.",
            details={
                "worker_id": worker_id, "instance_group": instance_group,
                "failed": outcome["failed"][:20],
                "activated": outcome["activated"][:20],
            },
        )
    return outcome


def run(
    stop_event: threading.Event | None = None,
    ready_event: threading.Event | None = None,
    *,
    instance_group: str = "",
    parent_generation_id: str = "",
    child_generation_id: str = "",
) -> None:
    """Run until asked to stop; fatal infrastructure failures deliberately exit."""
    global _stopping
    settings = get_settings()
    if settings.runtime_worker_mode == "disabled" or not settings.runtime_worker_enabled:
        return
    if stop_event is None:
        _stopping = False
        signal.signal(signal.SIGINT, _stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _stop)
    if not instance_group:
        instance_group = os.getenv("LYNEWRIGHT_RUNTIME_WORKER_INSTANCE_GROUP", "").strip()
    worker_id = _worker_id()
    queue = DatabaseRuntimeJobQueue()
    integration_queue = IntegrationWorkerInvocationStore()
    contract_step_queue: ContractStepRunStore | None = None
    health_publisher = WorkerHealthPublisher(
        queue,
        worker_id=worker_id,
        instance_group=instance_group,
        heartbeat_seconds=settings.runtime_worker_heartbeat_seconds,
        parent_generation_id=parent_generation_id,
        child_generation_id=child_generation_id,
    )
    backoff = WorkerBackoff(
        settings.runtime_worker_error_backoff_initial_seconds,
        settings.runtime_worker_error_backoff_max_seconds,
    )
    job_backoffs: dict[int, WorkerBackoff] = {}

    # Startup retries only temporary infrastructure failures.  A schema or code
    # error must reach the embedded supervisor (or exit a separate process).
    while not _stopping and not (stop_event and stop_event.is_set()):
        try:
            init_db()
            cleanup_runtime_jobs()
            queue.recover()
            integration_queue.recover_expired()
            contract_step_queue = ContractStepRunStore()
            if settings.runtime_worker_health_coalescing_enabled:
                health_publisher.publish_transition(force=True)
            else:
                queue.mark_worker_healthy(
                    worker_id=worker_id, instance_group=instance_group,
                    parent_generation_id=parent_generation_id, child_generation_id=child_generation_id,
                )
        except Exception as exc:
            delay = _main_loop_delay_or_raise(
                worker_id=worker_id,
                instance_group=instance_group,
                operation="worker_startup",
                exc=exc,
                backoff=backoff,
            )
            _wait(stop_event, delay)
        else:
            backoff.reset()
            # After the database is proven usable and before any job is
            # claimed, so a render job never races provider registration.
            _bootstrap_worker_integrations(worker_id=worker_id, instance_group=instance_group)
            if ready_event is not None:
                ready_event.set()
            break
    else:
        return

    next_cleanup = time.monotonic() + 6 * 3600
    next_recovery = time.monotonic() + settings.runtime_recovery_interval_seconds
    next_review_delivery_sweep = time.monotonic()
    health_needs_recovery = False
    integration_thread: threading.Thread | None = None
    review_delivery_sweep_thread: threading.Thread | None = None
    prefer_contract_step = False
    while not _stopping and not (stop_event and stop_event.is_set()):
        now = time.monotonic()
        if now >= next_review_delivery_sweep and (
            review_delivery_sweep_thread is None or not review_delivery_sweep_thread.is_alive()
        ):
            review_delivery_sweep_thread = threading.Thread(
                target=_sweep_stale_review_deliveries_safely,
                args=(worker_id,), kwargs={"instance_group": instance_group},
                daemon=True, name="review-delivery-recovery",
            )
            review_delivery_sweep_thread.start()
            next_review_delivery_sweep = now + _REVIEW_DELIVERY_SWEEP_SECONDS
        if now >= next_cleanup:
            try:
                cleanup_runtime_jobs()
            except Exception as exc:
                # Cleanup failures are non-blocking only when genuinely transient.
                _main_loop_delay_or_raise(
                    worker_id=worker_id,
                    instance_group=instance_group,
                    operation="cleanup",
                    exc=exc,
                    backoff=backoff,
                )
                health_needs_recovery = True
            next_cleanup = now + 6 * 3600
        if now >= next_recovery:
            try:
                queue.recover()
                integration_queue.recover_expired()
                if settings.runtime_worker_health_coalescing_enabled:
                    health_publisher.publish_transition(force=True)
                else:
                    queue.mark_worker_healthy(
                        worker_id=worker_id, instance_group=instance_group,
                        parent_generation_id=parent_generation_id, child_generation_id=child_generation_id,
                    )
                backoff.reset()
                health_needs_recovery = False
            except Exception as exc:
                _main_loop_delay_or_raise(
                    worker_id=worker_id,
                    instance_group=instance_group,
                    operation="recovery",
                    exc=exc,
                    backoff=backoff,
                )
                health_needs_recovery = True
            next_recovery = now + settings.runtime_recovery_interval_seconds
        try:
            if settings.runtime_worker_health_coalescing_enabled:
                health_publisher.heartbeat_if_due()
            else:
                queue.heartbeat(
                    worker_id=worker_id, instance_group=instance_group,
                    parent_generation_id=parent_generation_id, child_generation_id=child_generation_id,
                )
        except Exception as exc:
            _main_loop_delay_or_raise(
                worker_id=worker_id,
                instance_group=instance_group,
                operation="heartbeat",
                exc=exc,
                backoff=backoff,
            )
            health_needs_recovery = True
        # Provider-neutral integration invocations run in this process so an
        # adapter can use deployment-owned sockets/networks without granting
        # those privileges to API or MCP containers. Keep one provider task in
        # flight per runtime worker across both queues, so concurrency stays
        # explicit rather than becoming an accidental consequence of polling.
        if integration_thread is None or not integration_thread.is_alive():
            claim_order = ("contract_step", "integration") if prefer_contract_step else ("integration", "contract_step")
            for kind in claim_order:
                if kind == "integration":
                    try:
                        invocation = integration_queue.claim_next(
                            worker_id=worker_id,
                            lease_seconds=settings.runtime_job_lease_seconds,
                        )
                    except Exception as exc:
                        _main_loop_delay_or_raise(
                            worker_id=worker_id,
                            instance_group=instance_group,
                            operation="integration_invocation_poll",
                            exc=exc,
                            backoff=backoff,
                        )
                        invocation = None
                    if invocation is None:
                        continue
                    integration_thread = threading.Thread(
                        target=_process_integration_invocation_safely,
                        args=(invocation, worker_id),
                        kwargs={"instance_group": instance_group},
                        daemon=True,
                        name="integration-worker-invocation",
                    )
                else:
                    try:
                        step_run = (contract_step_queue or ContractStepRunStore()).claim_next(
                            worker_id=worker_id,
                            lease_seconds=settings.runtime_job_lease_seconds,
                        )
                    except Exception as exc:
                        _main_loop_delay_or_raise(
                            worker_id=worker_id,
                            instance_group=instance_group,
                            operation="contract_step_poll",
                            exc=exc,
                            backoff=backoff,
                        )
                        step_run = None
                    if step_run is None:
                        continue
                    integration_thread = threading.Thread(
                        target=_process_contract_step_safely,
                        args=(step_run, worker_id, contract_step_queue),
                        kwargs={
                            "instance_group": instance_group,
                            "lease_seconds": settings.runtime_job_lease_seconds,
                        },
                        daemon=True,
                        name="contract-step-runner",
                    )
                integration_thread.start()
                prefer_contract_step = kind == "integration"
                break
        try:
            job = queue.claim_next(worker_id=worker_id, lease_seconds=settings.runtime_job_lease_seconds, job_types=tuple(_RUNTIME_JOB_HANDLERS))
        except Exception as exc:
            delay = _main_loop_delay_or_raise(
                worker_id=worker_id,
                instance_group=instance_group,
                operation="queue_poll",
                exc=exc,
                backoff=backoff,
            )
            _wait(stop_event, delay)
            health_needs_recovery = True
            continue
        backoff.reset()
        if health_needs_recovery:
            if settings.runtime_worker_health_coalescing_enabled:
                health_publisher.publish_transition(force=True)
            else:
                queue.mark_worker_healthy(
                    worker_id=worker_id, instance_group=instance_group,
                    parent_generation_id=parent_generation_id, child_generation_id=child_generation_id,
                )
            health_needs_recovery = False
        if job is None:
            _wait(stop_event, settings.runtime_worker_poll_seconds)
            continue
        # Job payload errors remain isolated in _process_safely.  Only its
        # infrastructure heartbeat/lease failures are allowed to escape.
        job_id = int(job["id"])
        job_backoff = job_backoffs.setdefault(
            job_id,
            WorkerBackoff(
                settings.runtime_worker_error_backoff_initial_seconds,
                settings.runtime_worker_error_backoff_max_seconds,
            ),
        )
        outcome = _process_safely(
            job, worker_id, instance_group=instance_group, backoff=job_backoff,
        )
        if outcome in {"completed", "failed"}:
            job_backoffs.pop(job_id, None)


def _process_integration_invocation_safely(
    invocation: dict[str, object], worker_id: str, *, instance_group: str = "",
) -> None:
    """Execute one claimed provider callback in the worker process.

    The contribution id and operation are persisted by the API, but the
    callable is resolved from this worker's process-local compiled registry.
    That prevents the queue from becoming a code-execution surface and keeps
    provider lifecycle/network privileges on the worker side.
    """
    request_id = str(invocation.get("id") or "")
    store = IntegrationWorkerInvocationStore()
    try:
        from app.services import integration_host

        context = context_from_dict(dict(invocation.get("_context") or {}))
        args = tuple(invocation.get("_args") or ())
        kwargs = dict(invocation.get("_kwargs") or {})
        contribution = integration_host.contributions.get(str(invocation.get("contribution_id") or ""))
        if str(contribution.metadata.get("invocation") or "direct") == "sdk_scoped":
            # The API-side worker bridge strips the non-serializable SDK and
            # carries only this safe actor descriptor. Rebuild a fresh,
            # request-scoped SDK in the worker so provider code sees the same
            # sdk, operation, payload shape as a direct invocation.
            from app.services.capability_dispatch import get_sdk_factory
            from app.services.platform_capabilities import CapabilityActor

            actor_value = kwargs.pop("_lynewright_sdk_actor", None)
            if not isinstance(actor_value, dict) or context is None:
                raise PlatformOperationError("Worker sdk-scoped invocation context is incomplete.")
            actor = CapabilityActor(
                client_id=str(actor_value.get("client_id") or ""),
                name=str(actor_value.get("name") or ""),
                client_type=str(actor_value.get("client_type") or ""),
                permission_mode=str(actor_value.get("permission_mode") or ""),
                granted_capabilities=frozenset(str(item) for item in (actor_value.get("granted_capabilities") or ())),
                enabled=bool(actor_value.get("enabled", True)),
                internal=bool(actor_value.get("internal", False)),
            )
            sdk = get_sdk_factory(context.tenant_id).create(
                caller=actor,
                integration_id=str(contribution.integration_id),
                connection_id=context.connection_id,
                project_id=context.project_id,
                invocation_context=context,
            )
            args = (sdk, *args)
        result = integration_host.contributions.invoke(
            str(invocation.get("contribution_id") or ""),
            *args,
            invocation_context=context,
            **kwargs,
        )
        store.complete(
            request_id, worker_id=worker_id, claim_token=str(invocation.get("claim_token") or ""),
            result=result,
        )
    except Exception as exc:
        try:
            store.fail(
                request_id, worker_id=worker_id,
                claim_token=str(invocation.get("claim_token") or ""),
                error=type(exc).__name__,
            )
        except Exception as finalization_exc:
            _log(
                worker_id,
                "integration_invocation_finalization",
                finalization_exc,
                0,
                error_class=WorkerErrorClass.TRANSIENT,
                record_health=False,
                instance_group=instance_group,
            )
        log_event(
            level="warning",
            category="runtime_worker",
            source="runtime_job_worker",
            message="integration_invocation_failed",
            details={
                "worker_id": worker_id,
                "request_id": request_id,
                "contribution_id": str(invocation.get("contribution_id") or ""),
                "error": type(exc).__name__,
                "instance_group": instance_group,
            },
            exc=exc,
        )


def _process_contract_step_safely(
    run: dict[str, Any], worker_id: str, store: ContractStepRunStore | None = None,
    *, instance_group: str = "", lease_seconds: int = 120,
) -> None:
    try:
        process_claimed_step(run, worker_id, store=store, lease_seconds=lease_seconds)
    except Exception as exc:
        _log(
            worker_id,
            "contract_step_processing",
            exc,
            0,
            error_class=WorkerErrorClass.JOB_LOCAL,
            record_health=False,
            instance_group=instance_group,
        )


def _process_safely(
    job: dict[str, object], worker_id: str, *, instance_group: str = "",
    backoff: WorkerBackoff | None = None,
) -> str:
    claim_version = int(job["claim_version"])
    renewal_stop = threading.Event()
    ownership_lost = threading.Event()
    renewal_fatal_errors: list[Exception] = []
    queue = DatabaseRuntimeJobQueue()
    job_backoff = backoff or WorkerBackoff(
        get_settings().runtime_worker_error_backoff_initial_seconds,
        get_settings().runtime_worker_error_backoff_max_seconds,
    )
    outcome = "inflight"
    _publish_claimed_job_heartbeat_best_effort(
        queue=queue, worker_id=worker_id, job_id=int(job["id"]), instance_group=instance_group,
    )
    renewal = threading.Thread(
        target=_renew,
        args=(queue, int(job["id"]), worker_id, claim_version, renewal_stop,
              ownership_lost, renewal_fatal_errors, instance_group),
        daemon=True,
    )
    renewal.start()
    try:
        _process(job, worker_id, claim_version, ownership_lost)
        if not ownership_lost.is_set():
            with closing(get_connection()) as db, db:
                completed = complete_claimed_job_in_connection(
                    db, job_id=int(job["id"]), worker_id=worker_id, claim_version=claim_version,
                )
            if completed:
                job_backoff.reset()
                outcome = "completed"
    except Exception as exc:
        error_class = classify_worker_error(exc, operation="job_processing")
        retry_at = None
        delay = 0.0
        attempts = int(job.get("attempts") or 0)
        max_attempts = max(1, int(job.get("max_attempts") or 1))
        if error_class is WorkerErrorClass.TRANSIENT and attempts < max_attempts:
            delay = job_backoff.next_delay()
            retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            queue.fail(
                job_id=int(job["id"]), worker_id=worker_id, claim_version=claim_version,
                error=f"{type(exc).__name__}:{str(exc)}"[:1000], retry_at=retry_at,
            )
        except Exception as fail_exc:
            _log(worker_id, "job_failure_handling", fail_exc, 0,
                 error_class=WorkerErrorClass.JOB_LOCAL, record_health=False,
                 instance_group=instance_group)
        _log(worker_id, "job_processing", exc, delay,
             critical=error_class is WorkerErrorClass.FATAL,
             error_class=error_class, record_health=False, instance_group=instance_group)
        outcome = "retrying" if retry_at else "failed"
        if error_class is WorkerErrorClass.FATAL:
            raise
    finally:
        renewal_stop.set()
        renewal.join(timeout=1)
    if renewal_fatal_errors:
        raise renewal_fatal_errors[0]
    _clear_current_job_best_effort(queue=queue, worker_id=worker_id, instance_group=instance_group)
    return outcome


def _publish_claimed_job_heartbeat_best_effort(
    *,
    queue: DatabaseRuntimeJobQueue,
    worker_id: str,
    job_id: int,
    instance_group: str,
) -> None:
    try:
        queue.heartbeat(
            worker_id=worker_id,
            current_job_id=job_id,
            instance_group=instance_group,
        )
    except Exception as exc:
        error_class = classify_worker_error(exc, operation="claimed_job_heartbeat")
        _log(
            worker_id,
            "claimed_job_heartbeat",
            exc,
            0,
            critical=error_class is WorkerErrorClass.FATAL,
            error_class=error_class,
            instance_group=instance_group,
        )
        if error_class is WorkerErrorClass.FATAL:
            # Do not start product verification. The fenced claim remains
            # recoverable by its lease and the main loop reaches supervision.
            raise
        _log(
            worker_id,
            "claimed_job_continued_after_metadata_failure",
            exc,
            0,
            error_class=error_class,
            record_health=False,
            instance_group=instance_group,
        )


def _clear_current_job_best_effort(
    *,
    queue: DatabaseRuntimeJobQueue,
    worker_id: str,
    instance_group: str,
) -> None:
    try:
        queue.heartbeat(worker_id=worker_id, instance_group=instance_group)
    except Exception as exc:
        error_class = classify_worker_error(exc, operation="post_job_heartbeat")
        _log(
            worker_id,
            "post_job_heartbeat",
            exc,
            0,
            critical=error_class is WorkerErrorClass.FATAL,
            error_class=error_class,
            instance_group=instance_group,
        )
        # Completion/final fencing has already happened. This is diagnostic
        # metadata only, so even a fatal persistence classification must not
        # change the completed claim or bring down otherwise usable capacity.
        return


def _release_job_after_local_error(
    job: dict[str, object],
    worker_id: str,
    claim_version: int,
    exc: Exception,
    *,
    instance_group: str,
) -> None:
    try:
        _release_after_worker_error(job, worker_id, claim_version, f"worker_error:{type(exc).__name__}")
    except Exception as release_exc:
        _log(
            worker_id,
            "job_failure_handling",
            release_exc,
            0,
            error_class=WorkerErrorClass.JOB_LOCAL,
            record_health=False,
            instance_group=instance_group,
        )


def _process(
    job: dict[str, object], worker_id: str, claim_version: int,
    ownership_lost: threading.Event,
) -> Any:
    job_type = str(job.get("job_type") or "")
    handler = _RUNTIME_JOB_HANDLERS.get(job_type)
    if handler is None:
        raise LookupError(f"No runtime job handler is registered for {job_type!r}.")
    return handler(job, worker_id, claim_version, ownership_lost)


def _renew(
    queue: DatabaseRuntimeJobQueue,
    job_id: int,
    worker_id: str,
    claim_version: int,
    stop: threading.Event,
    ownership_lost: threading.Event,
    fatal_errors: list[Exception],
    instance_group: str,
) -> None:
    settings = get_settings()
    interval = max(
        1.0,
        min(
            float(settings.runtime_worker_heartbeat_seconds),
            float(settings.runtime_job_lease_seconds) / 3,
            45.0,
        ),
    )
    while not stop.wait(interval):
        try:
            queue.heartbeat(
                worker_id=worker_id,
                current_job_id=job_id,
                instance_group=instance_group,
            )
        except Exception as exc:
            error_class = classify_worker_error(exc, operation="lease_renewal_heartbeat")
            if error_class is WorkerErrorClass.FATAL:
                _log(
                    worker_id,
                    "lease_renewal_heartbeat",
                    exc,
                    0,
                    critical=True,
                    error_class=error_class,
                    instance_group=instance_group,
                )
                fatal_errors.append(exc)
                ownership_lost.set()
                return
            _log(
                worker_id,
                "lease_renewal_heartbeat",
                exc,
                0,
                error_class=error_class,
                instance_group=instance_group,
            )
        try:
            if not queue.renew_lease(
                job_id=job_id,
                worker_id=worker_id,
                claim_version=claim_version,
                lease_seconds=settings.runtime_job_lease_seconds,
            ):
                ownership_lost.set()
                return
        except Exception as exc:
            error_class = classify_worker_error(exc, operation="lease_renewal")
            if error_class is WorkerErrorClass.FATAL:
                _log(
                    worker_id,
                    "lease_renewal",
                    exc,
                    0,
                    critical=True,
                    error_class=error_class,
                    instance_group=instance_group,
                )
                fatal_errors.append(exc)
                ownership_lost.set()
                return
            # Do not declare ownership lost for a transient renewal DB error;
            # final fencing remains authoritative and the next renewal can win.
            _log(
                worker_id,
                "lease_renewal",
                exc,
                0,
                error_class=error_class,
                instance_group=instance_group,
            )


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        if isinstance(exc, RuntimeError) and "LYNEWRIGHT_" in str(exc):
            raise SystemExit(22) from exc
        if isinstance(exc, sqlite3.Error) or "schema" in str(exc).lower():
            raise SystemExit(23) from exc
        raise
