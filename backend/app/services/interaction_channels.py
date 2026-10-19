"""Provider-neutral channel abstraction for the interaction gateway.

A *channel* is a human communication surface (Telegram today, others later).
Each channel advertises the capabilities it supports; nothing in Lynewright may
assume a channel can do everything. Adapters translate the neutral contract to
a provider API and never leak provider-specific fields into core services.

This module deliberately knows nothing about any concrete provider: the
default registry wires providers in lazily so importing this module stays
cheap and dependency-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable


class ChannelCapability(str, Enum):
    """Capabilities a channel may advertise. Absence means "not supported"."""

    MESSAGE_RECEIVE = "message.receive"
    MESSAGE_SEND = "message.send"
    MESSAGE_EDIT = "message.edit"
    FILE_RECEIVE = "file.receive"
    FILE_SEND = "file.send"
    NOTIFICATION_SEND = "notification.send"
    APPROVAL_REQUEST = "approval.request"
    APPROVAL_RESOLVE = "approval.resolve"


# Shared operator run-state vocabulary (master plan §Shared Operator Run
# States). Session F only persists/validates it on conversation run bindings;
# the state machine itself belongs to future sessions.
OPERATOR_RUN_STATES = frozenset({
    "queued",
    "running",
    "waiting_tool",
    "waiting_job",
    "waiting_approval",
    "sleeping",
    "completed",
    "failed",
    "cancelled",
})


class ChannelError(RuntimeError):
    """Raised when a channel operation cannot be completed."""


@dataclass(frozen=True)
class ChannelDescriptor:
    channel_type: str
    display_name: str
    capabilities: frozenset[ChannelCapability]

    def supports(self, capability: ChannelCapability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class OutboundMessage:
    """A provider-neutral outbound message.

    ``buttons`` are (label, callback_data) pairs or (label, action-object)
    values rendered where the channel supports interactive actions; ``media_refs``
    reference durable Lynewright media rows (never raw blobs).
    """

    text: str = ""
    media_refs: tuple[Mapping[str, object], ...] = ()
    buttons: tuple[tuple[str, Any], ...] = ()
    parse_mode: str | None = None
    edit_external_message_id: str | None = None
    silent: bool = False


@dataclass(frozen=True)
class SendResult:
    ok: bool
    external_message_id: str = ""
    detail: str = ""


@dataclass(frozen=True)
class InboundMediaRef:
    """Reference to a durable Lynewright media row created from inbound content."""

    media_id: str
    name: str = ""
    content_type: str = ""
    size_bytes: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "media_id": self.media_id,
            "name": self.name,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
        }


@runtime_checkable
class ChannelAdapter(Protocol):
    """Contract every channel adapter implements."""

    async def send_message(
        self,
        *,
        connection: Mapping[str, object],
        chat_ref: str,
        message: OutboundMessage,
    ) -> SendResult: ...

    async def answer_action_prompt(
        self,
        *,
        connection: Mapping[str, object],
        prompt_ref: str,
        outcome: str,
    ) -> SendResult: ...

    def descriptor(self) -> ChannelDescriptor: ...

    def supports(self, capability: ChannelCapability) -> bool: ...


class ChannelRegistry:
    """Maps ``channel_type`` to its adapter instance."""

    def __init__(self) -> None:
        self._adapters: dict[str, object] = {}

    def register(self, adapter: object) -> None:
        descriptor_method = getattr(adapter, "descriptor", None)
        if not callable(descriptor_method):
            raise TypeError("Channel adapters must implement descriptor().")
        descriptor = descriptor_method()
        if not isinstance(descriptor, ChannelDescriptor) or not descriptor.channel_type:
            raise TypeError("Channel adapters must return a valid ChannelDescriptor.")
        existing = self._adapters.get(descriptor.channel_type)
        if existing is not None and existing is not adapter:
            raise ValueError(
                f"A different adapter is already registered for channel '{descriptor.channel_type}'."
            )
        self._adapters[descriptor.channel_type] = adapter

    def get(self, channel_type: str) -> object:
        adapter = self._adapters.get(channel_type)
        if adapter is None:
            raise ChannelError(f"Unsupported interaction channel: {channel_type}")
        return adapter

    def has(self, channel_type: str) -> bool:
        return channel_type in self._adapters

    def descriptors(self) -> list[ChannelDescriptor]:
        return [adapter.descriptor() for adapter in self._adapters.values()]  # type: ignore[attr-defined]

    def capabilities_for(self, channel_type: str) -> frozenset[ChannelCapability]:
        return self.get(channel_type).descriptor().capabilities  # type: ignore[attr-defined]


_default_registry: ChannelRegistry | None = None


def default_registry() -> ChannelRegistry:
    """Process-wide registry with every shipped adapter registered."""

    global _default_registry
    if _default_registry is None:
        registry = ChannelRegistry()
        from app.services.telegram_adapter import TelegramChannelAdapter

        registry.register(TelegramChannelAdapter())
        _default_registry = registry
    return _default_registry


def reset_default_registry() -> None:
    """Test hook: drop the cached default registry."""

    global _default_registry
    _default_registry = None
