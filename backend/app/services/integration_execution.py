"""Bounded, cooperative execution for trusted integration callbacks."""
from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from app.services.platform_services import PlatformOperationError, PlatformValidationError


_T = TypeVar("_T")
_current_context: contextvars.ContextVar["IntegrationExecutionContext | None"] = contextvars.ContextVar(
    "integration_execution_context", default=None,
)


@dataclass(frozen=True, slots=True)
class IntegrationExecutionContext:
    operation: str
    deadline_monotonic: float
    _cancelled: threading.Event

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())


class IntegrationExecutionTimeout(PlatformOperationError):
    """The host stopped waiting for a cooperative integration callback."""


def current_execution_context() -> IntegrationExecutionContext | None:
    """Return the current callback budget for adapters that support cancellation."""
    return _current_context.get()


class IntegrationExecutionRunner:
    """Runs trusted callback code without letting it indefinitely occupy a host worker."""

    def __init__(self, *, default_timeout_seconds: float = 30.0) -> None:
        self.default_timeout_seconds = self._timeout(default_timeout_seconds)

    def run(
        self,
        operation: str,
        callback: Callable[..., _T],
        *args: Any,
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> _T:
        timeout = self._timeout(self.default_timeout_seconds if timeout_seconds is None else timeout_seconds)
        cancelled, done = threading.Event(), threading.Event()
        deadline = time.monotonic() + timeout
        context = IntegrationExecutionContext(operation=operation, deadline_monotonic=deadline, _cancelled=cancelled)
        result: list[_T] = []
        errors: list[BaseException] = []
        caller_context = contextvars.copy_context()

        def invoke() -> None:
            token = _current_context.set(context)
            try:
                result.append(callback(*args, **kwargs))
            except BaseException as exc:
                errors.append(exc)
            finally:
                _current_context.reset(token)
                done.set()

        # A daemon worker makes the timeout a real host-worker boundary. Python
        # cannot safely kill arbitrary adapter code; adapters cooperate through
        # current_execution_context().cancelled and must release their own I/O.
        thread = threading.Thread(target=lambda: caller_context.run(invoke), daemon=True, name=f"integration-{operation[:32]}")
        thread.start()
        if not done.wait(timeout):
            cancelled.set()
            raise IntegrationExecutionTimeout(f"Integration {operation} exceeded its {timeout:g}-second execution budget.")
        if errors:
            raise errors[0]
        return result[0]

    @staticmethod
    def _timeout(value: float) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("Integration execution timeout must be a number.") from exc
        if not 0.05 <= timeout <= 300:
            raise PlatformValidationError("Integration execution timeout must be between 0.05 and 300 seconds.")
        return timeout
