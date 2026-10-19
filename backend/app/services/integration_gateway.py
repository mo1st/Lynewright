"""Verified inbound webhook and callback dispatch for generic integrations."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Literal

from app.services.integration_connections import ConnectionProfileService
from app.services.integration_contributions import Contribution, ContributionRegistry
from app.services.platform_services import CredentialService, PlatformNotFoundError, PlatformOperationError, PlatformStore, PlatformValidationError


InboundKind = Literal["webhook", "callback"]


@dataclass(frozen=True, slots=True)
class InboundIntegrationMessage:
    kind: InboundKind
    integration_id: str
    connection_id: str
    handler_id: str
    delivery_id: str
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlatformValidationError("Inbound integration payload is not valid JSON.") from exc


class IntegrationGateway:
    """Routes verified, deduplicated inbound messages to owned contributions."""

    def __init__(self, store: PlatformStore, connections: ConnectionProfileService, contributions: ContributionRegistry, *, now: Callable[[], float] | None = None) -> None:
        self.store = store
        self.connections = connections
        self.contributions = contributions
        self.credentials = CredentialService(store)
        self.now = now or time.time

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_integration_inbound_deliveries (
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                handler_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                delivery_hash TEXT NOT NULL,
                received_at_epoch INTEGER NOT NULL,
                PRIMARY KEY (tenant_id,integration_id,connection_id,handler_id,kind,delivery_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_integration_inbound_deliveries_received
                ON platform_integration_inbound_deliveries(received_at_epoch);
            """
        )

    def receive(self, kind: InboundKind, *, integration_id: str, connection_id: str, handler_id: str, body: bytes, headers: dict[str, str] | None = None) -> dict[str, Any]:
        if kind not in {"webhook", "callback"}:
            raise PlatformValidationError("Unsupported inbound integration message kind.")
        if len(body) > 1_000_000:
            raise PlatformValidationError("Inbound integration payload exceeds the 1 MiB limit.")
        connection = self.connections.get(integration_id, connection_id)
        if not connection["enabled"]:
            raise PlatformOperationError("Integration connection is disabled.")
        try:
            contribution = self.contributions.get(f"{integration_id}:{handler_id}")
        except PlatformNotFoundError:
            return self._receive_manual(
                kind=kind,
                integration_id=integration_id,
                connection_id=connection_id,
                handler_id=handler_id,
                body=body,
                headers=headers,
                connection=connection,
            )
        if contribution.kind != kind:
            raise PlatformOperationError("Integration handler does not accept this inbound message kind.")
        normalized_headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        self._verify(contribution, body, normalized_headers)
        delivery_id = self._delivery_id(contribution, body, normalized_headers)
        if not self._reserve_delivery(kind, integration_id, connection_id, handler_id, delivery_id):
            return {"accepted": True, "duplicate": True, "delivery_id": delivery_id}
        message = InboundIntegrationMessage(kind, integration_id, connection_id, handler_id, delivery_id, body, normalized_headers)
        try:
            self.contributions.invoke(contribution.id, message)
        except Exception as exc:
            raise PlatformOperationError("Integration inbound handler failed.") from exc
        return {"accepted": True, "duplicate": False, "delivery_id": delivery_id}

    def _receive_manual(
        self,
        *,
        kind: InboundKind,
        integration_id: str,
        connection_id: str,
        handler_id: str,
        body: bytes,
        headers: dict[str, str] | None,
        connection: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch a data-only manual webhook through host-owned events."""
        if kind != "webhook":
            raise PlatformNotFoundError("Integration handler not found.")
        integration = self.connections.registry.get(integration_id)
        manual = (integration.get("configuration") or {}).get("manual")
        if not isinstance(manual, dict) or manual.get("runtime") != "webhook":
            raise PlatformNotFoundError("Integration handler not found.")
        configuration = connection.get("configuration") if isinstance(connection.get("configuration"), dict) else {}
        webhook = configuration.get("webhook") if isinstance(configuration, dict) else None
        if not isinstance(webhook, dict) or str(webhook.get("handler_id") or "") != handler_id:
            raise PlatformNotFoundError("Integration handler not found.")
        if not integration.get("enabled"):
            raise PlatformOperationError("Integration is disabled.")
        normalized_headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        settings = webhook.get("verification")
        self._verify_settings(settings, body, normalized_headers)
        delivery_id = self._delivery_id_settings(settings, body, normalized_headers)
        if not self._reserve_delivery(kind, integration_id, connection_id, handler_id, delivery_id):
            return {"accepted": True, "duplicate": True, "delivery_id": delivery_id}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlatformValidationError("Inbound integration payload is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise PlatformValidationError("Manual webhook payloads must be JSON objects.")
        mapped = self._map_payload(payload, webhook.get("payload_mapping"))
        event_type = str(webhook.get("event_type") or "")
        dispatched = self.contributions.dispatch_events(event_type, mapped)
        return {
            "accepted": True,
            "duplicate": False,
            "delivery_id": delivery_id,
            "delivered": dispatched["delivered"],
            "failures": dispatched["failures"],
        }

    def receive_webhook(self, **kwargs: Any) -> dict[str, Any]:
        return self.receive("webhook", **kwargs)

    def receive_callback(self, **kwargs: Any) -> dict[str, Any]:
        return self.receive("callback", **kwargs)

    def _verify(self, contribution: Contribution, body: bytes, headers: dict[str, str]) -> None:
        settings = contribution.metadata.get("verification")
        self._verify_settings(settings, body, headers)

    def _verify_settings(self, settings: Any, body: bytes, headers: dict[str, str]) -> None:
        if not isinstance(settings, dict) or "mode" not in settings:
            raise PlatformOperationError("Inbound integration handler does not declare verification settings.")
        mode = str(settings.get("mode"))
        if mode == "none":
            return
        credential_ref = str(settings.get("credential_ref") or "")
        header = str(settings.get("signature_header") or "x-signature").lower()
        signature = headers.get(header, "")
        if not credential_ref or not signature:
            raise PlatformOperationError("Inbound integration signature is missing.")
        timestamp_header = str(settings.get("timestamp_header") or "").lower()
        signed = body
        if timestamp_header:
            timestamp = headers.get(timestamp_header, "")
            try:
                timestamp_value = int(timestamp)
            except ValueError as exc:
                raise PlatformOperationError("Inbound integration timestamp is invalid.") from exc
            tolerance = int(settings.get("tolerance_seconds") or 300)
            if tolerance < 1 or abs(int(self.now()) - timestamp_value) > tolerance:
                raise PlatformOperationError("Inbound integration timestamp is outside the replay window.")
            signed = f"{timestamp}.".encode("ascii") + body

        def verify(secret: str) -> bool:
            if mode == "shared_secret":
                return hmac.compare_digest(signature, secret)
            if mode == "hmac_sha256":
                expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
                return hmac.compare_digest(signature.removeprefix("sha256="), expected)
            raise PlatformValidationError("Unsupported inbound integration verification mode.")

        if not self.credentials.use(credential_ref, verify):
            raise PlatformOperationError("Inbound integration signature is invalid.")

    @staticmethod
    def _delivery_id(contribution: Contribution, body: bytes, headers: dict[str, str]) -> str:
        settings = contribution.metadata.get("verification")
        return IntegrationGateway._delivery_id_settings(settings, body, headers)

    @staticmethod
    def _delivery_id_settings(settings: Any, body: bytes, headers: dict[str, str]) -> str:
        if not isinstance(settings, dict):
            raise PlatformOperationError("Inbound integration handler does not declare verification settings.")
        delivery_header = str(settings.get("delivery_header") or "").lower()
        candidate = headers.get(delivery_header, "") if delivery_header else ""
        return candidate or hashlib.sha256(body).hexdigest()

    @staticmethod
    def _map_payload(payload: dict[str, Any], mapping: Any) -> dict[str, Any]:
        if not mapping:
            return payload
        if not isinstance(mapping, dict):
            raise PlatformValidationError("Manual webhook payload mapping is invalid.")
        result: dict[str, Any] = {}
        for target, source in mapping.items():
            current: Any = payload
            for part in str(source).split("."):
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(part)
            result[str(target)] = current
        return result

    def _reserve_delivery(self, kind: InboundKind, integration_id: str, connection_id: str, handler_id: str, delivery_id: str) -> bool:
        digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        try:
            with self._connection() as db, db:
                db.execute(
                    """INSERT INTO platform_integration_inbound_deliveries
                    (tenant_id,integration_id,connection_id,handler_id,kind,delivery_hash,received_at_epoch)
                    VALUES (?,?,?,?,?,?,?)""",
                    (self.connections.tenant_id, integration_id, connection_id, handler_id, kind, digest, int(self.now())),
                )
        except sqlite3.IntegrityError:
            return False
        return True
