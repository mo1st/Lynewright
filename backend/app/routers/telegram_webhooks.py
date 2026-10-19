"""Public Telegram webhook endpoint.

This route is intentionally unauthenticated at the Lynewright session layer:
Telegram cannot present a Lynewright session. Every delivery is authorized by the
connection's webhook secret header (verified constant-time against a stored
hash), rate limited, replay-protected, and size-bounded inside
``telegram_updates.handle_webhook``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from app.services.telegram_updates import WebhookRejected, handle_webhook

router = APIRouter(prefix="/api/interactions/telegram", tags=["telegram-webhooks"])


@router.post("/{connection_id}/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_telegram_update(connection_id: str, request: Request) -> dict:
    raw_body = await request.body()
    try:
        return await handle_webhook(
            connection_id=connection_id, headers=request.headers, raw_body=raw_body,
        )
    except WebhookRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/{connection_id}/webhook")
def telegram_webhook_handshake(connection_id: str) -> dict:
    """Telegram pings the bare URL when registering a webhook; answer 200."""
    return {"ok": True}
