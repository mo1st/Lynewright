"""Telegram adapter implementing the neutral channel contract.

Translates OutboundMessage/media references into Bot API calls and enforces
outbound pacing through the shared rate limiter. Media is never treated as
Telegram-owned: outbound media references must be either public HTTPS URLs
(SSRF-validated) or durable ``managed://`` Lynewright media rows, which are
exposed to Telegram only through a short-lived publication-media token URL.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol

from app.services.interaction_channels import (
    ChannelCapability,
    ChannelDescriptor,
    ChannelError,
    OutboundMessage,
    SendResult,
)
from app.services.rate_limiter import RateLimiterUnavailable, get_rate_limiter
from app.services.telegram_client import TelegramClient
from app.services.platform_services import HTTPService, PlatformValidationError

CHANNEL_TYPE = "telegram"


class MediaTokenIssuer(Protocol):
    def public_url(self, *, media_id: str, ttl_seconds: int) -> str: ...


_media_token_issuer: MediaTokenIssuer | None = None


def register_media_token_issuer(issuer: MediaTokenIssuer) -> Callable[[], bool]:
    """Register an optional managed-media URL issuer."""
    global _media_token_issuer
    previous = _media_token_issuer
    _media_token_issuer = issuer

    def unregister() -> bool:
        global _media_token_issuer
        if _media_token_issuer is not issuer:
            return False
        _media_token_issuer = previous
        return True

    return unregister

# The client spaces sends one second apart; this distributed window guard
# keeps one chat from exceeding that pace when multiple workers are active.
PER_CHAT_SENDS_PER_WINDOW = 60
SEND_WINDOW_SECONDS = 60


class TelegramChannelAdapter:
    def __init__(
        self,
        *,
        client_factory: Callable[[Mapping[str, object]], TelegramClient] | None = None,
        rate_limit: Callable[..., bool] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._rate_limit = rate_limit

    # ------------------------------------------------------------- descriptor

    def descriptor(self) -> ChannelDescriptor:
        return ChannelDescriptor(
            channel_type=CHANNEL_TYPE,
            display_name="Telegram",
            capabilities=frozenset(
                {
                    ChannelCapability.MESSAGE_RECEIVE,
                    ChannelCapability.MESSAGE_SEND,
                    ChannelCapability.MESSAGE_EDIT,
                    ChannelCapability.FILE_RECEIVE,
                    ChannelCapability.FILE_SEND,
                    ChannelCapability.NOTIFICATION_SEND,
                    ChannelCapability.APPROVAL_REQUEST,
                    ChannelCapability.APPROVAL_RESOLVE,
                }
            ),
        )

    def supports(self, capability: ChannelCapability) -> bool:
        return self.descriptor().supports(capability)

    # ------------------------------------------------------------------ client

    def _limiter_allows(self, key: str) -> bool:
        if self._rate_limit is not None:
            return self._rate_limit(key)
        try:
            limiter = get_rate_limiter()
        except RateLimiterUnavailable:
            return True
        return limiter.consume(
            namespace="interaction_telegram_send",
            key=key,
            maximum=PER_CHAT_SENDS_PER_WINDOW,
            window_seconds=SEND_WINDOW_SECONDS,
        )

    def client_for(self, connection: Mapping[str, object]) -> TelegramClient:
        if self._client_factory is not None:
            return self._client_factory(connection)
        from app.services.interaction_linking import decrypt_connection_credentials

        credentials = decrypt_connection_credentials(dict(connection))
        bot_token = str(credentials.get("bot_token") or "")
        if not bot_token:
            raise ChannelError("This Telegram connection has no bot token configured.")
        return TelegramClient(bot_token)

    @staticmethod
    def _connection_key(connection: Mapping[str, object], chat_ref: str) -> str:
        return f"{connection.get('id', '')}:{chat_ref}"

    # ------------------------------------------------------------------ sends

    @staticmethod
    def _inline_keyboard(buttons: tuple[tuple[str, Any], ...]) -> list[list[dict[str, Any]]]:
        keyboard: list[list[dict[str, Any]]] = []
        for label, action in buttons:
            if isinstance(action, Mapping) and "copy_text" in action:
                value = str(action.get("copy_text") or "")
                if not value or len(value) > 256:
                    continue
                keyboard.append([{"text": str(label)[:64], "copy_text": {"text": value}}])
            else:
                keyboard.append([{"text": str(label)[:64], "callback_data": str(action)[:64]}])
        return keyboard

    @staticmethod
    def _copy_text_rejected(exc: Exception) -> bool:
        """Identify a Bot API rejection, without retrying transport failures."""
        from app.services.telegram_client import TelegramAPIError

        if not isinstance(exc, TelegramAPIError):
            return False
        description = exc.description.lower()
        if "request failed" in description or "non-json response" in description:
            return False
        return any(token in description for token in ("copy_text", "copy text", "inline keyboard", "button_"))

    async def send_message(
        self,
        *,
        connection: Mapping[str, object],
        chat_ref: str,
        message: OutboundMessage,
    ) -> SendResult:
        text = message.text if message.parse_mode else message.text[:4096]
        if not text and not message.media_refs:
            return SendResult(ok=False, detail="empty message")
        if not self._limiter_allows(self._connection_key(connection, chat_ref)):
            return SendResult(ok=False, detail="rate limited; retry later")
        client = self.client_for(connection)
        keyboard = self._inline_keyboard(message.buttons)
        try:
            if message.edit_external_message_id:
                result = await client.edit_message_text(
                    chat_ref,
                    str(message.edit_external_message_id),
                    text,
                    inline_keyboard=keyboard or None,
                    parse_mode=message.parse_mode,
                )
                external_id = str(result.get("message_id", message.edit_external_message_id))
            else:
                result = await client.send_message(
                    chat_ref,
                    text,
                    inline_keyboard=keyboard or None,
                    disable_notification=message.silent,
                    parse_mode=message.parse_mode,
                )
                external_id = str(result.get("message_id", ""))
        except Exception as exc:  # provider errors must not crash callers
            copy_requested = any(isinstance(action, Mapping) and "copy_text" in action for _, action in message.buttons)
            if not message.edit_external_message_id and copy_requested and self._copy_text_rejected(exc):
                try:
                    result = await client.send_message(
                        chat_ref, text, inline_keyboard=None,
                        disable_notification=message.silent, parse_mode=message.parse_mode,
                    )
                    external_id = str(result.get("message_id", ""))
                except Exception as fallback_exc:
                    return SendResult(ok=False, detail=f"{type(fallback_exc).__name__}: {fallback_exc}"[:500])
            else:
                return SendResult(ok=False, detail=f"{type(exc).__name__}: {exc}"[:500])
        for media_ref in message.media_refs:
            media_result = await self.send_media(
                connection=connection, chat_ref=chat_ref, media_ref=dict(media_ref)
            )
            if not media_result.ok:
                return SendResult(ok=False, external_message_id=external_id, detail=media_result.detail)
        return SendResult(ok=True, external_message_id=external_id)

    async def send_media(
        self,
        *,
        connection: Mapping[str, object],
        chat_ref: str,
        media_ref: Mapping[str, Any],
        caption: str = "",
        buttons: tuple[tuple[str, str], ...] = (),
    ) -> SendResult:
        """Send one durable Lynewright media reference.

        ``media_ref`` shapes accepted:
          * {"media_id": "med_..."}                       -> resolved via platform_media
          * {"media_id": ..., "url": "managed://med_..."} -> managed row + token URL
          * {"url": "https://..."}                        -> SSRF-validated direct URL
        """

        client = self.client_for(connection)
        url = ""
        content_type = str(media_ref.get("content_type") or "")
        name = str(media_ref.get("name") or "")
        if not content_type and media_ref.get("media_id"):
            # A durable reference is often all the dispatcher has.  Resolve
            # its type inside the connection tenant so videos use Telegram's
            # native sendVideo method instead of falling back to a document.
            try:
                from app.services.platform_services import MediaService

                row = MediaService(tenant_id=str(connection.get("tenant_id") or "")).get(
                    str(media_ref.get("media_id")),
                )
                content_type = str(row.get("content_type") or "")
                name = name or str(row.get("name") or "")
            except Exception:
                pass
        try:
            raw_url = str(media_ref.get("url") or "")
            if raw_url.startswith("managed://"):
                url = await self._public_url_for_managed(
                    connection=connection, media_ref=media_ref, managed_ref=raw_url
                )
            elif raw_url.startswith("https://"):
                try:
                    HTTPService().validate_public_url(raw_url)
                except PlatformValidationError as exc:
                    raise ChannelError(str(exc)) from exc
                url = raw_url
            elif media_ref.get("media_id"):
                url = await self._public_url_for_managed(
                    connection=connection, media_ref=media_ref, managed_ref=""
                )
            else:
                return SendResult(ok=False, detail="media reference has no usable source")
        except (ChannelError, ValueError) as exc:
            return SendResult(ok=False, detail=f"media rejected: {exc}"[:300])
        normalized_type = content_type.lower()
        is_video = normalized_type.startswith("video/")
        is_document = bool(content_type) and not normalized_type.startswith(("image/", "video/"))
        method = "sendVideo" if is_video else ("sendDocument" if is_document else "sendPhoto")
        caption_text = (caption or name)[:1000]
        try:
            if method == "sendVideo":
                result = await client.send_video(chat_ref, url, caption=caption_text)
            elif method == "sendDocument":
                result = await client.send_document(chat_ref, url, caption=caption_text)
            elif buttons:
                result = await client.send_photo(
                    chat_ref, url, caption=caption_text, inline_keyboard=self._inline_keyboard(buttons),
                )
            else:
                result = await client.send_photo(chat_ref, url, caption=caption_text)
        except Exception as exc:
            return SendResult(ok=False, detail=f"{type(exc).__name__}: {exc}"[:500])
        return SendResult(ok=True, external_message_id=str(result.get("message_id", "")))

    async def send_photo_file(
        self,
        *,
        connection: Mapping[str, object],
        chat_ref: str,
        path: str,
        filename: str,
        caption: str = "",
        buttons: tuple[tuple[str, str], ...] = (),
    ) -> SendResult:
        """Upload a local review photo directly, with its inline actions."""
        if not self._limiter_allows(self._connection_key(connection, chat_ref)):
            return SendResult(ok=False, detail="rate limited; retry later")
        client = self.client_for(connection)
        try:
            result = await client.send_photo_file(
                chat_ref, path, filename, caption=caption,
                inline_keyboard=self._inline_keyboard(buttons) or None,
            )
        except Exception as exc:
            return SendResult(ok=False, detail=f"{type(exc).__name__}: {exc}"[:500])
        return SendResult(ok=True, external_message_id=str(result.get("message_id", "")))

    async def _public_url_for_managed(
        self,
        *,
        connection: Mapping[str, object],
        media_ref: Mapping[str, Any],
        managed_ref: str,
    ) -> str:
        """Mint a short-lived serving URL for a managed media row.

        Telegram must fetch the bytes itself; we hand it a scoped opaque token
        served by the existing publication-media endpoint instead of exposing
        the file publicly.
        """

        media_id = str(media_ref.get("media_id") or managed_ref.removeprefix("managed://"))
        issuer = _media_token_issuer
        if issuer is None:
            raise ChannelError("Managed-media delivery is not configured.")
        url = issuer.public_url(media_id=media_id, ttl_seconds=3600)
        try:
            HTTPService().validate_public_url(url)
        except PlatformValidationError as exc:
            raise ChannelError(str(exc)) from exc
        return url

    async def answer_action_prompt(
        self,
        *,
        connection: Mapping[str, object],
        prompt_ref: str,
        outcome: str,
    ) -> SendResult:
        """Edit an approval prompt message after a decision (best effort).

        ``prompt_ref`` is ``"<chat_ref>:<external_message_id>"`` as recorded on
        the interaction action. A failed edit never fails the resolution.
        """

        if ":" not in prompt_ref:
            return SendResult(ok=False, detail="prompt ref is not editable")
        chat_ref, _, external_message_id = prompt_ref.partition(":")
        client = self.client_for(connection)
        try:
            await client.edit_message_text(chat_ref, external_message_id, outcome)
        except Exception as exc:
            return SendResult(ok=False, detail=f"{type(exc).__name__}: {exc}"[:300])
        return SendResult(ok=True)

    # ---------------------------------------------------------------- receive

    async def fetch_media(
        self,
        *,
        connection: Mapping[str, object],
        file_id: str,
        max_bytes: int | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        """Fetch inbound file bytes from the pinned Bot API host.

        The only host ever contacted is the configured Bot API base URL; the
        provider-supplied value is the opaque ``file_path`` returned by
        ``getFile``, appended to that pinned host.
        """

        from app.services.telegram_client import DEFAULT_MAX_DOWNLOAD_BYTES

        limit = max_bytes or DEFAULT_MAX_DOWNLOAD_BYTES
        client = self.client_for(connection)
        info = await client.get_file(file_id)
        file_path = str(info.get("file_path") or "")
        if not file_path:
            raise ChannelError("Telegram did not return a downloadable file path.")
        data = await client.download_file(file_path, max_bytes=limit)
        meta = {
            "file_path": file_path,
            "file_size": info.get("file_size"),
            "mime": info.get("mime_type", ""),
        }
        return data, meta

    async def describe_bot(self, connection: Mapping[str, object]) -> dict[str, Any]:
        client = self.client_for(connection)
        me = await client.get_me()
        return {
            "bot_id": str(me.get("id", "")),
            "username": str(me.get("username") or ""),
            "display_name": str(me.get("first_name") or ""),
        }

