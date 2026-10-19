from __future__ import annotations

import asyncio
from contextlib import closing
from typing import Any

import pytest

from app import database
from app.services import interaction_linking as linking
from app.services import telegram_delivery
from app.services.telegram_client import TelegramAPIError

PROJECT = "tg-project"


class FakeClient:
    def __init__(self, script: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.script = script or {}

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, dict(payload or {})))
        result = self.script.get(method, True)
        if isinstance(result, list) and result and isinstance(result[0], Exception):
            raise result.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(payload or {})
        return result


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LYNEWRIGHT_DB_PATH", str(tmp_path / "telegram.sqlite3"))
    monkeypatch.setenv("LYNEWRIGHT_REDIS_URL", "")
    monkeypatch.setenv("LYNEWRIGHT_SECRET_KEY", "t" * 40)
    database.init_db()
    with closing(database.get_connection()) as db, db:
        db.execute("INSERT OR IGNORE INTO tenant_projects (project_id, tenant_id, name) VALUES (?, 'ten_default', 'TG')", (PROJECT,))
    created = linking.create_connection(
        tenant_id="ten_default", project_id=PROJECT, channel_type="telegram",
        name="Test bot", credentials={"bot_token": "123:abc"},
    )
    client = FakeClient()
    monkeypatch.setattr(telegram_delivery, "_client", lambda _connection, **_kwargs: client)
    return {"connection_id": created["id"], "client": client}


def _row(connection_id: str) -> dict[str, Any]:
    return telegram_delivery._load(connection_id) or {}


def test_new_connections_poll_by_default(setup) -> None:
    assert _row(setup["connection_id"])["delivery_mode"] == "polling"
    assert setup["connection_id"] in telegram_delivery.polling_connection_ids()


def test_polling_feeds_the_shared_handler_and_advances_the_offset(setup) -> None:
    handled: list[int] = []

    async def process(*, connection: dict[str, Any], update: dict[str, Any]) -> None:
        handled.append(update["update_id"])

    setup["client"].script["getUpdates"] = lambda payload: (
        [{"update_id": 41, "message": {}}, {"update_id": 42, "message": {}}] if not payload.get("offset") else []
    )
    poller = telegram_delivery.TelegramPoller(process_update=process)
    assert asyncio.run(poller.poll_once(setup["connection_id"])) == 0.0
    assert handled == [41, 42]
    assert _row(setup["connection_id"])["poll_offset"] == 43
    asyncio.run(poller.poll_once(setup["connection_id"]))
    method, payload = setup["client"].calls[-1]
    assert method == "getUpdates" and payload["offset"] == 43 and payload["timeout"] == 25


def test_one_bad_update_does_not_stop_delivery(setup) -> None:
    async def process(*, connection: dict[str, Any], update: dict[str, Any]) -> None:
        if update["update_id"] == 1:
            raise RuntimeError("broken update")

    setup["client"].script["getUpdates"] = [{"update_id": 1}, {"update_id": 2}]
    poller = telegram_delivery.TelegramPoller(process_update=process)
    asyncio.run(poller.poll_once(setup["connection_id"]))
    assert _row(setup["connection_id"])["poll_offset"] == 3


def test_only_one_poller_holds_a_bot(setup) -> None:
    connection_id = setup["connection_id"]
    assert telegram_delivery.acquire_lease(connection_id, "a", now=1000.0)
    assert not telegram_delivery.acquire_lease(connection_id, "b", now=1001.0)
    assert telegram_delivery.acquire_lease(connection_id, "a", now=1002.0)
    assert telegram_delivery.acquire_lease(connection_id, "b", now=1002.0 + telegram_delivery.LEASE_SECONDS + 1)


def test_conflict_removes_a_leftover_webhook(setup) -> None:
    setup["client"].script["getUpdates"] = TelegramAPIError("getUpdates", "Conflict", error_code=409)
    wait = asyncio.run(telegram_delivery.TelegramPoller().poll_once(setup["connection_id"]))
    assert wait == 1.0
    assert ("deleteWebhook", {"drop_pending_updates": False}) in setup["client"].calls


def test_revoked_token_is_reported_and_retried_rarely(setup) -> None:
    setup["client"].script["getUpdates"] = TelegramAPIError("getUpdates", "Unauthorized", error_code=401)
    wait = asyncio.run(telegram_delivery.TelegramPoller().poll_once(setup["connection_id"]))
    assert wait == telegram_delivery.UNAUTHORIZED_RETRY_SECONDS
    assert "rejected the bot token" in _row(setup["connection_id"])["last_error"]


def test_webhook_needs_a_public_https_address(setup, monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_PUBLIC_URL", "http://localhost:8080")
    with pytest.raises(telegram_delivery.DeliveryError, match="public https"):
        asyncio.run(telegram_delivery.set_delivery_mode(_row(setup["connection_id"]), "webhook"))
    assert setup["client"].calls == []


def test_app_registers_and_removes_the_webhook(setup, monkeypatch) -> None:
    from app.services import telegram_updates

    monkeypatch.setenv("LYNEWRIGHT_PUBLIC_URL", "https://lynewright.example.com")
    connection_id = setup["connection_id"]
    row = asyncio.run(telegram_delivery.set_delivery_mode(_row(connection_id), "webhook"))
    method, payload = setup["client"].calls[-1]
    assert method == "setWebhook"
    assert payload["url"] == f"https://lynewright.example.com/api/interactions/telegram/{connection_id}/webhook"
    assert row["delivery_mode"] == "webhook" and row["webhook_url"] == payload["url"]
    assert connection_id not in telegram_delivery.polling_connection_ids()

    # The secret the app registered is the one the webhook route accepts.
    accepted = asyncio.run(telegram_updates.handle_webhook(
        connection_id=connection_id,
        headers={"X-Telegram-Bot-Api-Secret-Token": payload["secret_token"]},
        raw_body=b'{"update_id": 7}',
    ))
    assert accepted["ok"] is True

    asyncio.run(telegram_delivery.set_delivery_mode(_row(connection_id), "polling"))
    assert setup["client"].calls[-1][0] == "deleteWebhook"
    with pytest.raises(telegram_updates.WebhookRejected) as rejected:
        asyncio.run(telegram_updates.handle_webhook(
            connection_id=connection_id,
            headers={"X-Telegram-Bot-Api-Secret-Token": payload["secret_token"]},
            raw_body=b'{"update_id": 8}',
        ))
    assert rejected.value.status_code == 409


def test_link_code_comes_with_a_telegram_deep_link_and_live_status(setup) -> None:
    connection_id = setup["connection_id"]
    linking.verify_connection_identity(tenant_id="ten_default", connection_id=connection_id, external_identity="@lyne_test_bot")
    admin = database.create_user("owner@example.com", "a-long-private-passphrase", "admin")
    token = linking.create_link_token(
        tenant_id="ten_default", project_id=PROJECT, user_id=admin["id"], connection_id=connection_id, ttl_seconds=600,
    )
    assert token["deep_link"] == f"https://t.me/lyne_test_bot?start={token['token']}"
    # Telegram start parameters allow at most 64 characters of [A-Za-z0-9_-].
    assert len(token["token"]) <= 64 and token["token"].replace("_", "").replace("-", "").isalnum()

    status = linking.link_token_status(tenant_id="ten_default", user_id=admin["id"], token_id=token["id"])
    assert status == {"id": token["id"], "linked": False, "expired": False, "external_username": ""}
    linking.redeem_link_token(connection_id=connection_id, raw_token=token["token"],
                              external_user_id="555", external_username="owner_tg")
    status = linking.link_token_status(tenant_id="ten_default", user_id=admin["id"], token_id=token["id"])
    assert status["linked"] is True and status["external_username"] == "owner_tg"

    other = database.create_user("other@example.com", "another-long-passphrase", "editor")
    with pytest.raises(LookupError):
        linking.link_token_status(tenant_id="ten_default", user_id=other["id"], token_id=token["id"])


def test_disabled_telegram_stops_polling(setup, monkeypatch) -> None:
    monkeypatch.setenv("LYNEWRIGHT_TELEGRAM_ENABLED", "0")
    stop = asyncio.Event()

    async def scenario() -> dict[str, Any]:
        poller = telegram_delivery.TelegramPoller()
        task = asyncio.create_task(poller.run(stop))
        await asyncio.sleep(0.05)
        tasks = dict(poller._tasks)
        stop.set()
        await task
        return tasks

    assert asyncio.run(scenario()) == {}


def test_deep_link_start_links_the_chat_and_opens_a_conversation(setup, monkeypatch) -> None:
    from app.services import telegram_updates
    from app.services.interaction_conversations import list_conversations

    replies: list[str] = []

    async def capture(*, connection: dict[str, Any], chat_ref: str, text: str) -> None:
        replies.append(text)

    monkeypatch.setattr(telegram_updates, "_reply", capture)
    connection_id = setup["connection_id"]
    linking.verify_connection_identity(tenant_id="ten_default", connection_id=connection_id, external_identity="@lyne_test_bot")
    admin = database.create_user("owner@example.com", "a-long-private-passphrase", "admin")
    token = linking.create_link_token(
        tenant_id="ten_default", project_id=PROJECT, user_id=admin["id"], connection_id=connection_id, ttl_seconds=600,
    )
    update = {"update_id": 900, "message": {
        "message_id": 1, "text": f"/start {token['token']}",
        "chat": {"id": 777, "type": "private"}, "from": {"id": 555, "username": "owner_tg"},
    }}
    outcome = asyncio.run(telegram_updates.process_update(connection=_row(connection_id), update=update))
    assert outcome["handled"] == "connected"
    assert replies and replies[-1].startswith("Linked.")
    chats = [item["external_chat_id"] for item in list_conversations(tenant_id="ten_default", project_id=PROJECT, channel_type="telegram")]
    assert chats == ["777"]
    assert _row(connection_id)["last_update_at"]
    # The same update delivered twice (webhook retry or poll overlap) is ignored.
    assert asyncio.run(telegram_updates.process_update(connection=_row(connection_id), update=update)) == {"ok": True, "duplicate": True}
