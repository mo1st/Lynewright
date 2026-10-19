"""Core platform event bus with durable events and isolated subscribers."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.services.platform_services import EventService, PlatformValidationError, _redact


EVENT_NAMESPACES = frozenset({"system", "project", "job", "media", "ai", "credential", "mcp"})
EventSubscriber = Callable[[dict[str, Any]], None]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EventSubscription:
    pattern: str
    callback: EventSubscriber


class CoreEventBus:
    """Persist first, then notify; one bad subscriber cannot break dispatch."""

    def __init__(self, events: EventService) -> None:
        self.events = events
        self._subscriptions: list[EventSubscription] = []

    def subscribe(self, pattern: str, callback: EventSubscriber) -> Callable[[], None]:
        self._validate_pattern(pattern)
        subscription = EventSubscription(pattern=pattern, callback=callback)
        self._subscriptions.append(subscription)

        def unsubscribe() -> None:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)

        return unsubscribe

    def publish(self, value: dict[str, Any]) -> dict[str, Any]:
        event_type = str(value.get("type") or "")
        self._validate_event_type(event_type)
        event = self.events.publish({
            "type": event_type,
            "payload": _redact(value.get("payload") if isinstance(value.get("payload"), dict) else {}),
            "project_id": value.get("project_id"),
        })
        for subscription in tuple(self._subscriptions):
            if not self._matches(subscription.pattern, event_type):
                continue
            try:
                subscription.callback(event)
            except Exception:
                logger.exception("Platform event subscriber failed", extra={"event_type": event_type})
        return event

    @staticmethod
    def _validate_event_type(event_type: str) -> None:
        namespace, separator, action = event_type.partition(".")
        if not separator or namespace not in EVENT_NAMESPACES or not action:
            raise PlatformValidationError("Event type must use an approved namespace, such as job.completed.")

    @staticmethod
    def _validate_pattern(pattern: str) -> None:
        if pattern == "*":
            return
        namespace = pattern[:-2] if pattern.endswith(".*") else pattern.split(".", 1)[0]
        if namespace not in EVENT_NAMESPACES:
            raise PlatformValidationError("Event subscription uses an unknown namespace.")

    @staticmethod
    def _matches(pattern: str, event_type: str) -> bool:
        return pattern == "*" or pattern == event_type or (pattern.endswith(".*") and event_type.startswith(pattern[:-1]))
