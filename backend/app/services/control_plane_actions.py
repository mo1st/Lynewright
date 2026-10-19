"""Host-owned dispatch for already-authorized Control Plane actions."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.services.control_plane_action_tokens import ControlPlaneActionTokenService
from app.services.platform_services import PlatformConflictError, PlatformOperationError, PlatformServiceError, PlatformValidationError


ActionHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class ControlPlaneActionDispatcher:
    def __init__(self, token_service: ControlPlaneActionTokenService | None = None) -> None:
        self.tokens = token_service or ControlPlaneActionTokenService()
        self._handlers: dict[tuple[str, str], ActionHandler] = {}

    def register(self, *, capability_id: str, action: str, handler: ActionHandler) -> None:
        if not capability_id or not action or not callable(handler):
            raise PlatformValidationError("Control Plane action registration is invalid.")
        key = (capability_id, action)
        if key in self._handlers and self._handlers[key] is not handler:
            raise PlatformConflictError("Control Plane action is already registered.")
        self._handlers[key] = handler

    def unregister(self, *, capability_id: str, action: str, handler: ActionHandler) -> bool:
        """Remove one provider-owned handler only when identity still matches."""
        key = (capability_id, action)
        existing = self._handlers.get(key)
        if existing is None:
            return False
        if existing is not handler:
            raise PlatformConflictError("Control Plane action is owned by another handler.")
        self._handlers.pop(key, None)
        return True

    def dispatch(self, token: str, *, tenant_id: str, actor_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        claims = self.tokens.peek(token, tenant_id=tenant_id)
        handler = self._handlers.get((str(claims["capability_id"]), str(claims["action"])))
        if handler is None:
            raise PlatformConflictError("Control Plane action is not available.")
        if payload is not None and not isinstance(payload, dict):
            raise PlatformValidationError("Control Plane action payload must be an object.")
        body = dict(payload or {})
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("Control Plane action payload must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > 32 * 1024:
            raise PlatformValidationError("Control Plane action payload exceeds the 32 KiB limit.")
        scope = {key: claims[key] for key in ("project_id", "resource_type", "resource_id", "integration_id", "connection_id", "surface_path", "component_id", "action", "capability_id", "context_revision")}
        consumed = self.tokens.consume(token, tenant_id=tenant_id, actor_id=actor_id, context=scope)
        try:
            result = handler(consumed, body)
        except PlatformServiceError:
            raise
        except Exception as exc:
            raise PlatformOperationError("Control Plane action execution failed.") from exc
        if not isinstance(result, dict):
            raise PlatformOperationError("Control Plane action returned an invalid result.")
        try:
            result_bytes = len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise PlatformOperationError("Control Plane action returned an invalid result.") from exc
        if result_bytes > 64 * 1024:
            raise PlatformOperationError("Control Plane action result exceeds the 64 KiB limit.")
        return {"ok": True, "capability_id": consumed["capability_id"], "action": consumed["action"], "result": result}


__all__ = ["ActionHandler", "ControlPlaneActionDispatcher"]
