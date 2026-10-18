"""Minimal async Telegram Bot API client.

The client pins every request to the configured Bot API base URL (default
``https://api.telegram.org``). Nothing here ever fetches an attacker-supplied
URL: file downloads go through ``getFile`` and are read from the same pinned
host. Bot tokens never appear in error messages or logs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time
from typing import Any, Awaitable, Callable

import httpx

from app.config import get_settings

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_DOWNLOAD_BYTES = 10_000_000
# Telegram's Bot API caps a multipart-uploaded file at 50 MB; refuse a little
# under that so the request is never attempted only to be rejected upstream
# (Phase 24).
MAX_UPLOAD_BYTES = 49 * 1024 * 1024
# Telegram's sendPhoto limit is lower than its general multipart upload limit.
MAX_PHOTO_UPLOAD_BYTES = 10_000_000
# A 59-page interior is about 37 MB; the upload itself completes fine, but
# waiting for Telegram's answer after that much data can exceed the default
# 10 s used for every other, much smaller call (Phase 24b).
DOCUMENT_UPLOAD_TIMEOUT_SECONDS = 180.0
CHAT_SEND_INTERVAL_SECONDS = 1.0
_CHAT_PACE_LOCK = threading.Lock()
_CHAT_NEXT_SEND_AT: dict[str, float] = {}
TelegramSleep = Callable[[float], Awaitable[None]]

# Transport injection point for tests: (method, payload) -> response JSON.
TelegramTransport = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
# Multipart transport injection point for tests: (method, form_fields, files) -> response JSON.
TelegramFileTransport = Callable[[str, dict[str, Any], dict[str, tuple[str, bytes]]], Awaitable[dict[str, Any]]]


class TelegramAPIError(RuntimeError):
    """A Telegram API call failed. Messages are safe to surface/log."""

    def __init__(self, method: str, description: str, *, retry_after: float | None = None) -> None:
        super().__init__(f"telegram {method} failed: {description}")
        self.method = method
        self.description = description
        self.retry_after = retry_after


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        *,
        api_base_url: str = "",
        transport: TelegramTransport | None = None,
        downloader: Callable[[str], Awaitable[bytes]] | None = None,
        file_transport: TelegramFileTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        sleep: TelegramSleep | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required.")
        self.bot_token = bot_token
        base = api_base_url or get_settings().telegram_api_base_url
        self.api_base_url = base.rstrip("/")
        self._transport = transport
        self._downloader = downloader
        self._file_transport = file_transport
        self._timeout_seconds = timeout_seconds
        self._sleep = sleep or asyncio.sleep

    def _method_url(self, method: str) -> tuple[str, str]:
        """Return (url, safe_label). The label never contains the token."""

        url = f"{self.api_base_url}/bot{self.bot_token}/{method}"
        return url, f"{self.api_base_url}/bot<redacted>/{method}"

    async def _pace_chat(self, chat_id: Any) -> None:
        key = str(chat_id or "").strip()
        if not key or CHAT_SEND_INTERVAL_SECONDS <= 0:
            return
        now = time.monotonic()
        with _CHAT_PACE_LOCK:
            due = max(now, _CHAT_NEXT_SEND_AT.get(key, now))
            _CHAT_NEXT_SEND_AT[key] = due + CHAT_SEND_INTERVAL_SECONDS
        if due > now:
            await self._sleep(due - now)

    @staticmethod
    def _api_error(method: str, data: Any, *, status_code: int = 0) -> TelegramAPIError:
        value = data if isinstance(data, dict) else {}
        params = value.get("parameters") if isinstance(value.get("parameters"), dict) else {}
        limited = status_code == 429 or value.get("error_code") == 429
        retry_after = None
        if limited:
            try:
                retry_after = max(0.0, float(params.get("retry_after") or 1.0))
            except (TypeError, ValueError):
                retry_after = 1.0
        description = str(value.get("description") or (f"HTTP {status_code}" if status_code else "unknown error"))
        return TelegramAPIError(method, description, retry_after=retry_after)

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {key: value for key, value in (payload or {}).items() if value is not None}
        for attempt in range(3):
            if method.startswith("send"):
                await self._pace_chat(body.get("chat_id"))
            try:
                if self._transport is not None:
                    data = await self._transport(method, body)
                    if isinstance(data, dict) and data.get("ok") is False:
                        raise self._api_error(method, data)
                    return self._unwrap(data)
                url, _safe_label = self._method_url(method)
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    resp = await client.post(url, json=body)
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise TelegramAPIError(method, f"non-JSON response (HTTP {resp.status_code})") from exc
                if resp.status_code >= 400 or not data.get("ok"):
                    raise self._api_error(method, data, status_code=resp.status_code)
                return self._unwrap(data)
            except TelegramAPIError as exc:
                if exc.retry_after is None or attempt >= 2:
                    raise
                await self._sleep(exc.retry_after)
            except httpx.HTTPError as exc:  # network/timeout errors are not retried here
                raise TelegramAPIError(method, f"request failed ({type(exc).__name__})") from exc
        raise TelegramAPIError(method, "rate limit retry budget exhausted")

    @staticmethod
    def _unwrap(response: Any) -> Any:
        """Bot API envelopes are {ok, result}; callers want the result."""

        if isinstance(response, dict) and "result" in response:
            return response["result"]
        return response

    async def download_file(
        self, file_path: str, *, max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES
    ) -> bytes:
        """Download a file by its Bot API ``file_path`` from the pinned host."""

        if self._downloader is not None:
            data = await self._downloader(file_path)
            if len(data) > max_bytes:
                raise TelegramAPIError("download", "file exceeds the allowed size")
            return data
        url = f"{self.api_base_url}/file/bot{self.bot_token}/{file_path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=False) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code >= 400:
                        raise TelegramAPIError("download", f"HTTP {resp.status_code}")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise TelegramAPIError("download", "file exceeds the allowed size")
                        chunks.append(chunk)
                    return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise TelegramAPIError("download", f"request failed ({type(exc).__name__})") from exc

    # ------------------------------------------------ typed convenience calls

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        disable_notification: bool = False,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        if disable_notification:
            payload["disable_notification"] = True
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self.call("sendMessage", payload)

    async def edit_message_text(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        *,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text,
        }
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self.call("editMessageText", payload)

    async def send_photo(
        self, chat_id: str, photo: str, caption: str = "",
        *, inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo}
        if caption:
            payload["caption"] = caption[:1000]
        if inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        return await self.call("sendPhoto", payload)

    async def send_photo_file(
        self, chat_id: str, path: str, filename: str, caption: str = "",
        *, inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> dict[str, Any]:
        """Multipart-upload a local photo so Telegram does not fetch its URL.

        Telegram's URL fetch has a smaller size limit than multipart upload.
        Uploading the review photo directly also keeps Telegram's fetcher from
        failing on an otherwise valid, scoped Lynewright media URL.
        """
        size = Path(path).stat().st_size
        if size > MAX_PHOTO_UPLOAD_BYTES:
            raise TelegramAPIError(
                "sendPhoto",
                f"{filename} is {size / (1024 * 1024):.1f} MB, over the 10 MB photo limit",
            )
        content = Path(path).read_bytes()
        fields: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption[:1000]
        if inline_keyboard:
            fields["reply_markup"] = json.dumps({"inline_keyboard": inline_keyboard})
        files = {"photo": (filename, content)}
        for attempt in range(3):
            await self._pace_chat(chat_id)
            try:
                if self._file_transport is not None:
                    data = await self._file_transport("sendPhoto", fields, files)
                    if isinstance(data, dict) and data.get("ok") is False:
                        raise self._api_error("sendPhoto", data)
                    return self._unwrap(data)
                url, _safe_label = self._method_url("sendPhoto")
                async with httpx.AsyncClient(timeout=DOCUMENT_UPLOAD_TIMEOUT_SECONDS) as client:
                    resp = await client.post(url, data=fields, files=files)
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise TelegramAPIError("sendPhoto", f"non-JSON response (HTTP {resp.status_code})") from exc
                if resp.status_code >= 400 or not data.get("ok"):
                    raise self._api_error("sendPhoto", data, status_code=resp.status_code)
                return self._unwrap(data)
            except TelegramAPIError as exc:
                if exc.retry_after is None or attempt >= 2:
                    raise
                await self._sleep(exc.retry_after)
            except httpx.HTTPError as exc:
                raise TelegramAPIError("sendPhoto", f"request failed ({type(exc).__name__})") from exc
        raise TelegramAPIError("sendPhoto", "rate limit retry budget exhausted")

    async def edit_message_reply_markup(
        self, chat_id: str, message_id: str,
        *, inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> dict[str, Any]:
        """Replace (or with no keyboard, remove) the buttons under any message, photos included."""

        payload: dict[str, Any] = {
            "chat_id": chat_id, "message_id": int(message_id),
            "reply_markup": {"inline_keyboard": inline_keyboard or []},
        }
        return await self.call("editMessageReplyMarkup", payload)

    async def send_video(self, chat_id: str, video: str, caption: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "video": video}
        if caption:
            payload["caption"] = caption[:1000]
        return await self.call("sendVideo", payload)

    async def send_document(
        self, chat_id: str, document: str, caption: str = ""
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "document": document}
        if caption:
            payload["caption"] = caption[:1000]
        return await self.call("sendDocument", payload)

    async def send_document_file(
        self, chat_id: str, path: str, filename: str, caption: str = "",
    ) -> dict[str, Any]:
        """Multipart-upload a local file as a document, unlike ``send_document``
        (which sends a URL or file_id that Telegram fetches itself). Telegram's
        Bot API caps a URL-sent file at 20 MB but a multipart upload at 50 MB,
        so a large document (tens of MB) must go this way.

        Refuses a file over ``MAX_UPLOAD_BYTES`` with a clear error before
        attempting anything -- it never even reads the file in that case.
        """
        size = Path(path).stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise TelegramAPIError(
                "sendDocument",
                f"{filename} is {size / (1024 * 1024):.1f} MB, over the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit",
            )
        content = Path(path).read_bytes()
        fields: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption[:1000]
        files = {"document": (filename, content)}
        for attempt in range(3):
            await self._pace_chat(chat_id)
            try:
                if self._file_transport is not None:
                    data = await self._file_transport("sendDocument", fields, files)
                    if isinstance(data, dict) and data.get("ok") is False:
                        raise self._api_error("sendDocument", data)
                    return self._unwrap(data)
                url, _safe_label = self._method_url("sendDocument")
                async with httpx.AsyncClient(timeout=DOCUMENT_UPLOAD_TIMEOUT_SECONDS) as client:
                    resp = await client.post(url, data=fields, files=files)
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise TelegramAPIError("sendDocument", f"non-JSON response (HTTP {resp.status_code})") from exc
                if resp.status_code >= 400 or not data.get("ok"):
                    raise self._api_error("sendDocument", data, status_code=resp.status_code)
                return self._unwrap(data)
            except TelegramAPIError as exc:
                if exc.retry_after is None or attempt >= 2:
                    raise
                await self._sleep(exc.retry_after)
            except httpx.HTTPError as exc:
                raise TelegramAPIError("sendDocument", f"request failed ({type(exc).__name__})") from exc
        raise TelegramAPIError("sendDocument", "rate limit retry budget exhausted")

    async def answer_callback_query(
        self, callback_query_id: str, text: str = "", show_alert: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "text": text[:200],
            "show_alert": show_alert,
        }
        return await self.call("answerCallbackQuery", payload)

    async def get_file(self, file_id: str) -> dict[str, Any]:
        result = await self.call("getFile", {"file_id": file_id})
        return result if isinstance(result, dict) else {}
