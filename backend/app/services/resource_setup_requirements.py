"""Typed, host-controlled setup requirements for resource bindings."""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any, Literal

from app.services.platform_services import PlatformConflictError, PlatformNotFoundError, PlatformStore, PlatformValidationError, _redact


RequirementType = Literal["web_script", "manual_instruction"]
RequirementState = Literal["PENDING", "AUTO_APPLYING", "MANUAL_REQUIRED", "APPLIED", "VERIFYING", "VERIFIED", "FAILED"]
_TYPES = frozenset({"web_script", "manual_instruction"})
_STATES = frozenset({"PENDING", "AUTO_APPLYING", "MANUAL_REQUIRED", "APPLIED", "VERIFYING", "VERIFIED", "FAILED"})
_MAX_ID = 256
_MAX_ERROR_CODE = 120
_MAX_ERROR_MESSAGE = 2_000


class ResourceSetupRequirementService:
    def __init__(self, store: PlatformStore | None = None, *, tenant_id: str = "ten_default") -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_resource_setup_requirements (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                requirement_type TEXT NOT NULL CHECK (requirement_type IN ('web_script','manual_instruction')),
                requirement_key TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL CHECK (state IN ('PENDING','AUTO_APPLYING','MANUAL_REQUIRED','APPLIED','VERIFYING','VERIFIED','FAILED')),
                revision INTEGER NOT NULL DEFAULT 1,
                instructions_json TEXT NOT NULL DEFAULT '{}',
                verification_json TEXT NOT NULL DEFAULT '{}',
                last_error_code TEXT NOT NULL DEFAULT '',
                last_error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                verified_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_resource_setup_state
              ON platform_resource_setup_requirements(tenant_id,state,updated_at DESC);
            """
        )
        # One binding can need several requirements of the same type -- a
        # measurement spec carries up to eight manual instructions for a single
        # page. Identity therefore includes a caller-supplied key. Existing rows
        # take the empty key, which is exactly the identity they already had, so
        # callers that do not use keys keep their current behaviour.
        columns = {row[1] for row in db.execute("PRAGMA table_info(platform_resource_setup_requirements)")}
        if "requirement_key" not in columns:
            db.execute(
                "ALTER TABLE platform_resource_setup_requirements "
                "ADD COLUMN requirement_key TEXT NOT NULL DEFAULT ''"
            )
        db.executescript(
            """
            DROP INDEX IF EXISTS idx_resource_setup_requirement;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_setup_requirement_key
              ON platform_resource_setup_requirements(tenant_id,binding_id,requirement_type,requirement_key);
            """
        )

    def ensure(
        self,
        *,
        binding_id: str,
        requirement_type: RequirementType,
        requirement_key: str = "",
        instructions: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        state: RequirementState = "PENDING",
    ) -> dict[str, Any]:
        """Create or refresh one requirement. Never moves its state.

        `state` applies to a requirement being created. An existing one keeps
        the state it reached: re-declaring a requirement is not evidence about
        whether it is satisfied, and a caller re-running its declaration must
        not be able to walk a VERIFIED requirement backwards.

        `verification` follows the same principle. Passing None leaves the
        stored evidence untouched; only an explicit dict replaces it. The
        alternative silently erased the evidence behind a VERIFIED state, which
        is worse than either losing the state or keeping stale evidence -- it
        left Core asserting a verification it could no longer show.
        """
        if not binding_id or len(binding_id) > _MAX_ID or requirement_type not in _TYPES or state not in _STATES:
            raise PlatformValidationError("Setup requirement is invalid.")
        if len(requirement_key) > _MAX_ID:
            raise PlatformValidationError("Setup requirement key is invalid.")
        instructions_json = self._safe_json(instructions)
        with self._connection() as db, db:
            existing = db.execute(
                "SELECT * FROM platform_resource_setup_requirements"
                " WHERE tenant_id=? AND binding_id=? AND requirement_type=? AND requirement_key=?",
                (self.tenant_id, binding_id, requirement_type, requirement_key),
            ).fetchone()
            if existing is None:
                requirement_id = f"req_{secrets.token_hex(12)}"
                db.execute(
                    """INSERT INTO platform_resource_setup_requirements
                    (id,tenant_id,binding_id,requirement_type,requirement_key,state,instructions_json,verification_json)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (requirement_id, self.tenant_id, binding_id, requirement_type, requirement_key,
                     state, instructions_json, self._safe_json(verification)),
                )
            else:
                requirement_id = str(existing["id"])
                assignments = ["instructions_json=?", "updated_at=CURRENT_TIMESTAMP"]
                params: list[Any] = [instructions_json]
                if verification is not None:
                    assignments.insert(1, "verification_json=?")
                    params.append(self._safe_json(verification))
                params.extend([self.tenant_id, requirement_id])
                db.execute(
                    "UPDATE platform_resource_setup_requirements"
                    f" SET {', '.join(assignments)} WHERE tenant_id=? AND id=?",
                    params,
                )
        return self.get(requirement_id)

    def transition(
        self,
        requirement_id: str,
        *,
        state: RequirementState,
        expected_revision: int,
        error_code: str = "",
        error_message: str = "",
        verification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not requirement_id or len(requirement_id) > _MAX_ID or state not in _STATES or expected_revision < 1:
            raise PlatformValidationError("Setup requirement transition is invalid.")
        if len(error_code) > _MAX_ERROR_CODE or len(error_message) > _MAX_ERROR_MESSAGE:
            raise PlatformValidationError("Setup requirement transition fields exceed their bounds.")
        assignments = ["state=?", "revision=revision+1", "last_error_code=?", "last_error_message=?", "verified_at=CASE WHEN ?='VERIFIED' THEN CURRENT_TIMESTAMP ELSE verified_at END", "updated_at=CURRENT_TIMESTAMP"]
        params: list[Any] = [state, error_code, error_message, state]
        if verification is not None:
            assignments.append("verification_json=?")
            params.append(self._safe_json(verification))
        params.extend([self.tenant_id, requirement_id, expected_revision])
        with self._connection() as db, db:
            changed = db.execute(
                f"UPDATE platform_resource_setup_requirements SET {', '.join(assignments)} WHERE tenant_id=? AND id=? AND revision=?",
                params,
            ).rowcount
        if not changed:
            raise PlatformConflictError("Setup requirement revision is stale or requirement is missing.")
        return self.get(requirement_id)

    def list(self, *, binding_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if not binding_id or len(binding_id) > _MAX_ID or limit < 1 or limit > 200:
            raise PlatformValidationError("Setup requirement list bounds are invalid.")
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM platform_resource_setup_requirements WHERE tenant_id=? AND binding_id=? ORDER BY id LIMIT ?",
                (self.tenant_id, binding_id, limit),
            ).fetchall()
        return [self._public(row) for row in rows]

    def get(self, requirement_id: str) -> dict[str, Any]:
        if not requirement_id or len(requirement_id) > _MAX_ID:
            raise PlatformValidationError("Setup requirement id is invalid.")
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM platform_resource_setup_requirements WHERE tenant_id=? AND id=?",
                (self.tenant_id, requirement_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Setup requirement was not found.")
        return self._public(row)

    @staticmethod
    def _safe_json(value: dict[str, Any] | None) -> str:
        if value is not None and not isinstance(value, dict):
            raise PlatformValidationError("Setup requirement data must be an object.")
        try:
            encoded = json.dumps(_redact(value or {}), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise PlatformValidationError("Setup requirement data must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise PlatformValidationError("Setup requirement data exceeds the 64 KiB limit.")
        return encoded

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for source, target in (("instructions_json", "instructions"), ("verification_json", "verification")):
            try:
                decoded = json.loads(result.pop(source) or "{}")
                result[target] = decoded if isinstance(decoded, dict) else {}
            except json.JSONDecodeError:
                result[target] = {}
        return result


__all__ = ["RequirementState", "RequirementType", "ResourceSetupRequirementService"]
