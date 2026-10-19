"""Durable evidence, rather than declarations, for integration capabilities."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from collections.abc import Callable
from typing import Any, Iterator, Literal

from app.services.integration_errors import IntegrationErrorCategory, integration_error
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import PlatformOperationError, PlatformValidationError


CapabilityEvidenceState = Literal["DECLARED", "DISCOVERED", "VERIFIED", "DEGRADED", "UNAVAILABLE"]
CAPABILITY_EVIDENCE_STATES = frozenset({"DECLARED", "DISCOVERED", "VERIFIED", "DEGRADED", "UNAVAILABLE"})
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,159}$")


class IntegrationCapabilityEvidenceService:
    """Records proof of a provider feature against a specific definition identity."""

    def __init__(self, registry: IntegrationRegistry, *, tenant_id: str | None = None, now: Callable[[], float] | None = None) -> None:
        self.registry = registry
        self.store = registry.store
        self.tenant_id = tenant_id or registry.tenant_id
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
            CREATE TABLE IF NOT EXISTS platform_integration_capability_evidence (
                tenant_id TEXT NOT NULL,
                integration_id TEXT NOT NULL,
                contract TEXT NOT NULL,
                capability TEXT NOT NULL,
                state TEXT NOT NULL,
                definition_version TEXT NOT NULL,
                definition_fingerprint TEXT NOT NULL,
                verified_at_epoch REAL,
                reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id,integration_id,contract,capability)
            );
            CREATE INDEX IF NOT EXISTS idx_integration_capability_evidence_state
                ON platform_integration_capability_evidence(tenant_id,integration_id,state);
            """
        )

    def declare(self, *, integration_id: str, contract: str, capability: str) -> dict[str, Any]:
        """Store the provider declaration and invalidate stale proof on identity change."""
        self._validate(contract, capability)
        version, fingerprint = self._definition_identity(integration_id)
        with self._connection() as db, db:
            row = self._row(db, integration_id, contract, capability)
            if row is None:
                db.execute(
                    """INSERT INTO platform_integration_capability_evidence
                    (tenant_id,integration_id,contract,capability,state,definition_version,definition_fingerprint)
                    VALUES (?,?,?,?,?,?,?)""",
                    (self.tenant_id, integration_id, contract, capability, "DECLARED", version, fingerprint),
                )
            elif row["definition_version"] != version or row["definition_fingerprint"] != fingerprint:
                db.execute(
                    """UPDATE platform_integration_capability_evidence
                    SET state='DECLARED',definition_version=?,definition_fingerprint=?,verified_at_epoch=NULL,
                        reason='definition_identity_changed',updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=? AND integration_id=? AND contract=? AND capability=?""",
                    (version, fingerprint, self.tenant_id, integration_id, contract, capability),
                )
        return self.get(integration_id=integration_id, contract=contract, capability=capability)

    def set_state(
        self,
        *,
        integration_id: str,
        contract: str,
        capability: str,
        state: CapabilityEvidenceState,
        reason: str = "",
    ) -> dict[str, Any]:
        if state not in CAPABILITY_EVIDENCE_STATES:
            raise PlatformValidationError("Unsupported capability evidence state.")
        current = self.declare(integration_id=integration_id, contract=contract, capability=capability)
        verified_at = self.now() if state == "VERIFIED" else (None if state in {"DECLARED", "DISCOVERED"} else current["verified_at_epoch"])
        with self._connection() as db, db:
            db.execute(
                """UPDATE platform_integration_capability_evidence
                SET state=?,verified_at_epoch=?,reason=?,updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=? AND integration_id=? AND contract=? AND capability=?""",
                (state, verified_at, reason[:500], self.tenant_id, integration_id, contract, capability),
            )
        return self.get(integration_id=integration_id, contract=contract, capability=capability)

    def get(self, *, integration_id: str, contract: str, capability: str) -> dict[str, Any]:
        self._validate(contract, capability)
        with self._connection() as db:
            row = self._row(db, integration_id, contract, capability)
        if row is None:
            raise PlatformOperationError("Provider capability has not been declared.")
        return self._public(row)

    def list(self, *, integration_id: str, contract: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                """SELECT * FROM platform_integration_capability_evidence
                WHERE tenant_id=? AND integration_id=? AND (? IS NULL OR contract=?)
                ORDER BY contract,capability""",
                (self.tenant_id, integration_id, contract, contract),
            ).fetchall()
        return [self._public(row) for row in rows]

    def require_usable(self, *, integration_id: str, contract: str, capability: str, require_verified: bool = False) -> dict[str, Any]:
        evidence = self.get(integration_id=integration_id, contract=contract, capability=capability)
        if evidence["state"] in {"DEGRADED", "UNAVAILABLE"} or (require_verified and evidence["state"] != "VERIFIED"):
            raise integration_error(
                IntegrationErrorCategory.PROVIDER_UNAVAILABLE,
                diagnostic={"provider": integration_id, "contract": contract, "capability": capability, "state": evidence["state"]},
            )
        return evidence

    def _definition_identity(self, integration_id: str) -> tuple[str, str]:
        integration = self.registry.get(integration_id)
        manifest = integration["manifest"]
        fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return str(integration["version"]), fingerprint

    def _row(self, db: sqlite3.Connection, integration_id: str, contract: str, capability: str) -> sqlite3.Row | None:
        return db.execute(
            """SELECT * FROM platform_integration_capability_evidence
            WHERE tenant_id=? AND integration_id=? AND contract=? AND capability=?""",
            (self.tenant_id, integration_id, contract, capability),
        ).fetchone()

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["verified"] = value["state"] == "VERIFIED"
        return value

    @staticmethod
    def _validate(contract: str, capability: str) -> None:
        if not contract.startswith("lynewright.") or _NAME.fullmatch(capability) is None:
            raise PlatformValidationError("Capability evidence requires a Lynewright contract and bounded capability name.")
