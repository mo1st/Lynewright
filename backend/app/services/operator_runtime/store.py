"""Durable storage for operator profiles, runs, steps, and approvals.

Deterministic transitions: every status change goes through
``transition_run``, which validates the edge against ``RUN_TRANSITIONS`` and
performs a compare-and-swap on ``revision`` so two concurrent executors cannot
double-apply a step. All reads/writes are tenant-scoped.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from typing import Any

from app.services.operator_runtime.schema import RUN_STATES, TERMINAL_RUN_STATES

RUN_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelled", "failed"},
    "running": {
        "running", "waiting_tool", "waiting_job", "waiting_approval",
        "sleeping", "completed", "failed", "cancelled",
    },
    "waiting_tool": {"running", "cancelled"},
    "waiting_job": {"running", "cancelled"},
    # waiting_approval also admits `failed`: resolving the approval with
    # "denied" is itself a terminal human verdict on the run.
    "waiting_approval": {"running", "failed", "cancelled"},
    "sleeping": {"running", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}

STEP_TYPES = frozenset({
    "model_decision", "tool_call", "wait", "resume", "failure", "finish",
    "approval_request", "action_plan",
})

MAX_STEPS_BOUND = (1, 100)
MAX_TOOL_FAILURES_BOUND = (1, 20)
MAX_MODEL_FAILURES_BOUND = (1, 10)
MAX_TOTAL_TOKENS_BOUND = (0, 200_000)
MAX_MODEL_DECISIONS_BOUND = (0, 100)
MAX_NETWORK_ATTEMPTS_BOUND = (0, 200)
NO_PROGRESS_LIMIT_BOUND = (1, 10)
RETRIEVAL_MODES = frozenset({"lexical", "hybrid_local"})
RETRIEVAL_CANARY_PERCENT_BOUND = (0, 100)
EXECUTOR_LEASE_SECONDS_BOUND = (15, 900)

_TEXT_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|token|secret|password|authorization|bearer)\s*[:=]\s*[^\s,;\"']+"
)
_KEY_LIKE = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b")
_GEMINI_KEY_LIKE = re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")
_SENSITIVE_JSON_KEY = re.compile(
    r"(secret|token|password|api[_-]?key|credential|authorization)", re.IGNORECASE
)


class OperatorError(Exception):
    """Stable base error for the operator runtime."""

    code = "operator_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class OperatorValidationError(OperatorError):
    code = "operator_invalid_input"


class OperatorNotFound(OperatorError):
    code = "operator_not_found"


class OperatorConflict(OperatorError):
    code = "operator_conflict"


class OperatorInvalidTransition(OperatorConflict):
    code = "operator_invalid_transition"


class OperatorLeaseUnavailable(OperatorConflict):
    """Another live executor owns the run's fenced decision lease."""

    code = "operator_executor_busy"


class OperatorLeaseLost(OperatorConflict):
    """An executor tried to apply a result after losing its lease."""

    code = "operator_executor_lease_lost"


# ----------------------------------------------------------------------
# Redaction / bounding — no secret value may reach a step row or model prompt
# ----------------------------------------------------------------------

def redact_text(value: Any, *, limit: int = 4000) -> str:
    text = str(value if value is not None else "").strip()[:limit]
    text = _TEXT_SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _KEY_LIKE.sub("[REDACTED]", text)
    return _GEMINI_KEY_LIKE.sub("[REDACTED]", text)


def sanitize_json(value: Any) -> Any:
    """Deep-copy JSON-ish data with secret values removed and sizes bounded."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            name = str(key)[:160]
            result[name] = "[REDACTED]" if _SENSITIVE_JSON_KEY.search(name) else sanitize_json(item)
        return result
    if isinstance(value, list):
        return [sanitize_json(item) for item in value[:200]]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value, limit=1000)


def redact_json(value: Any) -> Any:
    """Deep-copy JSON-ish data while redacting secrets but not dropping data.

    ``sanitize_json`` is intentionally bounded for prompts and audit previews.
    Reducer bindings are different: silently dropping the 201st list item or
    key can make a valid pointer look invalid.  Authoritative result refs use
    this lossless-redaction variant and apply their byte limit explicitly.
    """
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)[:160]
            result[name] = "[REDACTED]" if _SENSITIVE_JSON_KEY.search(name) else redact_json(item)
        return result
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value, limit=RESULT_REF_MAX_BYTES)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value, limit=4000)


def canonical_json(value: Any) -> str:
    return json.dumps(sanitize_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_authoritative_json(value: Any) -> str:
    return json.dumps(redact_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def bound_json_text(value: Any, *, limit: int = 16_000) -> str:
    encoded = canonical_json(value)
    if len(encoded) > limit:
        return json.dumps({"truncated": True, "preview": encoded[: limit - 60]}, ensure_ascii=False)
    return encoded


def input_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


RESULT_REF_MAX_BYTES = 64 * 1024


def create_result_ref(
    db: sqlite3.Connection, run_row: sqlite3.Row, *, step_id: str,
    result: Any, schema_revision: str = "",
) -> str:
    """Persist one sanitized authoritative result for reducer bindings.

    The reference is scoped to the run and step, and large values are marked
    unavailable rather than silently projected into a lossy prompt summary.
    """
    encoded = canonical_authoritative_json(result)
    truncated = int(len(encoded.encode("utf-8")) > RESULT_REF_MAX_BYTES)
    if truncated:
        encoded = json.dumps({"truncated": True, "digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest()})
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    ref_id = f"oref_{secrets.token_hex(12)}"
    db.execute(
        """INSERT INTO operator_run_result_refs
           (id,run_id,tenant_id,project_id,step_id,schema_revision,result_json,result_digest,truncated)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id,step_id) DO UPDATE SET
             schema_revision=excluded.schema_revision, result_json=excluded.result_json,
             result_digest=excluded.result_digest, truncated=excluded.truncated""",
        (
            ref_id, str(run_row["id"]), str(run_row["tenant_id"]), str(run_row["project_id"]),
            str(step_id), str(schema_revision or ""), encoded, digest, truncated,
        ),
    )
    row = db.execute(
        "SELECT id FROM operator_run_result_refs WHERE run_id=? AND step_id=?",
        (str(run_row["id"]), str(step_id)),
    ).fetchone()
    return str(row["id"]) if row is not None else ref_id


def get_result_ref(db: sqlite3.Connection, *, run_id: str, ref_id: str) -> Any:
    row = db.execute(
        "SELECT result_json,truncated FROM operator_run_result_refs WHERE run_id=? AND id=?",
        (str(run_id), str(ref_id)),
    ).fetchone()
    if row is None:
        raise OperatorNotFound("Operator result reference was not found.")
    if bool(row["truncated"]):
        raise OperatorValidationError("The referenced result is too large for deterministic binding.")
    try:
        return json.loads(str(row["result_json"] or "null"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OperatorValidationError("The referenced result is not valid JSON.") from exc


# ----------------------------------------------------------------------
# Profiles
# ----------------------------------------------------------------------

def create_profile(
    db: sqlite3.Connection, *, tenant_id: str, project_id: str, name: str,
    instructions: str, granted_capabilities: frozenset[str], enabled: bool,
    max_steps: int, max_consecutive_tool_failures: int, max_model_failures: int,
    max_total_tokens: int = 0, max_model_decisions: int = 0,
    max_network_attempts: int = 0, no_progress_limit: int = 3,
    retrieval_mode: str = "hybrid_local", retrieval_canary_percent: int = 10,
    decision_policy: str = "cloud_only", needle_canary_percent: int = 0,
    needle_read_confidence: float = 0.92, needle_write_confidence: float = 0.97,
    needle_sensitive_confidence: float = 0.99,
) -> sqlite3.Row:
    profile_id = f"opr_{secrets.token_hex(12)}"
    try:
        db.execute(
            """INSERT INTO operator_profiles
               (id,tenant_id,project_id,name,enabled,instructions,granted_capabilities_json,
                max_steps,max_consecutive_tool_failures,max_model_failures,
                max_total_tokens,max_model_decisions,max_network_attempts,no_progress_limit,
                retrieval_mode,retrieval_canary_percent,decision_policy,needle_canary_percent,
                needle_read_confidence,needle_write_confidence,needle_sensitive_confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                profile_id, tenant_id, project_id, name.strip(), int(enabled),
                redact_text(instructions, limit=8000),
                json.dumps(sorted(granted_capabilities)),
                int(max_steps), int(max_consecutive_tool_failures), int(max_model_failures),
                int(max_total_tokens), int(max_model_decisions), int(max_network_attempts),
                int(no_progress_limit), str(retrieval_mode), int(retrieval_canary_percent),
                str(decision_policy), int(needle_canary_percent), float(needle_read_confidence),
                float(needle_write_confidence), float(needle_sensitive_confidence),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise OperatorConflict("An operator profile with that name already exists.") from exc
    return get_profile(db, tenant_id=tenant_id, profile_id=profile_id)


def get_profile(db: sqlite3.Connection, *, tenant_id: str, profile_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM operator_profiles WHERE id=? AND tenant_id=?",
        (profile_id, tenant_id),
    ).fetchone()
    if row is None:
        raise OperatorNotFound("Operator profile not found.")
    return row


def list_profiles(
    db: sqlite3.Connection, *, tenant_id: str, project_id: str | None = None,
    include_global: bool = True,
) -> list[sqlite3.Row]:
    clauses, params = ["tenant_id=?", "archived_at IS NULL"], [tenant_id]
    if project_id is not None:
        clause = "(project_id=? OR project_id='')" if include_global else "project_id=?"
        clauses.append(clause)
        params.extend([project_id] if include_global else [project_id])
    return db.execute(
        f"SELECT * FROM operator_profiles WHERE {' AND '.join(clauses)} ORDER BY created_at, id",
        params,
    ).fetchall()


def archive_profile(db: sqlite3.Connection, *, tenant_id: str, profile_id: str) -> sqlite3.Row:
    row = get_profile(db, tenant_id=tenant_id, profile_id=profile_id)
    if row["archived_at"]:
        raise OperatorConflict("Operator profile is already removed.")
    active_run = db.execute(
        "SELECT 1 FROM operator_runs WHERE tenant_id=? AND profile_id=? AND status NOT IN ('completed','failed','cancelled') LIMIT 1",
        (tenant_id, profile_id),
    ).fetchone()
    if active_run:
        raise OperatorConflict("This profile has active runs. Let them finish before removing the profile.")
    db.execute(
        "UPDATE operator_profiles SET enabled=0,archived_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
        (profile_id, tenant_id),
    )
    return get_profile(db, tenant_id=tenant_id, profile_id=profile_id)


def update_profile(
    db: sqlite3.Connection, *, tenant_id: str, profile_id: str,
    fields: dict[str, Any],
) -> sqlite3.Row:
    row = get_profile(db, tenant_id=tenant_id, profile_id=profile_id)
    if row["archived_at"]:
        raise OperatorConflict("Removed Operator profiles cannot be changed.")
    assignments: list[str] = []
    params: list[Any] = []
    column_values: dict[str, Any] = {}
    if "name" in fields and fields["name"] is not None:
        column_values["name"] = str(fields["name"]).strip()
    if "enabled" in fields and fields["enabled"] is not None:
        column_values["enabled"] = int(bool(fields["enabled"]))
    if "instructions" in fields and fields["instructions"] is not None:
        column_values["instructions"] = redact_text(fields["instructions"], limit=8000)
    if "granted_capabilities" in fields and fields["granted_capabilities"] is not None:
        column_values["granted_capabilities_json"] = json.dumps(sorted(fields["granted_capabilities"]))
    if "max_steps" in fields and fields["max_steps"] is not None:
        column_values["max_steps"] = int(fields["max_steps"])
    if "max_consecutive_tool_failures" in fields and fields["max_consecutive_tool_failures"] is not None:
        column_values["max_consecutive_tool_failures"] = int(fields["max_consecutive_tool_failures"])
    if "max_model_failures" in fields and fields["max_model_failures"] is not None:
        column_values["max_model_failures"] = int(fields["max_model_failures"])
    for name in (
        "max_total_tokens", "max_model_decisions", "max_network_attempts", "no_progress_limit",
        "retrieval_canary_percent", "needle_canary_percent",
    ):
        if name in fields and fields[name] is not None:
            column_values[name] = int(fields[name])
    if "retrieval_mode" in fields and fields["retrieval_mode"] is not None:
        mode = str(fields["retrieval_mode"]).strip()
        if mode not in RETRIEVAL_MODES:
            raise OperatorValidationError("retrieval_mode must be lexical or hybrid_local.")
        column_values["retrieval_mode"] = mode
    if "decision_policy" in fields and fields["decision_policy"] is not None:
        mode = str(fields["decision_policy"]).strip()
        if mode not in {"cloud_only", "needle_shadow", "needle_assisted", "needle_only"}:
            raise OperatorValidationError("decision_policy must be cloud_only, needle_shadow, needle_assisted, or needle_only.")
        column_values["decision_policy"] = mode
    for name in ("needle_read_confidence", "needle_write_confidence", "needle_sensitive_confidence"):
        if name in fields and fields[name] is not None:
            column_values[name] = float(fields[name])
    for column, value in column_values.items():
        assignments.append(f"{column}=?")
        params.append(value)
    if not assignments:
        return row
    try:
        db.execute(
            f"UPDATE operator_profiles SET {', '.join(assignments)},updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
            (*params, profile_id, tenant_id),
        )
    except sqlite3.IntegrityError as exc:
        raise OperatorConflict("An operator profile with that name already exists.") from exc
    return get_profile(db, tenant_id=tenant_id, profile_id=profile_id)


def profile_public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "project_id": str(row["project_id"] or ""),
        "name": str(row["name"]),
        "enabled": bool(row["enabled"]),
        "instructions": str(row["instructions"] or ""),
        "granted_capabilities": json.loads(str(row["granted_capabilities_json"] or "[]")),
        "max_steps": int(row["max_steps"]),
        "max_consecutive_tool_failures": int(row["max_consecutive_tool_failures"]),
        "max_model_failures": int(row["max_model_failures"]),
        "max_total_tokens": int(row["max_total_tokens"] or 0),
        "max_model_decisions": int(row["max_model_decisions"] or 0),
        "max_network_attempts": int(row["max_network_attempts"] or 0),
        "no_progress_limit": int(row["no_progress_limit"] or 3),
        "retrieval_mode": str(row["retrieval_mode"] or "hybrid_local"),
        "retrieval_canary_percent": int(row["retrieval_canary_percent"] or 0),
        "decision_policy": str(row["decision_policy"] or "cloud_only"),
        "needle_canary_percent": int(row["needle_canary_percent"] or 0),
        "needle_read_confidence": float(row["needle_read_confidence"] if row["needle_read_confidence"] is not None else 0.92),
        "needle_write_confidence": float(row["needle_write_confidence"] if row["needle_write_confidence"] is not None else 0.97),
        "needle_sensitive_confidence": float(row["needle_sensitive_confidence"] if row["needle_sensitive_confidence"] is not None else 0.99),
        "archived_at": str(row["archived_at"] or ""),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


# ----------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------

def create_run(
    db: sqlite3.Connection, *, profile_row: sqlite3.Row, tenant_id: str,
    project_id: str, objective: str, context: dict[str, Any],
    idempotency_key: str, created_by: str,
    decision_model_checksum: str = "", tool_schema_fingerprint: str = "",
) -> tuple[sqlite3.Row, bool]:
    if profile_row["archived_at"]:
        raise OperatorConflict("Removed Operator profiles cannot start new runs.")
    safe_objective = redact_text(objective, limit=8000)
    if not safe_objective:
        raise OperatorValidationError("Operator run requires a non-empty objective.")
    if idempotency_key:
        existing = db.execute(
            """SELECT id, objective FROM operator_runs
               WHERE tenant_id=? AND profile_id=? AND idempotency_key=?""",
            (tenant_id, profile_row["id"], idempotency_key),
        ).fetchone()
        if existing is not None:
            if str(existing["objective"]) != safe_objective:
                raise OperatorConflict(
                    "That idempotency key was already used with different input."
                )
            return get_run(db, tenant_id=tenant_id, run_id=str(existing["id"])), True
    run_id = f"oprun_{secrets.token_hex(12)}"
    try:
        db.execute(
            """INSERT INTO operator_runs
               (id,profile_id,tenant_id,project_id,objective,context_json,idempotency_key,
                decision_policy,needle_read_confidence,needle_write_confidence,
                needle_sensitive_confidence,needle_canary_percent,decision_model_checksum,
                tool_schema_fingerprint,created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, str(profile_row["id"]), tenant_id, project_id, safe_objective,
                bound_json_text(context), idempotency_key,
                str(profile_row["decision_policy"] or "cloud_only"),
                float(profile_row["needle_read_confidence"] if profile_row["needle_read_confidence"] is not None else 0.92),
                float(profile_row["needle_write_confidence"] if profile_row["needle_write_confidence"] is not None else 0.97),
                float(profile_row["needle_sensitive_confidence"] if profile_row["needle_sensitive_confidence"] is not None else 0.99),
                int(profile_row["needle_canary_percent"] or 0),
                str(decision_model_checksum or ""), str(tool_schema_fingerprint or ""), created_by,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise OperatorConflict("That idempotency key was already used with different input.") from exc
    return get_run(db, tenant_id=tenant_id, run_id=run_id), False


def get_run(db: sqlite3.Connection, *, tenant_id: str, run_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM operator_runs WHERE id=? AND tenant_id=?",
        (run_id, tenant_id),
    ).fetchone()
    if row is None:
        raise OperatorNotFound("Operator run not found.")
    return row


def get_run_any_tenant(db: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    """Internal executor lookup; service layer still enforces scope on outputs."""
    row = db.execute("SELECT * FROM operator_runs WHERE id=?", (run_id,)).fetchone()
    return row


# ----------------------------------------------------------------------
# Executor leases
# ----------------------------------------------------------------------

def acquire_executor_lease(
    db: sqlite3.Connection, *, run_id: str, owner_id: str,
    lease_seconds: int = 180,
) -> dict[str, Any] | None:
    """Atomically claim the single executor lease for ``run_id``.

    The lease intentionally lives outside ``operator_runs`` so a cancellation
    can still transition the run while a provider call is in flight.  The
    opaque fencing token must be presented on every renewal and before an
    external response is persisted.  Expired leases may be taken by a new
    executor; late owners can then observe but never apply their result.
    """
    duration = max(EXECUTOR_LEASE_SECONDS_BOUND[0], min(int(lease_seconds), EXECUTOR_LEASE_SECONDS_BOUND[1]))
    token = f"olease_{secrets.token_urlsafe(24)}"
    owner = str(owner_id or "")[:160]
    if not owner:
        raise OperatorValidationError("Executor lease requires an owner id.")
    db.execute(
        """INSERT INTO operator_run_executor_leases
           (run_id,owner_id,fencing_token,epoch,expires_at)
           VALUES (?,?,?,1,datetime('now', ?))
           ON CONFLICT(run_id) DO UPDATE SET
               owner_id=excluded.owner_id,
               fencing_token=excluded.fencing_token,
               epoch=operator_run_executor_leases.epoch + 1,
               expires_at=excluded.expires_at,
               heartbeat_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP
           WHERE operator_run_executor_leases.expires_at<=CURRENT_TIMESTAMP""",
        (run_id, owner, token, f"+{duration} seconds"),
    )
    row = db.execute(
        """SELECT owner_id,fencing_token,epoch,expires_at
           FROM operator_run_executor_leases WHERE run_id=?""",
        (run_id,),
    ).fetchone()
    if row is None or str(row["fencing_token"]) != token:
        return None
    return {
        "run_id": run_id,
        "owner_id": owner,
        "fencing_token": token,
        "epoch": int(row["epoch"]),
        "expires_at": str(row["expires_at"]),
        "lease_seconds": duration,
    }


def renew_executor_lease(
    db: sqlite3.Connection, *, run_id: str, fencing_token: str,
    lease_seconds: int = 180,
) -> dict[str, Any]:
    """Extend a still-owned lease or raise before any result is applied."""
    duration = max(EXECUTOR_LEASE_SECONDS_BOUND[0], min(int(lease_seconds), EXECUTOR_LEASE_SECONDS_BOUND[1]))
    cursor = db.execute(
        """UPDATE operator_run_executor_leases
           SET expires_at=datetime('now', ?),heartbeat_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
           WHERE run_id=? AND fencing_token=? AND expires_at>CURRENT_TIMESTAMP""",
        (f"+{duration} seconds", run_id, str(fencing_token)),
    )
    if cursor.rowcount != 1:
        raise OperatorLeaseLost("The operator executor lease was lost before this result could be applied.")
    row = db.execute(
        "SELECT owner_id,epoch,expires_at FROM operator_run_executor_leases WHERE run_id=?",
        (run_id,),
    ).fetchone()
    return {
        "run_id": run_id,
        "owner_id": str(row["owner_id"]),
        "fencing_token": str(fencing_token),
        "epoch": int(row["epoch"]),
        "expires_at": str(row["expires_at"]),
        "lease_seconds": duration,
    }


def release_executor_lease(db: sqlite3.Connection, *, run_id: str, fencing_token: str) -> bool:
    """Release only the caller's lease; a newer owner is never disturbed."""
    cursor = db.execute(
        "DELETE FROM operator_run_executor_leases WHERE run_id=? AND fencing_token=?",
        (run_id, str(fencing_token)),
    )
    return bool(cursor.rowcount)


def list_runs(
    db: sqlite3.Connection, *, tenant_id: str, project_id: str | None = None,
    status: str | None = None, limit: int = 50,
) -> list[sqlite3.Row]:
    if not 1 <= int(limit) <= 500:
        raise OperatorValidationError("Run list limit must be between 1 and 500.")
    if status is not None and status not in RUN_STATES:
        raise OperatorValidationError(f"Unknown run status: {status}")
    clauses, params = ["tenant_id=?"], [tenant_id]
    if project_id:
        clauses.append("project_id=?")
        params.append(project_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    rows = db.execute(
        f"""SELECT * FROM operator_runs WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, id DESC LIMIT ?""",
        (*params, int(limit)),
    ).fetchall()
    return rows


def transition_run(
    db: sqlite3.Connection, run_id: str, *, expected_revision: int,
    target_status: str, error_code: str = "", error_summary: str = "",
    summary: str | None = None, wake_reason: str | None = None,
    wake_at: str | None = ..., pending_job_id: str | None = None,
    pending_operation_json: str | None = None,
    pending_approval_id: str | None = None, current_step: int | None = None,
    consecutive_tool_failures: int | None = None, model_failures: int | None = None,
    provider: str | None = None, protocol: str | None = None, model_name: str | None = None,
) -> sqlite3.Row:
    """CAS-guarded deterministic transition. Raises on stale revision."""
    if target_status not in RUN_STATES:
        raise OperatorValidationError(f"Unknown run status: {target_status}")
    row = db.execute("SELECT * FROM operator_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise OperatorNotFound("Operator run not found.")
    current_status = str(row["status"])
    if target_status not in RUN_TRANSITIONS.get(current_status, set()):
        raise OperatorInvalidTransition(f"Operator run cannot move from {current_status} to {target_status}.")
    next_revision = expected_revision + 1
    assignments = ["status=?", "revision=?", "updated_at=CURRENT_TIMESTAMP"]
    params: list[Any] = [target_status, next_revision]
    if summary is not None:
        assignments.append("summary=?")
        params.append(redact_text(summary, limit=4000))
    if error_code:
        assignments.append("error_code=?")
        params.append(error_code[:120])
    if error_summary:
        assignments.append("error_summary=?")
        params.append(redact_text(error_summary, limit=2000))
    if wake_reason is not None:
        assignments.append("wake_reason=?")
        params.append(wake_reason[:40])
    if wake_at is not ...:
        assignments.append("wake_at=?")
        params.append(wake_at)
    if pending_job_id is not None:
        assignments.append("pending_job_id=?")
        params.append(pending_job_id)
    if pending_operation_json is not None:
        assignments.append("pending_operation_json=?")
        params.append(pending_operation_json)
    if pending_approval_id is not None:
        assignments.append("pending_approval_id=?")
        params.append(pending_approval_id)
    if current_step is not None:
        assignments.append("current_step=?")
        params.append(int(current_step))
    if consecutive_tool_failures is not None:
        assignments.append("consecutive_tool_failures=?")
        params.append(int(consecutive_tool_failures))
    if model_failures is not None:
        assignments.append("model_failures=?")
        params.append(int(model_failures))
    if provider is not None:
        assignments.append("last_provider=?")
        params.append(provider[:80])
    if protocol is not None:
        assignments.append("last_protocol=?")
        params.append(protocol[:80])
    if model_name is not None:
        assignments.append("last_model=?")
        params.append(model_name[:160])
    if target_status == "running":
        assignments.append("started_at=COALESCE(started_at, CURRENT_TIMESTAMP)")
    if target_status in TERMINAL_RUN_STATES:
        assignments.append("completed_at=CURRENT_TIMESTAMP")
        assignments.append("wake_reason=''")
        assignments.append("pending_job_id=''")
        assignments.append("pending_operation_json=''")
        assignments.append("pending_approval_id=''")
    cursor = db.execute(
        f"UPDATE operator_runs SET {', '.join(assignments)} WHERE id=? AND revision=?",
        (*params, run_id, expected_revision),
    )
    if cursor.rowcount != 1:
        raise OperatorConflict(
            "The operator run changed while this step was executing. Resume it to continue."
        )
    return get_run(db, tenant_id=str(row["tenant_id"]), run_id=run_id)


def append_step(
    db: sqlite3.Connection, run_row: sqlite3.Row, *, sequence: int,
    step_type: str, tool_name: str = "", decision: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None, output: dict[str, Any] | None = None,
    job_id: str | None = None, approval_id: str | None = None,
    provider: str = "", protocol: str = "", model_name: str = "",
    usage: dict[str, Any] | None = None, provider_attempts: int = 0,
    decision_backend: str = "", decision_confidence: float | None = None,
    decision_risk_class: str = "", needle_latency_ms: int = 0,
    needle_schema_fingerprint: str = "", evidence_validation_result: str = "",
    sensitive_verification_required: bool = False, fallback_reason: str = "",
    duration_ms: int = 0, status: str = "succeeded", error_code: str = "",
    error_summary: str = "",
) -> int:
    if step_type not in STEP_TYPES:
        raise OperatorValidationError(f"Unknown operator step type: {step_type}")
    if sequence != int(run_row["current_step"]):
        # Callers advance ``current_step`` through transition_run (CAS-guarded)
        # and then persist the step at that same sequence; UNIQUE(run_id,
        # sequence) is the final integrity backstop.
        raise OperatorConflict("Operator step sequence diverged from the persisted run state.")
    usage_payload = usage if isinstance(usage, dict) else {}
    # Provider usage belongs to the model request that produced the decision.
    # Handler rows (tool/approval/wait/finish/resume) are audit references to
    # that decision, not additional model requests. Keeping counters only on
    # their owner prevents the UI and exports from charging one call multiple
    # times when a single decision is followed by a tool step.
    if step_type in {"tool_call", "wait", "resume", "finish", "approval_request", "action_plan"}:
        usage_payload = {}
        provider_attempts = 0
    def _usage_int(*keys: str) -> int:
        for key in keys:
            try:
                value = int(usage_payload.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        return 0

    input_tokens = _usage_int("input_tokens", "prompt_tokens")
    output_tokens = _usage_int("output_tokens", "completion_tokens")
    cached_input_tokens = _usage_int("cached_input_tokens", "cache_read_input_tokens")
    reasoning_tokens = _usage_int("reasoning_tokens", "thoughts_tokens")
    usage_exact = int(bool(usage_payload.get("exact")))
    try:
        prompt_chars = max(0, int(usage_payload.get("prompt_chars") or 0))
    except (TypeError, ValueError):
        prompt_chars = 0
    cursor = db.execute(
        """INSERT INTO operator_run_steps
           (run_id,tenant_id,project_id,sequence,step_type,status,tool_name,
            decision_json,input_json,output_json,job_id,approval_id,
            provider,protocol,model,input_tokens,output_tokens,cached_input_tokens,
            reasoning_tokens,usage_exact,provider_attempts,prompt_chars,decision_backend,
            decision_confidence,decision_risk_class,needle_latency_ms,needle_schema_fingerprint,
            evidence_validation_result,sensitive_verification_required,fallback_reason,
            duration_ms,error_code,error_summary)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(run_row["id"]), str(run_row["tenant_id"]), str(run_row["project_id"]),
            int(sequence), step_type, status, tool_name,
            bound_json_text(decision or {}), bound_json_text(inputs or {}),
            bound_json_text(output or {}), job_id, approval_id,
            provider[:80], protocol[:80], model_name[:160],
            input_tokens, output_tokens, cached_input_tokens, reasoning_tokens,
            usage_exact, max(0, int(provider_attempts)), prompt_chars,
            str(decision_backend or "")[:80],
            None if decision_confidence is None else float(decision_confidence),
            str(decision_risk_class or "")[:80], max(0, int(needle_latency_ms or 0)),
            str(needle_schema_fingerprint or "")[:128],
            str(evidence_validation_result or "")[:160], int(bool(sensitive_verification_required)),
            redact_text(fallback_reason, limit=500),
            max(0, int(duration_ms)), error_code[:120],
            redact_text(error_summary, limit=2000),
        ),
    )
    return int(cursor.lastrowid)


def append_ai_attempts(
    db: sqlite3.Connection, run_row: sqlite3.Row, *, decision_sequence: int,
    records: list[dict[str, Any]] | None,
) -> int:
    """Persist one row per provider attempt without charging audit rows twice.

    The provider layer owns attempt identity; this store only attaches the
    records to the durable run after the call returns. ``INSERT OR IGNORE``
    makes a replay idempotent when a worker crashes after the model decision
    was persisted but before the surrounding transaction is retried.
    """
    if not isinstance(records, list):
        return 0

    def _int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    inserted = 0
    for ordinal, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        attempt_id = str(record.get("attempt_id") or f"aiatt_{secrets.token_hex(12)}")[:96]
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        input_tokens = _int(usage.get("input_tokens") or usage.get("prompt_tokens"))
        output_tokens = _int(usage.get("output_tokens") or usage.get("completion_tokens"))
        cached_tokens = _int(usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens"))
        reasoning_tokens = _int(usage.get("reasoning_tokens") or usage.get("thoughts_tokens"))
        http_attempts = _int(usage.get("http_attempts"))
        prompt_chars = _int(usage.get("prompt_chars"))
        try:
            cursor = db.execute(
                """INSERT OR IGNORE INTO operator_ai_attempts
                   (attempt_id,run_id,tenant_id,project_id,decision_sequence,ordinal,
                    provider,profile_id,protocol,model,network_attempt,outcome,failure_kind,
                    input_tokens,output_tokens,cached_input_tokens,reasoning_tokens,
                    http_attempts,prompt_chars,usage_exact,usage_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id, str(run_row["id"]), str(run_row["tenant_id"]),
                    str(run_row["project_id"]), max(0, int(decision_sequence)),
                    _int(record.get("ordinal")) or ordinal,
                    str(record.get("provider") or "")[:80],
                    str(record.get("profile_id") or "")[:160],
                    str(record.get("protocol") or "")[:80],
                    str(record.get("model") or "")[:160],
                    int(bool(record.get("network_attempt"))),
                    str(record.get("outcome") or "")[:40],
                    str(record.get("failure_kind") or "")[:120],
                    input_tokens, output_tokens, cached_tokens, reasoning_tokens,
                    http_attempts, prompt_chars, int(bool(usage.get("exact"))),
                    bound_json_text(usage, limit=2000),
                ),
            )
            inserted += int(cursor.rowcount > 0)
        except sqlite3.IntegrityError:
            # Legacy databases may have the table created by an older additive
            # initializer. A duplicate is already accounted for; never retry
            # a provider call merely because telemetry was replayed.
            continue
    return inserted


def ai_attempts_for_run(
    db: sqlite3.Connection, run_id: str, *, limit: int | None = None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM operator_ai_attempts WHERE run_id=? ORDER BY decision_sequence, ordinal"
    params: list[Any] = [run_id]
    if limit is not None:
        query = "SELECT * FROM (" + query + " DESC LIMIT ?) ORDER BY decision_sequence, ordinal"
        params.append(max(1, int(limit)))
    rows = db.execute(query, params).fetchall()
    return [
        {
            "attempt_id": str(row["attempt_id"]),
            "run_id": str(row["run_id"]),
            "decision_sequence": int(row["decision_sequence"] or 0),
            "ordinal": int(row["ordinal"] or 0),
            "provider": str(row["provider"] or ""),
            "profile_id": str(row["profile_id"] or ""),
            "protocol": str(row["protocol"] or ""),
            "model": str(row["model"] or ""),
            "network_attempt": bool(row["network_attempt"]),
            "outcome": str(row["outcome"] or ""),
            "failure_kind": str(row["failure_kind"] or ""),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cached_input_tokens": int(row["cached_input_tokens"] or 0),
            "reasoning_tokens": int(row["reasoning_tokens"] or 0),
            "http_attempts": int(row["http_attempts"] or 0),
            "prompt_chars": int(row["prompt_chars"] or 0),
            "usage_exact": bool(row["usage_exact"]),
        }
        for row in rows
    ]


def ai_usage_summary(db: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Return billing-safe cloud usage plus local Needle outcome counters."""
    row = db.execute(
        """SELECT COUNT(*) AS attempts,
                  SUM(CASE WHEN network_attempt=1 THEN 1 ELSE 0 END) AS network_attempts,
                  SUM(CASE WHEN provider='local_needle' THEN 1 ELSE 0 END) AS local_decisions,
                  SUM(CASE WHEN provider='local_needle' AND outcome='accepted' THEN 1 ELSE 0 END) AS needle_accepted,
                  SUM(CASE WHEN provider='local_needle' AND outcome='fallback' THEN 1 ELSE 0 END) AS needle_fallbacks,
                  SUM(CASE WHEN provider='local_needle' AND outcome NOT IN ('accepted', 'fallback') THEN 1 ELSE 0 END) AS needle_rejected,
                  SUM(CASE WHEN provider='local_needle' AND outcome='accepted' THEN CAST(prompt_chars / 4 AS INTEGER) ELSE 0 END) AS estimated_cloud_tokens_avoided,
                  SUM(CASE WHEN network_attempt=1 THEN input_tokens ELSE 0 END) AS input_tokens,
                  SUM(CASE WHEN network_attempt=1 THEN output_tokens ELSE 0 END) AS output_tokens,
                  SUM(CASE WHEN network_attempt=1 THEN cached_input_tokens ELSE 0 END) AS cached_input_tokens,
                  SUM(CASE WHEN network_attempt=1 THEN reasoning_tokens ELSE 0 END) AS reasoning_tokens,
                  SUM(CASE WHEN network_attempt=1 AND usage_exact=0 THEN 1 ELSE 0 END) AS unknown_attempts,
                  SUM(CASE WHEN network_attempt=1 THEN http_attempts ELSE 0 END) AS http_attempts
           FROM operator_ai_attempts WHERE run_id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        return {
            "attempts": 0, "network_attempts": 0, "local_decisions": 0,
            "needle_accepted": 0, "needle_rejected": 0, "needle_fallbacks": 0,
            "estimated_cloud_tokens_avoided": 0, "cloud_input_tokens": 0,
            "cloud_output_tokens": 0, "cloud_total_tokens": 0, "input_tokens": 0,
            "output_tokens": 0, "cached_input_tokens": 0, "reasoning_tokens": 0,
            "total_tokens": 0, "unknown_attempts": 0, "http_attempts": 0,
        }
    input_tokens = int(row["input_tokens"] or 0)
    output_tokens = int(row["output_tokens"] or 0)
    reasoning_tokens = int(row["reasoning_tokens"] or 0)
    cloud_total_tokens = input_tokens + output_tokens + reasoning_tokens
    return {
        "attempts": int(row["attempts"] or 0),
        "network_attempts": int(row["network_attempts"] or 0),
        "local_decisions": int(row["local_decisions"] or 0),
        "needle_accepted": int(row["needle_accepted"] or 0),
        "needle_rejected": int(row["needle_rejected"] or 0),
        "needle_fallbacks": int(row["needle_fallbacks"] or 0),
        "estimated_cloud_tokens_avoided": int(row["estimated_cloud_tokens_avoided"] or 0),
        "cloud_input_tokens": input_tokens,
        "cloud_output_tokens": output_tokens,
        "cloud_total_tokens": cloud_total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": int(row["cached_input_tokens"] or 0),
        "reasoning_tokens": reasoning_tokens,
        # Preserve the existing UI field while ensuring local inference never
        # appears as provider-token spend.
        "total_tokens": cloud_total_tokens,
        "unknown_attempts": int(row["unknown_attempts"] or 0),
        "http_attempts": int(row["http_attempts"] or 0),
    }


def complete_step(
    db: sqlite3.Connection, step_id: int, *, status: str,
    output: dict[str, Any] | None = None, duration_ms: int = 0,
    error_code: str = "", error_summary: str = "",
) -> None:
    db.execute(
        """UPDATE operator_run_steps SET status=?,output_json=?,duration_ms=?,
               error_code=?,error_summary=? WHERE id=?""",
        (
            status, bound_json_text(output or {}), max(0, int(duration_ms)),
            error_code[:120], redact_text(error_summary, limit=2000), step_id,
        ),
    )


def count_steps(db: sqlite3.Connection, run_id: str, step_type: str) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM operator_run_steps WHERE run_id=? AND step_type=?",
        (run_id, step_type),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def steps_for_run(db: sqlite3.Connection, run_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    if limit is None:
        rows = db.execute(
            "SELECT * FROM operator_run_steps WHERE run_id=? ORDER BY sequence", (run_id,)
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT * FROM (
                   SELECT * FROM operator_run_steps WHERE run_id=?
                   ORDER BY sequence DESC LIMIT ?
               ) ORDER BY sequence ASC""",
            (run_id, int(limit)),
        ).fetchall()
    return [step_public(row) for row in rows]


def step_public(row: sqlite3.Row) -> dict[str, Any]:
    def _json(raw: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(str(raw or "{}"))
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    return {
        "id": int(row["id"]),
        "sequence": int(row["sequence"]),
        "step_type": str(row["step_type"]),
        "status": str(row["status"]),
        "tool_name": str(row["tool_name"] or ""),
        "decision": _json(row["decision_json"]),
        "input": _json(row["input_json"]),
        "output": _json(row["output_json"]),
        "job_id": row["job_id"],
        "approval_id": row["approval_id"],
        "provider": str(row["provider"] or ""),
        "protocol": str(row["protocol"] or ""),
        "model": str(row["model"] or ""),
        "input_tokens": int(row["input_tokens"] or 0),
        "output_tokens": int(row["output_tokens"] or 0),
        "cached_input_tokens": int(row["cached_input_tokens"] or 0),
        "reasoning_tokens": int(row["reasoning_tokens"] or 0),
        "usage_exact": bool(row["usage_exact"]),
        "provider_attempts": int(row["provider_attempts"] or 0),
        "prompt_chars": int(row["prompt_chars"] or 0),
        "decision_backend": str(row["decision_backend"] or ""),
        "decision_confidence": (
            None if row["decision_confidence"] is None else float(row["decision_confidence"])
        ),
        "decision_risk_class": str(row["decision_risk_class"] or ""),
        "needle_latency_ms": int(row["needle_latency_ms"] or 0),
        "needle_schema_fingerprint": str(row["needle_schema_fingerprint"] or ""),
        "evidence_validation_result": str(row["evidence_validation_result"] or ""),
        "sensitive_verification_required": bool(row["sensitive_verification_required"]),
        "fallback_reason": str(row["fallback_reason"] or ""),
        "duration_ms": int(row["duration_ms"]),
        "error_code": str(row["error_code"] or ""),
        "error_summary": str(row["error_summary"] or ""),
        "created_at": str(row["created_at"]),
    }


def run_public(row: sqlite3.Row, *, steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result = {
        "id": str(row["id"]),
        "profile_id": str(row["profile_id"]),
        "tenant_id": str(row["tenant_id"]),
        "project_id": str(row["project_id"]),
        "objective": str(row["objective"]),
        "status": str(row["status"]),
        "summary": str(row["summary"] or ""),
        "context": json.loads(str(row["context_json"] or "{}")),
        "current_step": int(row["current_step"]),
        "consecutive_tool_failures": int(row["consecutive_tool_failures"]),
        "model_failures": int(row["model_failures"]),
        "wake_reason": str(row["wake_reason"] or ""),
        "wake_at": row["wake_at"],
        "pending_job_id": str(row["pending_job_id"] or ""),
        "pending_operation": (
            json.loads(str(row["pending_operation_json"] or "{}"))
            if "pending_operation_json" in row.keys() and row["pending_operation_json"]
            else None
        ),
        "pending_approval_id": str(row["pending_approval_id"] or ""),
        "error_code": str(row["error_code"] or ""),
        "error_summary": str(row["error_summary"] or ""),
        "revision": int(row["revision"]),
        "idempotency_key": str(row["idempotency_key"] or ""),
        "last_provider": str(row["last_provider"] or ""),
        "last_protocol": str(row["last_protocol"] or ""),
        "last_model": str(row["last_model"] or ""),
        "decision_policy": str(row["decision_policy"] or "cloud_only"),
        "needle_read_confidence": float(row["needle_read_confidence"] if row["needle_read_confidence"] is not None else 0.92),
        "needle_write_confidence": float(row["needle_write_confidence"] if row["needle_write_confidence"] is not None else 0.97),
        "needle_sensitive_confidence": float(row["needle_sensitive_confidence"] if row["needle_sensitive_confidence"] is not None else 0.99),
        "needle_canary_percent": int(row["needle_canary_percent"] or 0),
        "decision_model_checksum": str(row["decision_model_checksum"] or ""),
        "tool_schema_fingerprint": str(row["tool_schema_fingerprint"] or ""),
        "created_by": str(row["created_by"] or ""),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }
    if steps is not None:
        result["steps"] = steps
    return result


# ----------------------------------------------------------------------
# Approvals — the waiting_approval boundary other sessions resolve
# ----------------------------------------------------------------------

def create_approval(
    db: sqlite3.Connection, run_row: sqlite3.Row, *, action: str,
    summary: str, payload_ref: dict[str, Any], requested_by: str,
) -> sqlite3.Row:
    approval_id = f"opapp_{secrets.token_hex(12)}"
    if not action.strip():
        raise OperatorValidationError("Operator approval requires an action.")
    db.execute(
        """INSERT INTO operator_approvals
           (id,run_id,tenant_id,project_id,action,summary,payload_ref_json,requested_by)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            approval_id, str(run_row["id"]), str(run_row["tenant_id"]),
            str(run_row["project_id"]), action.strip()[:200],
            redact_text(summary, limit=2000), bound_json_text(payload_ref),
            requested_by[:160],
        ),
    )
    return get_approval(db, tenant_id=str(run_row["tenant_id"]), approval_id=approval_id)


def get_approval(db: sqlite3.Connection, *, tenant_id: str, approval_id: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM operator_approvals WHERE id=? AND tenant_id=?",
        (approval_id, tenant_id),
    ).fetchone()
    if row is None:
        raise OperatorNotFound("Operator approval not found.")
    return row


def resolve_approval_in_store(
    db: sqlite3.Connection, *, tenant_id: str, approval_id: str,
    decision: str, resolved_by: str, note: str,
) -> sqlite3.Row:
    if decision not in {"approved", "denied"}:
        raise OperatorValidationError("Approval decision must be 'approved' or 'denied'.")
    row = get_approval(db, tenant_id=tenant_id, approval_id=approval_id)
    if str(row["status"]) != "pending":
        raise OperatorConflict("Only a pending approval can be resolved.")
    db.execute(
        """UPDATE operator_approvals SET status=?,resolved_by=?,resolution_note=?,
               resolved_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?""",
        (decision, resolved_by[:160], redact_text(note, limit=2000), approval_id, tenant_id),
    )
    return get_approval(db, tenant_id=tenant_id, approval_id=approval_id)


def latest_pending_approval_for_run(
    db: sqlite3.Connection, run_id: str, *, action: str | None = None,
) -> sqlite3.Row | None:
    if action is None:
        return db.execute(
            """SELECT * FROM operator_approvals WHERE run_id=? AND status='pending'
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
    return db.execute(
        """SELECT * FROM operator_approvals WHERE run_id=? AND action=? AND status='pending'
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (run_id, action),
    ).fetchone()


def arguments_fingerprint(arguments: Any) -> str:
    """Stable fingerprint of the exact arguments a human approved.

    Canonical JSON (sorted keys, no incidental whitespace) so that logically
    identical arguments hash identically regardless of key order.
    """
    try:
        canonical = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(arguments)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approved_arguments_for_tool(
    db: sqlite3.Connection, run_id: str, *, action: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    """Recover the exact approved argument set for a resumed tool call.

    A channel/model may omit fields or accidentally send changed fields after
    approval.  The approval is bound to the tool action and its stored
    arguments, so the approved payload must win rather than creating a second
    approval for a model-generated variant.
    """
    rows = db.execute(
        """SELECT id, action, payload_ref_json FROM operator_approvals
           WHERE run_id=? AND status='approved' AND consumed_at IS NULL
           ORDER BY created_at ASC""",
        (run_id,),
    ).fetchall()

    for row in rows:
        try:
            payload = json.loads(row["payload_ref_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        row_action = str(row["action"] or "")
        exact_action = row_action == action
        bridged_action = (
            not exact_action
            and not row_action.startswith("tool:")
            and str(payload.get("tool") or "") == action.removeprefix("tool:")
        )
        if not exact_action and not bridged_action:
            continue

        approved_args = payload.get("arguments")
        if not isinstance(approved_args, dict):
            step = db.execute(
                """SELECT decision_json FROM operator_run_steps
                   WHERE approval_id=? AND step_type='approval_request'
                   ORDER BY sequence DESC LIMIT 1""",
                (str(row["id"]),),
            ).fetchone()
            if step is not None:
                try:
                    decision = json.loads(step["decision_json"] or "{}")
                except (TypeError, ValueError):
                    decision = {}
                candidate = decision.get("arguments") if isinstance(decision, dict) else None
                approved_args = candidate if isinstance(candidate, dict) else None
        if not isinstance(approved_args, dict):
            continue
        # Step/approval payloads are redacted before persistence. A redacted
        # secret is not a usable replacement payload; keep the resumed model
        # values and let claim_approved_approval() verify their exact
        # fingerprint against the immutable approval record. Non-secret
        # arguments remain fully pinned and are returned verbatim.
        def contains_redaction(value: Any) -> bool:
            if value == "[REDACTED]":
                return True
            if isinstance(value, dict):
                return any(contains_redaction(item) for item in value.values())
            if isinstance(value, list):
                return any(contains_redaction(item) for item in value)
            return False
        if contains_redaction(approved_args):
            continue
        return dict(approved_args)
    return None


def approved_tool_decision_for_run(db: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """Return the exact tool decision behind the next approved write.

    The approval request step already stores the model decision that created
    the gate. Reusing it lets a wake-up invoke the pinned action directly,
    without spending another model turn to reconstruct the same call.
    """
    rows = db.execute(
        """SELECT id, action FROM operator_approvals
           WHERE run_id=? AND status='approved' AND consumed_at IS NULL
           ORDER BY created_at ASC, id ASC""",
        (run_id,),
    ).fetchall()
    for row in rows:
        action = str(row["action"] or "")
        if not action.startswith("tool:"):
            continue
        step = db.execute(
            """SELECT decision_json FROM operator_run_steps
               WHERE approval_id=? AND step_type='approval_request'
               ORDER BY sequence DESC LIMIT 1""",
            (str(row["id"]),),
        ).fetchone()
        if step is None:
            continue
        try:
            decision = json.loads(step["decision_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            decision = {}
        if not isinstance(decision, dict):
            continue
        tool = str(decision.get("tool") or action.removeprefix("tool:")).strip()
        arguments = decision.get("arguments")
        # Secret-bearing decisions are intentionally redacted in durable step
        # rows. They must use the existing model-resume path, where the
        # channel/runtime can provide the original secret without persisting it
        # in the approval record or audit trail.
        def contains_redaction(value: Any) -> bool:
            if value == "[REDACTED]":
                return True
            if isinstance(value, dict):
                return any(contains_redaction(item) for item in value.values())
            if isinstance(value, list):
                return any(contains_redaction(item) for item in value)
            return False
        if tool and isinstance(arguments, dict) and not contains_redaction(arguments):
            resumed = {"type": "tool_call", "tool": tool, "arguments": arguments,
                       "message": "approved pinned invocation"}
            # Preserve the opaque action-plan step marker through approval.
            # It contains no credentials and lets the deterministic reducer
            # advance the plan after the approved mutation completes.
            plan_step_id = str(decision.get("_plan_step_id") or "").strip()
            if plan_step_id:
                resumed["_plan_step_id"] = plan_step_id
            return resumed
    return None


def claim_approved_approval(
    db: sqlite3.Connection, run_id: str, *, action: str, fingerprint: str,
) -> bool:
    """Consume one approval that authorises exactly these arguments.

    An approval authorises a single execution of a specific tool call. It must
    therefore match on all three of run, action, and argument fingerprint, and
    it is consumed on use, so that:

    * a human who approved ``publish(draft_123)`` has not also approved
      ``publish(everything_else)`` -- the fingerprint differs; and
    * one approval cannot be replayed for repeated executions within the run.

    Approvals recorded before argument pinning existed carry no fingerprint;
    those are matched on run+action alone so in-flight runs approved under the
    old behaviour still resolve rather than dead-locking after an upgrade.
    Returns True only if an approval was claimed.
    """
    # Normally the approval action is the exact ``tool:<name>`` action.  A
    # channel/model may also have requested an explicit business action first
    # (for example ``publish_buffer_test_post``) and then repeated the exact
    # tool call after resume.  Those approvals are bridged only when their
    # pinned payload names this exact tool and carries the same argument
    # fingerprint; a generic approval can never authorize an unrelated write.
    rows = db.execute(
        """SELECT id, action, payload_ref_json FROM operator_approvals
           WHERE run_id=? AND status='approved' AND consumed_at IS NULL
           ORDER BY created_at ASC""",
        (run_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_ref_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        row_action = str(row["action"] or "")
        exact_action = row_action == action
        bridged_action = (
            not exact_action
            and not row_action.startswith("tool:")
            and str(payload.get("tool") or "") == action.removeprefix("tool:")
        )
        if not exact_action and not bridged_action:
            continue
        approved_fingerprint = str(payload.get("arguments_fingerprint") or "")
        if approved_fingerprint and approved_fingerprint != fingerprint:
            continue
        claimed = db.execute(
            """UPDATE operator_approvals SET consumed_at=CURRENT_TIMESTAMP
               WHERE id=? AND consumed_at IS NULL""",
            (str(row["id"]),),
        )
        if claimed.rowcount:
            return True
    return False


def approval_public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "tenant_id": str(row["tenant_id"]),
        "project_id": str(row["project_id"]),
        "action": str(row["action"]),
        "summary": str(row["summary"] or ""),
        "payload_ref": json.loads(str(row["payload_ref_json"] or "{}")),
        "status": str(row["status"]),
        "requested_by": str(row["requested_by"] or ""),
        "resolved_by": str(row["resolved_by"] or ""),
        "resolution_note": str(row["resolution_note"] or ""),
        "created_at": str(row["created_at"]),
        "resolved_at": row["resolved_at"],
    }
