"""Generation-scoped, reversible runtime effects for integrations."""
from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from collections.abc import Callable, Iterator
from typing import Literal

from app.services.platform_services import PlatformOperationError


ActivationScopeState = Literal[
    "REGISTERED", "ACTIVATING", "READY", "SUSPENDED",
    "DISPOSING", "DISPOSED", "FAILED",
]


class IntegrationActivationScope:
    """Owns one activation generation and all of its process-local effects."""

    def __init__(self, integration_id: str, *, generation_id: str | None = None) -> None:
        self.integration_id = integration_id
        self.generation_id = generation_id or uuid.uuid4().hex
        self._state: ActivationScopeState = "REGISTERED"
        self._accepting = True
        self._in_flight = 0
        self._disposers: list[Callable[[], object]] = []
        self._lock = threading.RLock()
        self._drained = threading.Condition(self._lock)
        self._dispose_errors: list[str] = []

    @property
    def state(self) -> ActivationScopeState:
        with self._lock:
            return self._state

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting and self._state not in {"DISPOSING", "DISPOSED", "FAILED"}

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def dispose_errors(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._dispose_errors)

    def set_state(self, state: ActivationScopeState) -> None:
        with self._lock:
            if self._state in {"DISPOSED", "FAILED"} and state not in {"DISPOSED", "FAILED"}:
                raise PlatformOperationError("A disposed activation scope cannot be reactivated.")
            self._state = state

    def ensure_accepting(self) -> None:
        if not self.accepting:
            raise PlatformOperationError("Integration activation scope is no longer accepting work.")

    def register_disposer(self, disposer: Callable[[], object]) -> None:
        if not callable(disposer):
            raise PlatformOperationError("Activation scope disposer must be callable.")
        with self._lock:
            self.ensure_accepting()
            self._disposers.append(disposer)

    @contextmanager
    def admit(self) -> Iterator[None]:
        with self._lock:
            self.ensure_accepting()
            self._in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1
                if self._in_flight <= 0:
                    self._drained.notify_all()

    def stop_accepting(self) -> None:
        with self._lock:
            if self._state not in {"DISPOSED", "FAILED"}:
                self._accepting = False
                if self._state not in {"DISPOSING", "SUSPENDED"}:
                    self._state = "DISPOSING"

    def drain(self, *, timeout_seconds: float = 5.0) -> bool:
        timeout = max(0.0, float(timeout_seconds))
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._drained.wait(remaining)
            return True

    def dispose(self, *, timeout_seconds: float = 5.0) -> tuple[str, ...]:
        """Stop admission, drain bounded work, and dispose in reverse order."""
        self.stop_accepting()
        self.drain(timeout_seconds=timeout_seconds)
        with self._lock:
            if self._state == "DISPOSED":
                return tuple(self._dispose_errors)
            self._state = "DISPOSING"
            disposers = list(reversed(self._disposers))
            self._disposers.clear()
        for disposer in disposers:
            try:
                disposer()
            except Exception as exc:  # cleanup must continue after one failure
                with self._lock:
                    self._dispose_errors.append(type(exc).__name__)
        with self._lock:
            self._state = "DISPOSED" if not self._dispose_errors else "FAILED"
            return tuple(self._dispose_errors)


__all__ = ["ActivationScopeState", "IntegrationActivationScope"]
