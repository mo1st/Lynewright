"""How Telegram updates reach Lynewright: long polling (default) or a webhook.

Polling needs no public address, so a loopback install works out of the box.
Webhook mode is registered with Telegram by the app itself; nobody has to call
``setWebhook`` by hand. Both modes feed ``telegram_updates.process_update``.
"""
from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from contextlib import closing
from typing import Any
from urllib.parse import urlsplit

from app.config import get_settings
from app.database import get_connection, init_db
from app.services.application_logging import log_event
from app.services.interaction_schema import init_interaction_schema
from app.services.telegram_client import TelegramAPIError, TelegramClient

DELIVERY_MODES = ("polling", "webhook")
ALLOWED_UPDATES = ["message", "callback_query"]
POLL_TIMEOUT_SECONDS = 25
LEASE_SECONDS = 90.0
SCAN_INTERVAL_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 60.0
# A revoked or wrong token will not fix itself; check again only rarely.
UNAUTHORIZED_RETRY_SECONDS = 300.0


class DeliveryError(ValueError):
    """The requested delivery change is not possible."""


def _client(connection: dict[str, Any], *, timeout_seconds: float = 10.0) -> TelegramClient:
    from app.services.interaction_linking import decrypt_connection_credentials

    token = str(decrypt_connection_credentials(dict(connection)).get("bot_token") or "")
    if not token:
        raise DeliveryError("This Telegram connection has no bot token.")
    return TelegramClient(token, timeout_seconds=timeout_seconds)


def _update(connection_id: str, **columns: Any) -> None:
    if not columns:
        return
    assignments = ",".join(f"{name}=?" for name in columns)
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        db.execute(
            f"UPDATE interaction_connections SET {assignments} WHERE id=?",
            (*columns.values(), connection_id),
        )


def _load(connection_id: str) -> dict[str, Any] | None:
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        row = db.execute("SELECT * FROM interaction_connections WHERE id=?", (connection_id,)).fetchone()
    return dict(row) if row is not None else None


def webhook_url_for(connection_id: str) -> str:
    return f"{get_settings().public_url.rstrip('/')}/api/interactions/telegram/{connection_id}/webhook"


def webhook_available() -> bool:
    """Telegram only pushes to public HTTPS addresses."""
    parsed = urlsplit(get_settings().public_url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host not in {"", "localhost", "127.0.0.1", "::1"}


async def use_polling(connection: dict[str, Any]) -> dict[str, Any]:
    """Remove any webhook (getUpdates refuses to run while one is set)."""
    await _client(connection).call("deleteWebhook", {"drop_pending_updates": False})
    _update(str(connection["id"]), delivery_mode="polling", webhook_url="", last_error="")
    return _load(str(connection["id"])) or {}


async def use_webhook(connection: dict[str, Any]) -> dict[str, Any]:
    """Register the webhook with a fresh secret; the secret is never shown."""
    if not webhook_available():
        raise DeliveryError(
            "Webhook delivery needs LYNEWRIGHT_PUBLIC_URL to be a public https:// address."
        )
    from app.services.interaction_linking import rotate_webhook_secret

    connection_id = str(connection["id"])
    rotated = rotate_webhook_secret(tenant_id=str(connection["tenant_id"]), connection_id=connection_id)
    url = webhook_url_for(connection_id)
    await _client(connection).call("setWebhook", {
        "url": url, "secret_token": rotated["webhook_secret"],
        "allowed_updates": ALLOWED_UPDATES, "drop_pending_updates": False,
    })
    _update(connection_id, delivery_mode="webhook", webhook_url=url, last_error="",
            poll_lease_owner="", poll_lease_until=0)
    return _load(connection_id) or {}


async def set_delivery_mode(connection: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode not in DELIVERY_MODES:
        raise DeliveryError("Delivery mode must be polling or webhook.")
    try:
        if mode == "webhook":
            return await use_webhook(connection)
        return await use_polling(connection)
    except TelegramAPIError as exc:
        raise DeliveryError(f"Telegram refused the change: {exc.description}") from exc


async def remove_delivery(connection: dict[str, Any]) -> None:
    """Best-effort cleanup before a connection is deleted."""
    try:
        await _client(connection).call("deleteWebhook", {"drop_pending_updates": True})
    except Exception:  # deletion must not depend on Telegram being reachable
        pass


# ------------------------------------------------------------------- polling

def acquire_lease(connection_id: str, owner: str, *, now: float | None = None,
                  seconds: float = LEASE_SECONDS) -> bool:
    """One poller per bot: Telegram rejects concurrent getUpdates with 409."""
    moment = time.time() if now is None else now
    with closing(get_connection()) as db, db:
        init_interaction_schema(db)
        changed = db.execute(
            """UPDATE interaction_connections SET poll_lease_owner=?, poll_lease_until=?
               WHERE id=? AND (poll_lease_owner=? OR poll_lease_until<?)""",
            (owner, moment + seconds, connection_id, owner, moment),
        ).rowcount
    return changed == 1


def release_lease(connection_id: str, owner: str) -> None:
    with closing(get_connection()) as db, db:
        db.execute(
            "UPDATE interaction_connections SET poll_lease_owner='', poll_lease_until=0 "
            "WHERE id=? AND poll_lease_owner=?",
            (connection_id, owner),
        )


def polling_connection_ids() -> list[str]:
    init_db()
    with closing(get_connection()) as db:
        init_interaction_schema(db)
        rows = db.execute(
            """SELECT id FROM interaction_connections
               WHERE channel_type='telegram' AND enabled=1 AND delivery_mode='polling'
                 AND credentials_json LIKE '%bot_token%' ORDER BY id"""
        ).fetchall()
    return [str(row[0]) for row in rows]


class TelegramPoller:
    """Runs one long-poll loop per polling connection inside the API process."""

    def __init__(self, *, process_update: Any = None, sleep: Any = None) -> None:
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._process_update = process_update
        self._sleep = sleep or asyncio.sleep
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def _handle(self, connection: dict[str, Any], update: dict[str, Any]) -> None:
        if self._process_update is not None:
            await self._process_update(connection=connection, update=update)
            return
        from app.services.telegram_updates import process_update

        await process_update(connection=connection, update=update)

    async def poll_once(self, connection_id: str) -> float:
        """Run one getUpdates round. Returns how long to wait before the next."""
        connection = _load(connection_id)
        if (connection is None or not connection.get("enabled")
                or str(connection.get("delivery_mode") or "polling") != "polling"):
            return -1.0
        if not acquire_lease(connection_id, self.owner):
            return SCAN_INTERVAL_SECONDS
        try:
            client = _client(connection, timeout_seconds=POLL_TIMEOUT_SECONDS + 10)
            updates = await client.call("getUpdates", {
                "offset": int(connection.get("poll_offset") or 0) or None,
                "timeout": POLL_TIMEOUT_SECONDS, "allowed_updates": ALLOWED_UPDATES,
            })
        except TelegramAPIError as exc:
            if exc.error_code == 409:
                # A webhook is still registered (or another poller exists):
                # polling mode owns this bot, so remove the webhook.
                try:
                    await client.call("deleteWebhook", {"drop_pending_updates": False})
                except Exception:
                    pass
                _update(connection_id, last_error="Telegram reported a delivery conflict; retrying.")
                return 1.0
            if exc.error_code in {401, 404}:
                _update(connection_id, last_error="Telegram rejected the bot token. Paste a new token.")
                return UNAUTHORIZED_RETRY_SECONDS
            _update(connection_id, last_error=f"Telegram is not reachable: {exc.description}"[:300])
            return -2.0
        except DeliveryError as exc:
            _update(connection_id, last_error=str(exc))
            return UNAUTHORIZED_RETRY_SECONDS
        offset = int(connection.get("poll_offset") or 0)
        for update in updates if isinstance(updates, list) else []:
            if not isinstance(update, dict):
                continue
            try:
                await self._handle(connection, update)
            except Exception as exc:  # one update must never stop delivery
                log_event(
                    level="error", category="interaction", source="telegram_poller",
                    message="Failed to process a polled Telegram update.",
                    tenant_id=connection.get("tenant_id"),
                    details={"connection_id": connection_id}, exc=exc,
                )
            try:
                offset = max(offset, int(update.get("update_id")) + 1)
            except (TypeError, ValueError):
                continue
            _update(connection_id, poll_offset=offset)
        if connection.get("last_error"):
            _update(connection_id, last_error="")
        return 0.0

    async def _loop(self, connection_id: str, stop: asyncio.Event) -> None:
        backoff = 1.0
        try:
            while not stop.is_set():
                wait = await self.poll_once(connection_id)
                if wait == -1.0:
                    return
                if wait == -2.0:
                    wait, backoff = backoff, min(backoff * 2, MAX_BACKOFF_SECONDS)
                else:
                    backoff = 1.0
                if wait > 0:
                    await self._sleep(wait)
        finally:
            release_lease(connection_id, self.owner)

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                if get_settings().telegram_enabled:
                    wanted = set(polling_connection_ids())
                else:
                    wanted = set()
                for connection_id, task in list(self._tasks.items()):
                    if task.done() or connection_id not in wanted:
                        task.cancel()
                        self._tasks.pop(connection_id, None)
                for connection_id in wanted - set(self._tasks):
                    self._tasks[connection_id] = asyncio.create_task(self._loop(connection_id, stop))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=SCAN_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            for task in self._tasks.values():
                task.cancel()
            self._tasks.clear()
