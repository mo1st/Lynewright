from __future__ import annotations

import sqlite3
from typing import Any

from app.services import line_engine, notification_gateway
from app.services.line_engine import ProductionLineEngine
from app.services.platform_services import PlatformStore

TENANT = "ten_notify_test"
PROJECT = "notify-project"


class _InlineThread:
    def __init__(self, *, target, **_kwargs) -> None:
        self.target = target

    def start(self) -> None:
        self.target()


def test_review_wait_and_finish_send_notifications(monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    async def capture(**kwargs: Any) -> dict[str, Any]:
        sent.append(kwargs)
        return {}

    monkeypatch.setattr(notification_gateway, "send_notification", capture)
    monkeypatch.setattr(line_engine.threading, "Thread", _InlineThread)
    monkeypatch.setenv("LYNEWRIGHT_PUBLIC_URL", "http://localhost:8080")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    engine = ProductionLineEngine(store=PlatformStore(lambda: db), tenant_id=TENANT)
    engine.db.put_line(
        "notify-line", name="Notify line", project_id=PROJECT,
        definition={"stages": [{"id": "review", "kind": "gate:review", "items_template": [{"id": "draft", "title": "Draft"}]}]},
        enabled=True, actor_id="admin", operation_directory=set(),
    )
    run = engine.start("notify-line", variables={}, project_id=PROJECT)
    assert run["status"] == "waiting"
    assert [item["event_class"] for item in sent] == ["review_waiting"]
    assert sent[0]["body"].endswith("http://localhost:8080/reviews")
    assert sent[0]["dedupe_key"] == f"line-status:{run['id']}:review:waiting"

    engine.continue_run(run["id"], value={"approved": True}, actor={"user_id": "admin"}, actor_id="admin")
    assert [item["event_class"] for item in sent] == ["review_waiting", "run_done"]
    assert sent[1]["body"].endswith(f"/runs/{run['id']}")
    db.close()
