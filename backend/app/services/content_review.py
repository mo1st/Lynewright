"""Content Integrity / Accuracy / Persuasion gate and the server-side render block.

Plan v5.3 sections 9-11 and 16. Verdicts are bound to an exact script version;
any script edit makes prior verdicts stale; rendering requires all three
verdicts APPROVED plus human VERIFIED review evidence for the current version.
AI pre-checks are recorded through the shared capability-evidence discipline and
can never satisfy the render gate.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from app.services.integration_capability_evidence import IntegrationCapabilityEvidenceService
from app.services.integration_registry import IntegrationRegistry
from app.services.platform_services import (
    PlatformNotFoundError,
    PlatformOperationError,
    PlatformStore,
    register_job_creation_gate,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID


RENDER_JOB_TYPE = "video.render"

CONTENT_REVIEW_CONTRACT = "lynewright.content.review"
CONTENT_REVIEW_CONTRACT_STATUS = "PROVISIONAL"
CONTENT_REVIEW_INTEGRATION_ID = "lynewright.integration.content-review"
CONTENT_REVIEW_VERSION = "1.0.0"

GATE_NAMES = ("integrity", "accuracy", "persuasion")
VERDICT_STATUSES = frozenset({"APPROVED", "REJECTED"})
REVIEWER_TYPES = frozenset({"human", "ai_assisted_human"})

REJECTION_TAXONOMY = frozenset({
    "CLAIM_THERAPEUTIC",
    "CLAIM_GUARANTEE",
    "CLAIM_TIMEFRAME",
    "PROOF_FABRICATED",
    "PROOF_UNSOURCED",
    "ACCURACY_INGREDIENT",
    "ACCURACY_CONCENTRATION",
    "ACCURACY_USAGE",
    "ACCURACY_INTERACTION",
    "URGENCY_FALSE",
    "COMPETITOR_FALSE",
    "WEAK_HOOK",
    "NO_MECHANISM",
    "NO_SPECIFIC",
    "NO_LIMITATION",
    "MULTIPLE_CTA",
    "SOURCE_MISSING",
})


class ContentReviewService:
    """Owns content records, per-version review verdicts, and render authorization."""

    CONTRACT = CONTENT_REVIEW_CONTRACT
    CAPABILITY = "render_authorization"

    def __init__(
        self,
        store: PlatformStore | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        registry: IntegrationRegistry | None = None,
        evidence: IntegrationCapabilityEvidenceService | None = None,
    ) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id
        self.registry = registry or IntegrationRegistry(self.store)
        self._ensure_reviewer_definition()
        self.evidence = evidence or IntegrationCapabilityEvidenceService(self.registry, tenant_id=tenant_id)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS content_items (
                tenant_id TEXT NOT NULL,
                content_id TEXT NOT NULL,
                topic_id TEXT NOT NULL DEFAULT '',
                context_json TEXT NOT NULL DEFAULT '{}',
                market TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                register TEXT NOT NULL DEFAULT '',
                target_platform TEXT NOT NULL DEFAULT '',
                current_script_version INTEGER NOT NULL DEFAULT 1,
                synthetic_content INTEGER NOT NULL DEFAULT 0,
                synthetic_voice INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, content_id)
            );
            CREATE TABLE IF NOT EXISTS content_scripts (
                tenant_id TEXT NOT NULL,
                content_id TEXT NOT NULL,
                script_version INTEGER NOT NULL,
                hook TEXT NOT NULL DEFAULT '',
                script TEXT NOT NULL,
                sources_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, content_id, script_version)
            );
            CREATE TABLE IF NOT EXISTS content_reviews (
                tenant_id TEXT NOT NULL,
                content_id TEXT NOT NULL,
                script_version INTEGER NOT NULL,
                gate TEXT NOT NULL,
                status TEXT NOT NULL,
                reviewer_id TEXT NOT NULL DEFAULT '',
                reviewer_type TEXT NOT NULL DEFAULT 'human',
                rejection_reasons_json TEXT NOT NULL DEFAULT '[]',
                notes TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, content_id, script_version, gate)
            );
            CREATE TABLE IF NOT EXISTS content_prechecks (
                tenant_id TEXT NOT NULL,
                precheck_id TEXT NOT NULL,
                content_id TEXT NOT NULL,
                script_version INTEGER NOT NULL,
                findings_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, precheck_id)
            );
            """
        )
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(content_items)").fetchall()}
        if "context_json" not in columns:
            db.execute("ALTER TABLE content_items ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}' ")

    def _ensure_reviewer_definition(self) -> None:
        known = {str(item["id"]) for item in self.registry.list()}
        if CONTENT_REVIEW_INTEGRATION_ID not in known:
            self.registry.register({
                "schema_version": "1.0", "id": CONTENT_REVIEW_INTEGRATION_ID,
                "name": "Lynewright content review authority", "version": "1.0.0",
                "type": "native", "lynewright_version": "^0.1.0", "sdk_version": "^1.0.0",
                "backend": "dist/backend.mjs", "requested_capabilities": [],
            })
        try:
            self.registry.enable(CONTENT_REVIEW_INTEGRATION_ID)
        except PlatformOperationError:
            pass

    # ------------------------------------------------------------------ content

    def create_content(self, payload: dict[str, Any]) -> dict[str, Any]:
        script = str(payload.get("script") or "").strip()
        if not script:
            raise PlatformOperationError("Content requires a script.")
        sources = payload.get("sources") or []
        if not isinstance(sources, list):
            raise PlatformOperationError("Content sources must be a list.")
        # Optional localization hints remain metadata; validation belongs to
        # an integration that contributes its own rules.
        market = str(payload.get("market") or "").strip().upper()
        language = str(payload.get("language") or "").strip().lower()
        register = str(payload.get("register") or "").strip().lower()
        content_id = f"cnt_{secrets.token_hex(8)}"
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO content_items
                (tenant_id,content_id,topic_id,context_json,
                 market,language,register,target_platform)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    self.tenant_id, content_id,
                    str(payload.get("topic_id") or ""),
                    json.dumps(payload.get("context") if isinstance(payload.get("context"), dict) else {}),
                    market, language, register, str(payload.get("target_platform") or ""),
                ),
            )
            db.execute(
                """INSERT INTO content_scripts (tenant_id,content_id,script_version,hook,script,sources_json)
                VALUES (?,?,?,?,?,?)""",
                (
                    self.tenant_id, content_id, 1,
                    str(payload.get("hook") or ""), script, json.dumps(sources),
                ),
            )
        return {"content_id": content_id, "script_version": 1}

    def update_script(self, content_id: str, *, hook: str = "", script: str, sources: list[Any]) -> dict[str, Any]:
        script = str(script or "").strip()
        if not script:
            raise PlatformOperationError("A script revision requires a non-empty script.")
        if not isinstance(sources, list):
            raise PlatformOperationError("Content sources must be a list.")
        content = self.get_content(content_id)
        next_version = int(content["current_script_version"]) + 1
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO content_scripts (tenant_id,content_id,script_version,hook,script,sources_json)
                VALUES (?,?,?,?,?,?)""",
                (self.tenant_id, content_id, next_version, str(hook), script, json.dumps(sources)),
            )
            db.execute(
                """UPDATE content_items SET current_script_version=?,updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=? AND content_id=?""",
                (next_version, self.tenant_id, content_id),
            )
        return {"content_id": content_id, "script_version": next_version}

    def get_content(self, content_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM content_items WHERE tenant_id=? AND content_id=?",
                (self.tenant_id, content_id),
            ).fetchone()
            scripts = db.execute(
                """SELECT * FROM content_scripts WHERE tenant_id=? AND content_id=?
                ORDER BY script_version DESC""",
                (self.tenant_id, content_id),
            ).fetchall()
        if row is None:
            raise PlatformNotFoundError("Content record was not found.")
        result = dict(row)
        result["synthetic_content"] = bool(result["synthetic_content"])
        result["synthetic_voice"] = bool(result["synthetic_voice"])
        current = next((item for item in scripts if item["script_version"] == row["current_script_version"]), None)
        result["hook"] = str(current["hook"]) if current else ""
        result["script"] = str(current["script"]) if current else ""
        result["sources"] = json.loads(current["sources_json"]) if current else []
        return result

    # ------------------------------------------------------------------ reviews

    def record_human_verdict(
        self,
        content_id: str,
        *,
        gate: str,
        status: str,
        reviewer_id: str,
        reviewer_type: str = "human",
        rejection_reasons: tuple[str, ...] | list[str] = (),
        notes: str = "",
    ) -> dict[str, Any]:
        if gate not in GATE_NAMES:
            raise PlatformOperationError(f"Unknown review gate: {gate}.")
        if status not in VERDICT_STATUSES:
            raise PlatformOperationError(f"Unsupported review status: {status}.")
        if reviewer_type not in REVIEWER_TYPES:
            raise PlatformOperationError(f"Unsupported reviewer type: {reviewer_type}.")
        reasons = [str(reason) for reason in rejection_reasons]
        if status == "REJECTED":
            if not reasons:
                raise PlatformOperationError("A rejected verdict requires at least one taxonomy reason code.")
            unknown = [reason for reason in reasons if reason not in REJECTION_TAXONOMY]
            if unknown:
                raise PlatformOperationError(f"Rejection reasons outside the fixed taxonomy: {unknown}.")
        elif reasons:
            raise PlatformOperationError("An approved verdict cannot carry rejection reasons.")
        content = self.get_content(content_id)
        version = int(content["current_script_version"])
        with self._connection() as db, db:
            db.execute(
                """INSERT OR REPLACE INTO content_reviews
                (tenant_id,content_id,script_version,gate,status,reviewer_id,reviewer_type,
                 rejection_reasons_json,notes,reviewed_at)
                VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    self.tenant_id, content_id, version, gate, status,
                    str(reviewer_id), reviewer_type, json.dumps(reasons), str(notes)[:2_000],
                ),
            )
        self._sync_review_evidence(content_id, version)
        return {
            "content_id": content_id, "script_version": version, "gate": gate,
            "status": status, "reviewed_by_type": reviewer_type,
            "rejection_reasons": reasons, "reviewer_id": str(reviewer_id),
        }

    def record_ai_precheck(
        self,
        content_id: str,
        *,
        detected_risks: tuple[str, ...] | list[str] = (),
        suggested_sources: tuple[Any, ...] | list[Any] = (),
    ) -> dict[str, Any]:
        content = self.get_content(content_id)
        version = int(content["current_script_version"])
        precheck_id = f"pre_{secrets.token_hex(8)}"
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO content_prechecks (tenant_id,precheck_id,content_id,script_version,findings_json)
                VALUES (?,?,?,?,?)""",
                (
                    self.tenant_id, precheck_id, content_id, version,
                    json.dumps({"detected_risks": [str(r) for r in detected_risks], "suggested_sources": list(suggested_sources)}),
                ),
            )
        # AI assistance is recorded as declared evidence only; it can never
        # promote itself to DISCOVERED-as-authority or VERIFIED.
        state = self.evidence.set_state(
            integration_id=CONTENT_REVIEW_INTEGRATION_ID,
            contract=self.CONTRACT,
            capability=self.CAPABILITY,
            state="DECLARED",
            reason="ai_precheck_recorded_non_authoritative_findings",
        )
        return {"precheck_id": precheck_id, "script_version": version, "authoritative": False, "evidence": state}

    def _sync_review_evidence(self, content_id: str, version: int) -> None:
        with self._connection() as db:
            rows = db.execute(
                """SELECT gate,status FROM content_reviews
                WHERE tenant_id=? AND content_id=? AND script_version=?""",
                (self.tenant_id, content_id, version),
            ).fetchall()
        statuses = {str(row["gate"]): str(row["status"]) for row in rows}
        complete = all(statuses.get(gate) == "APPROVED" for gate in GATE_NAMES)
        any_rejected = any(statuses.get(gate) == "REJECTED" for gate in GATE_NAMES)
        if complete and not any_rejected:
            self.evidence.set_state(
                integration_id=CONTENT_REVIEW_INTEGRATION_ID,
                contract=self.CONTRACT,
                capability=self.CAPABILITY,
                state="VERIFIED",
                reason="human_review_complete_for_current_script_version",
            )

    # ------------------------------------------------------------------- gates

    def gate_record(self, content_id: str) -> dict[str, Any]:
        content = self.get_content(content_id)
        version = int(content["current_script_version"])
        with self._connection() as db:
            rows = db.execute(
                """SELECT * FROM content_reviews WHERE tenant_id=? AND content_id=?
                ORDER BY reviewed_at""",
                (self.tenant_id, content_id),
            ).fetchall()
        current_rows = {str(row["gate"]): dict(row) for row in rows if row["script_version"] == version}
        older_gates = {str(row["gate"]) for row in rows if row["script_version"] != version}

        verdicts: list[dict[str, Any]] = []
        resolved: dict[str, str] = {}
        for gate in GATE_NAMES:
            row = current_rows.get(gate)
            if row is not None:
                status = str(row["status"])
            elif gate in older_gates:
                status = "STALE"
            else:
                status = "PENDING"
            resolved[gate] = status
            if row is not None:
                verdicts.append({
                    "gate": gate, "status": status, "script_version": int(row["script_version"]),
                    "reviewed_by_type": str(row["reviewer_type"]), "reviewer_id": str(row["reviewer_id"]),
                    "rejection_reasons": json.loads(row["rejection_reasons_json"]),
                    "notes": str(row["notes"]), "reviewed_at": str(row["reviewed_at"]),
                })
        try:
            # Eager identity refresh: if the review-authority definition
            # changed since verification, prior proof is invalidated here.
            refreshed = self.evidence.declare(
                integration_id=CONTENT_REVIEW_INTEGRATION_ID,
                contract=self.CONTRACT,
                capability=self.CAPABILITY,
            )
            evidence_state = str(refreshed["state"])
        except Exception:
            evidence_state = "UNDECLARED"
        latest = verdicts[-1] if verdicts else {}
        return {
            "content_id": content_id,
            "script_version": version,
            "integrity_status": resolved["integrity"],
            "accuracy_status": resolved["accuracy"],
            "persuasion_status": resolved["persuasion"],
            "verdicts": verdicts,
            "review_evidence_state": evidence_state,
            "reviewed_by_type": str(latest.get("reviewed_by_type") or ""),
            "rejection_reasons": [reason for v in verdicts for reason in v["rejection_reasons"]],
        }

    def authorize_render(self, content_id: str, *, script_version: int) -> dict[str, Any]:
        content = self.get_content(content_id)
        requested = int(script_version)
        current = int(content["current_script_version"])
        if requested != current:
            raise PlatformOperationError(
                f"render blocked: reviewed script version {requested} differs from current version {current}."
            )
        record = self.gate_record(content_id)
        unmet = [gate for gate in GATE_NAMES if record[f"{gate}_status"] != "APPROVED"]
        if unmet:
            raise PlatformOperationError(
                f"render blocked: content gate verdicts are not APPROVED for version {requested}: "
                f"{', '.join(f'{gate}={record[gate + '_status']}' for gate in unmet)}."
            )
        self.evidence.require_usable(
            integration_id=CONTENT_REVIEW_INTEGRATION_ID,
            contract=self.CONTRACT,
            capability=self.CAPABILITY,
            require_verified=True,
        )
        return {
            "content_id": content_id, "script_version": requested,
            "authorized": True, "review_evidence_state": "VERIFIED",
        }


def install_content_render_gate(store: PlatformStore | None = None, *, tenant_id: str = DEFAULT_TENANT_ID) -> None:
    """Enforce the content gate inside JobService.create for render jobs.

    Once installed, every job creation path - REST, MCP, CLI, or direct
    service call - refuses render jobs whose exact script version lacks three
    human-approved verdicts plus VERIFIED review evidence.
    """

    def _gate(job_type: str, payload: dict[str, Any]) -> None:
        if job_type != RENDER_JOB_TYPE:
            return
        data = payload or {}
        content_id = str(data.get("content_id") or "")
        try:
            version = int(data.get("script_version") or 0)
        except (TypeError, ValueError):
            version = 0
        if not content_id or version <= 0:
            raise PlatformOperationError(
                "render blocked: job payload must carry content_id and script_version."
            )
        ContentReviewService(store, tenant_id=tenant_id).authorize_render(content_id, script_version=version)

    register_job_creation_gate(_gate)
