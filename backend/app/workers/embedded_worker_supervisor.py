from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import get_settings
from app.services.application_logging import log_event
from app.services.job_queue import DatabaseRuntimeJobQueue
from app.workers.runtime_job_worker import run
from app.workers.worker_backoff import WorkerBackoff
from app.workers.worker_errors import WorkerErrorClass, classify_worker_error

_STATUS_RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)
_watcher_lock = asyncio.Lock()


@dataclass(frozen=True)
class StatusPublishResult:
    persisted: bool
    transient_failure: bool
    fatal_failure: bool
    error_message: str = ""


@dataclass
class SupervisorRuntimeState:
    status: str = "offline"
    restart_count_last_hour: int = 0
    restart_limit_exceeded: bool = False
    current_backoff_seconds: float = 0
    last_error_operation: str = ""
    last_error_message: str = ""
    last_transition_monotonic: float = field(default_factory=time.monotonic)
    instance_group: str = ""
    generation_id: str = ""
    generation_started_at: str = ""
    supervisor_id: str = ""
    task_status: str = "running"
    child_generation_id: str = ""
    child_sequence: int = 0
    replacement_blocked: bool = False
    orphan_worker_possible: bool = False
    failure_reason: str = ""


class SupervisorStatusPublishFatal(RuntimeError):
    pass


def _instance_group() -> str:
    """Keep an embedded supervisor and its child worker in one deployment group."""
    configured = os.getenv("LYNEWRIGHT_RUNTIME_WORKER_INSTANCE_GROUP", "").strip()
    return configured or socket.gethostname()


def _generation_started_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_supervisor_runtime_state() -> SupervisorRuntimeState:
    instance_group = _instance_group()
    generation_id = uuid.uuid4().hex
    return SupervisorRuntimeState(
        instance_group=instance_group,
        generation_id=generation_id,
        generation_started_at=_generation_started_at(),
        supervisor_id=f"embedded-supervisor:{instance_group}:{generation_id}",
    )


def worker_run_was_stable(
    *,
    ready_at: float | None,
    ended_at: float,
    stable_period_seconds: int,
) -> bool:
    """A merely running thread is not healthy until the worker signalled ready."""
    return ready_at is not None and ended_at - ready_at >= stable_period_seconds


def _safe_log(*, level: str, message: str, details: dict[str, object]) -> None:
    try:
        log_event(
            level=level,
            category="runtime_worker",
            source="embedded_worker_supervisor",
            message=message,
            details=details,
        )
    except Exception:
        # Observability must never become supervision authority.
        return


async def safe_publish_supervisor_status(
    *,
    queue: DatabaseRuntimeJobQueue,
    state: SupervisorRuntimeState,
) -> StatusPublishResult:
    """Persist supervisor metadata without letting transient DB failures stop it."""
    try:
        await asyncio.to_thread(
            queue.set_worker_status,
            worker_id=state.supervisor_id,
            worker_kind="supervisor",
            instance_group=state.instance_group,
            generation_id=state.generation_id,
            generation_started_at=state.generation_started_at,
            supervisor_task_status=state.task_status,
            parent_generation_id=state.generation_id,
            child_generation_id=state.child_generation_id,
            replacement_blocked=state.replacement_blocked,
            orphan_worker_possible=state.orphan_worker_possible,
            failure_reason=state.failure_reason,
            status=state.status,
            current_backoff_seconds=state.current_backoff_seconds,
            restart_count=state.restart_count_last_hour,
            restart_limit_exceeded=state.restart_limit_exceeded,
            error_operation=state.last_error_operation,
            error_message=state.last_error_message,
        )
    except Exception as exc:
        error_class = classify_worker_error(exc, operation="supervisor_status_publish")
        fatal = error_class is WorkerErrorClass.FATAL
        _safe_log(
            level="critical" if fatal else "warning",
            message="supervisor_status_persist_failed",
            details={
                "generation_id": state.generation_id,
                "instance_group": state.instance_group,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "error_class": error_class.value,
            },
        )
        return StatusPublishResult(
            persisted=False,
            transient_failure=not fatal,
            fatal_failure=fatal,
            error_message=str(exc)[:500],
        )
    return StatusPublishResult(persisted=True, transient_failure=False, fatal_failure=False)


class SupervisorStatusPublisher:
    """Coalesces status writes and retries them independently of worker backoff."""

    def __init__(self, queue: DatabaseRuntimeJobQueue, heartbeat_seconds: int) -> None:
        self.queue = queue
        self.heartbeat_seconds = max(1.0, float(heartbeat_seconds))
        self.last_persisted_signature: tuple[object, ...] | None = None
        self.last_persisted_monotonic = 0.0
        self.pending_signature: tuple[object, ...] | None = None
        self.next_retry_monotonic = 0.0
        self.retry_index = 0

    @staticmethod
    def _signature(state: SupervisorRuntimeState) -> tuple[object, ...]:
        return (
            state.status,
            state.restart_count_last_hour,
            state.restart_limit_exceeded,
            state.current_backoff_seconds,
            state.last_error_operation,
            state.last_error_message,
            state.task_status,
        )

    async def publish(self, state: SupervisorRuntimeState, *, force: bool = False) -> StatusPublishResult:
        now = time.monotonic()
        signature = self._signature(state)
        if not force:
            if self.pending_signature == signature and now < self.next_retry_monotonic:
                return StatusPublishResult(False, False, False)
            changed = signature != self.last_persisted_signature
            heartbeat_due = now - self.last_persisted_monotonic >= self.heartbeat_seconds
            if self.pending_signature is None and not changed and not heartbeat_due:
                return StatusPublishResult(False, False, False)

        result = await safe_publish_supervisor_status(queue=self.queue, state=state)
        if result.persisted:
            recovered = self.pending_signature is not None
            self.last_persisted_signature = signature
            self.last_persisted_monotonic = now
            self.pending_signature = None
            self.next_retry_monotonic = 0.0
            self.retry_index = 0
            if recovered:
                _safe_log(
                    level="info",
                    message="supervisor_status_persist_recovered",
                    details={"generation_id": state.generation_id, "instance_group": state.instance_group},
                )
            return result
        if result.fatal_failure:
            raise SupervisorStatusPublishFatal(result.error_message or "fatal supervisor status persistence failure")

        delay = _STATUS_RETRY_DELAYS[min(self.retry_index, len(_STATUS_RETRY_DELAYS) - 1)]
        self.retry_index += 1
        self.pending_signature = signature
        self.next_retry_monotonic = now + delay
        return result

    def next_retry_delay(self) -> float:
        if self.pending_signature is None:
            return 0.0
        return max(0.0, self.next_retry_monotonic - time.monotonic())


def _transition(
    state: SupervisorRuntimeState,
    *,
    status: str | None = None,
    task_status: str | None = None,
    restart_count: int | None = None,
    restart_limit_exceeded: bool | None = None,
    current_backoff_seconds: float | None = None,
    error_operation: str | None = None,
    error_message: str | None = None,
    replacement_blocked: bool | None = None,
    orphan_worker_possible: bool | None = None,
    failure_reason: str | None = None,
) -> None:
    if status is not None:
        state.status = status
    if task_status is not None:
        state.task_status = task_status
    if restart_count is not None:
        state.restart_count_last_hour = restart_count
    if restart_limit_exceeded is not None:
        state.restart_limit_exceeded = restart_limit_exceeded
    if current_backoff_seconds is not None:
        state.current_backoff_seconds = max(0.0, current_backoff_seconds)
    if error_operation is not None:
        state.last_error_operation = error_operation[:80]
    if error_message is not None:
        state.last_error_message = error_message[:500]
    if replacement_blocked is not None:
        state.replacement_blocked = replacement_blocked
    if orphan_worker_possible is not None:
        state.orphan_worker_possible = orphan_worker_possible
    if failure_reason is not None:
        state.failure_reason = failure_reason[:120]
    state.last_transition_monotonic = time.monotonic()


async def _stop_owned_child(
    *,
    state: SupervisorRuntimeState,
    child_stop_event: threading.Event,
    worker_task: asyncio.Task[None],
    timeout_seconds: int,
) -> bool:
    """Stop the only child owned by this generation before replacement is allowed."""
    child_stop_event.set()
    _safe_log(
        level="info",
        message="child_worker_stop_requested",
        details={"generation_id": state.generation_id, "child_generation_id": state.child_generation_id},
    )
    if worker_task.done():
        return True
    try:
        await asyncio.wait_for(asyncio.shield(worker_task), timeout=max(1, timeout_seconds))
    except asyncio.TimeoutError:
        _transition(
            state,
            status="failed",
            task_status="failed",
            restart_limit_exceeded=True,
            current_backoff_seconds=0,
            error_operation="child_worker_shutdown",
            error_message="child worker did not stop before the shutdown timeout",
            replacement_blocked=True,
            orphan_worker_possible=True,
            failure_reason="child_worker_shutdown_unconfirmed",
        )
        await safe_publish_supervisor_status(queue=DatabaseRuntimeJobQueue(), state=state)
        _safe_log(
            level="critical",
            message="child_worker_shutdown_timeout",
            details={"generation_id": state.generation_id, "child_generation_id": state.child_generation_id},
        )
        return False
    except Exception as exc:
        # The task exited, even if it did so with an error. The caller records
        # that error through normal restart handling; ownership is confirmed.
        _safe_log(
            level="warning",
            message="child_worker_shutdown_error",
            details={"generation_id": state.generation_id, "error_type": type(exc).__name__},
        )
    _safe_log(
        level="info",
        message="child_worker_stopped",
        details={"generation_id": state.generation_id, "child_generation_id": state.child_generation_id},
    )
    return True


async def supervise_embedded_worker(
    stop_event: threading.Event,
    *,
    runtime_state: SupervisorRuntimeState | None = None,
) -> SupervisorRuntimeState:
    """Supervise one worker generation without ever orphaning its child."""
    settings = get_settings()
    state = runtime_state or new_supervisor_runtime_state()
    queue = DatabaseRuntimeJobQueue()
    publisher = SupervisorStatusPublisher(queue, settings.runtime_worker_heartbeat_seconds)
    restart_backoff = WorkerBackoff(
        settings.runtime_worker_restart_backoff_initial_seconds,
        settings.runtime_worker_restart_backoff_max_seconds,
    )
    restarts: deque[float] = deque()
    await publisher.publish(state, force=True)
    _safe_log(level="info", message="supervisor_generation_started", details={"generation_id": state.generation_id, "instance_group": state.instance_group})

    while not stop_event.is_set():
        ready_event = threading.Event()
        child_stop_event = threading.Event()
        state.child_sequence += 1
        state.child_generation_id = f"{state.generation_id}:child:{state.child_sequence}"
        worker_task = asyncio.create_task(
            asyncio.to_thread(
                run,
                child_stop_event,
                ready_event,
                instance_group=state.instance_group,
                parent_generation_id=state.generation_id,
                child_generation_id=state.child_generation_id,
            )
        )
        _safe_log(level="info", message="child_worker_started", details={"generation_id": state.generation_id, "child_generation_id": state.child_generation_id})
        ready_at: float | None = None
        reason = "unexpected_clean_exit"
        try:
            while not worker_task.done() and not stop_event.is_set():
                now = time.monotonic()
                while restarts and now - restarts[0] > 3600:
                    restarts.popleft()
                if ready_at is None and ready_event.is_set():
                    ready_at = now
                    _transition(state, status="online", task_status="running", restart_count=len(restarts), current_backoff_seconds=0)
                await publisher.publish(state)
                await asyncio.sleep(min(1.0, max(0.25, settings.runtime_worker_heartbeat_seconds / 2)))
            if worker_task.done():
                try:
                    await worker_task
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            child_stopped = await _stop_owned_child(
                state=state,
                child_stop_event=child_stop_event,
                worker_task=worker_task,
                timeout_seconds=settings.runtime_worker_shutdown_timeout_seconds,
            )

        if not child_stopped:
            return state
        ended_at = time.monotonic()
        if stop_event.is_set():
            _transition(state, status="offline", task_status="stopped", current_backoff_seconds=0)
            await publisher.publish(state, force=True)
            return state
        if worker_run_was_stable(ready_at=ready_at, ended_at=ended_at, stable_period_seconds=settings.runtime_worker_stable_period_seconds):
            restart_backoff.reset()
        while restarts and ended_at - restarts[0] > 3600:
            restarts.popleft()
        restarts.append(ended_at)
        if len(restarts) > settings.runtime_worker_max_restarts_per_hour:
            _transition(state, status="failed", task_status="failed", restart_count=len(restarts), restart_limit_exceeded=True, current_backoff_seconds=0, error_operation="worker_restart", error_message=reason)
            await publisher.publish(state, force=True)
            _safe_log(level="critical", message="supervisor_terminal_failure", details={"generation_id": state.generation_id, "instance_group": state.instance_group, "restart_count_last_hour": len(restarts), "reason": reason})
            return state
        delay = restart_backoff.next_delay()
        _transition(state, status="restarting", task_status="running", restart_count=len(restarts), current_backoff_seconds=delay, error_operation="worker_restart", error_message=reason)
        await publisher.publish(state, force=True)
        _safe_log(level="error", message="Embedded worker stopped unexpectedly; restarting.", details={"generation_id": state.generation_id, "reason": reason, "restart_count_last_hour": len(restarts), "retry_delay_seconds": delay})
        await asyncio.to_thread(stop_event.wait, delay)
    return state


async def _wait_for_terminal_reset(
    *,
    queue: DatabaseRuntimeJobQueue,
    state: SupervisorRuntimeState,
    stop_event: threading.Event,
    heartbeat_seconds: int,
) -> bool:
    """Keep a terminal generation visible until an explicit reset or shutdown."""
    publisher = SupervisorStatusPublisher(queue, heartbeat_seconds)
    while not stop_event.is_set():
        try:
            await publisher.publish(state)
        except SupervisorStatusPublishFatal:
            # The failure is already safely logged by the publisher. Keep the
            # terminal generation in memory and let shutdown/admin recovery
            # decide the next action instead of creating a hot restart loop.
            return False
        try:
            acknowledged = await asyncio.to_thread(
                queue.is_supervisor_generation_acknowledged,
                supervisor_id=state.supervisor_id,
                generation_id=state.generation_id,
            )
        except Exception as exc:
            _safe_log(
                level="warning",
                message="supervisor_terminal_reset_check_failed",
                details={"generation_id": state.generation_id, "error_type": type(exc).__name__},
            )
            acknowledged = False
        if acknowledged:
            return True
        retry_delay = publisher.next_retry_delay()
        delay = retry_delay if retry_delay else min(max(1, heartbeat_seconds), 5)
        await asyncio.to_thread(stop_event.wait, min(max(0.1, delay), 5))
    return False


async def watch_embedded_supervisor(stop_event: threading.Event) -> None:
    """Lifespan-level guard that restarts unexpectedly exited supervisors safely."""
    settings = get_settings()
    if not settings.runtime_supervisor_task_watcher_enabled:
        await supervise_embedded_worker(stop_event)
        return

    async with _watcher_lock:
        restarts: deque[float] = deque()
        restart_backoff = WorkerBackoff(1, 30)
        while not stop_event.is_set():
            state = new_supervisor_runtime_state()
            unexpected_reason = ""
            try:
                await supervise_embedded_worker(stop_event, runtime_state=state)
            except Exception as exc:
                unexpected_reason = f"{type(exc).__name__}: {str(exc)[:300]}"
                _transition(
                    state,
                    status="failed",
                    task_status="failed",
                    current_backoff_seconds=0,
                    error_operation="supervisor_task",
                    error_message=unexpected_reason,
                )
                await safe_publish_supervisor_status(queue=DatabaseRuntimeJobQueue(), state=state)
                _safe_log(
                    level="critical",
                    message="supervisor_task_exited",
                    details={"generation_id": state.generation_id, "reason": unexpected_reason},
                )

            if stop_event.is_set():
                return
            if state.restart_limit_exceeded:
                if not await _wait_for_terminal_reset(
                    queue=DatabaseRuntimeJobQueue(),
                    state=state,
                    stop_event=stop_event,
                    heartbeat_seconds=settings.runtime_worker_heartbeat_seconds,
                ):
                    return
                restart_backoff.reset()
                continue

            now = time.monotonic()
            while restarts and now - restarts[0] > 3600:
                restarts.popleft()
            restarts.append(now)
            if len(restarts) > settings.runtime_supervisor_max_restarts_per_hour:
                _transition(
                    state,
                    status="failed",
                    task_status="failed",
                    restart_count=len(restarts),
                    restart_limit_exceeded=True,
                    current_backoff_seconds=0,
                    error_operation="supervisor_task",
                    error_message=unexpected_reason or "unexpected supervisor exit",
                )
                await safe_publish_supervisor_status(queue=DatabaseRuntimeJobQueue(), state=state)
                _safe_log(
                    level="critical",
                    message="supervisor_terminal_failure",
                    details={"generation_id": state.generation_id, "restart_count_last_hour": len(restarts)},
                )
                if not await _wait_for_terminal_reset(
                    queue=DatabaseRuntimeJobQueue(),
                    state=state,
                    stop_event=stop_event,
                    heartbeat_seconds=settings.runtime_worker_heartbeat_seconds,
                ):
                    return
                restart_backoff.reset()
                continue

            delay = restart_backoff.next_delay()
            _transition(
                state,
                status="restarting",
                task_status="restarting",
                restart_count=len(restarts),
                current_backoff_seconds=delay,
                error_operation="supervisor_task",
                error_message=unexpected_reason or "unexpected supervisor exit",
            )
            await safe_publish_supervisor_status(queue=DatabaseRuntimeJobQueue(), state=state)
            _safe_log(
                level="error",
                message="supervisor_task_restarted",
                details={"generation_id": state.generation_id, "retry_delay_seconds": delay},
            )
            await asyncio.to_thread(stop_event.wait, delay)
