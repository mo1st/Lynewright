from __future__ import annotations

import asyncio

from app.services.interaction_channels import OutboundMessage
from app.services.telegram_adapter import TelegramChannelAdapter
from app.services.telegram_client import TelegramAPIError


class _Client:
    def __init__(self, *, reject_copy_once: bool = False) -> None:
        self.calls: list[dict] = []
        self.reject_copy_once = reject_copy_once

    async def send_message(
        self, chat_id: str, text: str, *, inline_keyboard=None,
        disable_notification=False, parse_mode=None,
    ):
        self.calls.append({
            "chat_id": chat_id, "text": text, "inline_keyboard": inline_keyboard,
            "disable_notification": disable_notification, "parse_mode": parse_mode,
        })
        if self.reject_copy_once and inline_keyboard:
            self.reject_copy_once = False
            raise TelegramAPIError("sendMessage", "BUTTON_COPY_TEXT_INVALID")
        return {"message_id": len(self.calls)}


def _send(adapter: TelegramChannelAdapter, client: _Client, message: OutboundMessage):
    return asyncio.run(adapter.send_message(
        connection={"id": "connection-1"},
        chat_ref="12345",
        message=message,
    ))


def test_copy_text_button_shape_keeps_callbacks_and_refuses_long_values() -> None:
    buttons = TelegramChannelAdapter._inline_keyboard((
        ("📋 Titel", {"copy_text": "A & B"}),
        ("Review", "q:action:approve"),
        ("Too long", {"copy_text": "x" * 257}),
    ))
    assert buttons == [
        [{"text": "📋 Titel", "copy_text": {"text": "A & B"}}],
        [{"text": "Review", "callback_data": "q:action:approve"}],
    ]


def test_api_copy_text_rejection_retries_without_any_buttons() -> None:
    client = _Client(reject_copy_once=True)
    adapter = TelegramChannelAdapter(client_factory=lambda _connection: client, rate_limit=lambda _key: True)
    result = _send(adapter, client, OutboundMessage(
        text="<code>A &amp; B</code>",
        buttons=(("📋 Titel", {"copy_text": "A & B"}), ("Review", "q:action:approve")),
        parse_mode="HTML",
    ))
    assert result.ok
    assert len(client.calls) == 2
    assert client.calls[0]["inline_keyboard"] == [
        [{"text": "📋 Titel", "copy_text": {"text": "A & B"}}],
        [{"text": "Review", "callback_data": "q:action:approve"}],
    ]
    assert client.calls[1]["inline_keyboard"] is None
    assert client.calls[0]["text"] == client.calls[1]["text"]
    assert client.calls[1]["parse_mode"] == "HTML"
