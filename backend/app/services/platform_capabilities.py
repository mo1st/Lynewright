"""Unified authentication and capability policy for external Lynewright adapters."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.services.platform_models import ClientCreateInput, ClientUpdateInput
from app.services.platform_services import (
    PlatformConflictError,
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    PlatformValidationError,
    _validated,
)


PUBLIC_CAPABILITIES = frozenset({
    "system:read", "system:diagnostics",
    "projects:read", "projects:create", "projects:update", "projects:delete",
    "jobs:read", "jobs:create", "jobs:cancel", "jobs:retry",
    "media:read", "media:create", "media:delete",
    "ai:use", "ai:read-config", "ai:configure", "ai:test",
    "credentials:list", "credentials:status", "credentials:set", "credentials:delete", "credentials:use",
    "settings:read", "settings:write", "network:use", "events:publish",
    "storage:read", "storage:write", "mappings:read", "mappings:write",
    "sync:read", "sync:write",
    "sources:read", "sources:write", "research:read",
    "customer_studio:read", "customer_studio:write", "customer_studio:publish",
    "publishing:read", "publishing:write",
    # Read-only access to the merchant's connected-site workspace: crawl
    # inventory, first-party integration directory, and Lynewright-owned analytics rollups.
    "website:read",
    "mcp:client:use", "mcp:client:configure",
})
RESERVED_CAPABILITIES = frozenset({
    "system:internal", "core-db:read", "core-db:write", "auth:internal",
    "capabilities:internal", "runtime:internal",
})
FULL_ACCESS_CAPABILITIES = PUBLIC_CAPABILITIES
_ALL_CAPABILITIES = PUBLIC_CAPABILITIES | RESERVED_CAPABILITIES


@dataclass(frozen=True, slots=True)
class CapabilityActor:
    client_id: str
    name: str
    client_type: str
    permission_mode: str
    granted_capabilities: frozenset[str]
    enabled: bool = True
    internal: bool = False

    @classmethod
    def platform_manager(cls) -> "CapabilityActor":
        """Internal-only actor used by authenticated platform administration."""
        return cls(
            client_id="internal-platform-manager", name="Platform Manager", client_type="internal",
            permission_mode="internal", granted_capabilities=frozenset({"capabilities:internal"}), internal=True,
        )


# publishing:read did not exist when every publishing operation was declared
# publishing:write, including pure reads. Existing tokens hold only the write
# grant, so a read capability that write does not imply would silently strip
# read access from every caller that has it today. This is deliberately a
# single named pair, not a generic write-implies-read graph: the platform has
# no such rule elsewhere, and inventing one for every capability family at
# once is a bigger change than this one narrow gap calls for.
_CAPABILITY_IMPLICATIONS: dict[str, str] = {"publishing:write": "publishing:read"}


def _with_implied_capabilities(capabilities: frozenset[str]) -> frozenset[str]:
    implied = {
        target for held, target in _CAPABILITY_IMPLICATIONS.items() if held in capabilities
    }
    return capabilities | implied if implied else capabilities


def allowed_capabilities(actor: CapabilityActor) -> frozenset[str]:
    if not actor.enabled:
        return frozenset()
    if actor.internal:
        return _with_implied_capabilities(actor.granted_capabilities)
    if actor.permission_mode == "full":
        return FULL_ACCESS_CAPABILITIES
    if actor.permission_mode in {"restricted", "custom"}:
        return _with_implied_capabilities(actor.granted_capabilities & PUBLIC_CAPABILITIES)
    return frozenset()


def require_capability(actor: CapabilityActor, capability: str) -> None:
    if capability not in _ALL_CAPABILITIES:
        raise PlatformValidationError("Unknown capability.")
    if capability not in allowed_capabilities(actor):
        raise PlatformOperationError("Client is not authorized for this capability.")


class ClientAuthorizationService:
    """Persists external client identities; external clients cannot self-administer."""

    def __init__(self, store: PlatformStore | None = None) -> None:
        self.store = store or PlatformStore()

    def list_clients(self, *, actor: CapabilityActor) -> list[dict[str, Any]]:
        self._require_manager(actor)
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM platform_clients ORDER BY created_at,id").fetchall()
        return [self._public(row) for row in rows]

    def get_client(self, client_id: str, *, actor: CapabilityActor) -> dict[str, Any]:
        self._require_manager(actor)
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM platform_clients WHERE id=?", (client_id,)).fetchone()
        if row is None:
            raise PlatformNotFoundError("Client not found.")
        return self._public(row)

    def create_client(self, value: ClientCreateInput | dict[str, Any], *, actor: CapabilityActor) -> dict[str, Any]:
        self._require_manager(actor)
        payload = value if isinstance(value, ClientCreateInput) else _validated(ClientCreateInput, value)
        grants = self._normalize_grants(payload.permission_mode, payload.granted_capabilities)
        client_id = f"clt_{secrets.token_hex(12)}"
        token = f"qxc_{secrets.token_urlsafe(32)}"
        try:
            with self.store.connection() as db, db:
                db.execute(
                    "INSERT INTO platform_clients (id,name,client_type,permission_mode,granted_capabilities_json,token_hash) VALUES (?,?,?,?,?,?)",
                    (client_id, payload.name, payload.client_type, payload.permission_mode, json.dumps(sorted(grants)), self._token_hash(token)),
                )
        except sqlite3.IntegrityError as exc:
            raise PlatformConflictError("A client with that name already exists.") from exc
        return self.get_client(client_id, actor=actor) | {"access_token": token}

    def update_client(self, client_id: str, value: ClientUpdateInput | dict[str, Any], *, actor: CapabilityActor) -> dict[str, Any]:
        self._require_manager(actor)
        payload = value if isinstance(value, ClientUpdateInput) else _validated(ClientUpdateInput, value)
        with self.store.connection() as db, db:
            row = db.execute("SELECT * FROM platform_clients WHERE id=?", (client_id,)).fetchone()
            if row is None:
                raise PlatformNotFoundError("Client not found.")
            mode = payload.permission_mode or str(row["permission_mode"])
            existing_grants = json.loads(str(row["granted_capabilities_json"] or "[]"))
            grants = self._normalize_grants(mode, payload.granted_capabilities if payload.granted_capabilities is not None else existing_grants)
            fields, params = ["permission_mode=?", "granted_capabilities_json=?"], [mode, json.dumps(sorted(grants))]
            if payload.name is not None:
                fields.append("name=?")
                params.append(payload.name)
            if payload.enabled is not None:
                fields.append("enabled=?")
                params.append(int(payload.enabled))
            try:
                db.execute(f"UPDATE platform_clients SET {', '.join(fields)},updated_at=CURRENT_TIMESTAMP WHERE id=?", (*params, client_id))
            except sqlite3.IntegrityError as exc:
                raise PlatformConflictError("A client with that name already exists.") from exc
        return self.get_client(client_id, actor=actor)

    def authenticate(self, token: str) -> CapabilityActor:
        if not token or not token.startswith("qxc_"):
            raise PlatformOperationError("Client authentication failed.")
        candidate_hash = self._token_hash(token)
        with self.store.connection() as db, db:
            row = db.execute("SELECT * FROM platform_clients WHERE token_hash=?", (candidate_hash,)).fetchone()
            if row is None or not bool(row["enabled"]):
                raise PlatformOperationError("Client authentication failed.")
            db.execute("UPDATE platform_clients SET last_authenticated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        return CapabilityActor(
            client_id=str(row["id"]), name=str(row["name"]), client_type=str(row["client_type"]),
            permission_mode=str(row["permission_mode"]),
            granted_capabilities=frozenset(json.loads(str(row["granted_capabilities_json"] or "[]"))),
        )

    def resolve_actor(self, client_id: str) -> CapabilityActor:
        """Resolve the current persisted policy after transport authentication."""
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM platform_clients WHERE id=?", (client_id,)).fetchone()
        if row is None or not bool(row["enabled"]):
            raise PlatformOperationError("Client authentication failed.")
        return CapabilityActor(
            client_id=str(row["id"]), name=str(row["name"]), client_type=str(row["client_type"]),
            permission_mode=str(row["permission_mode"]),
            granted_capabilities=frozenset(json.loads(str(row["granted_capabilities_json"] or "[]"))),
        )

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_grants(mode: str, values: list[str]) -> frozenset[str]:
        candidate = frozenset(str(item) for item in values)
        unknown = candidate - PUBLIC_CAPABILITIES
        if unknown:
            raise PlatformValidationError("Only known public capabilities may be granted to an external client.")
        if mode == "full":
            if candidate:
                raise PlatformValidationError("Full Access clients do not accept custom capability grants.")
            return frozenset()
        if mode not in {"restricted", "custom"}:
            raise PlatformValidationError("Unsupported permission mode.")
        return candidate

    @staticmethod
    def _require_manager(actor: CapabilityActor) -> None:
        if not actor.internal or "capabilities:internal" not in actor.granted_capabilities:
            raise PlatformOperationError("Client records may only be managed by the platform administrator.")

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]), "name": str(row["name"]), "type": str(row["client_type"]),
            "permission_mode": str(row["permission_mode"]),
            "granted_capabilities": json.loads(str(row["granted_capabilities_json"] or "[]")),
            "enabled": bool(row["enabled"]), "created_at": row["created_at"],
            "updated_at": row["updated_at"], "last_authenticated_at": row["last_authenticated_at"],
        }
