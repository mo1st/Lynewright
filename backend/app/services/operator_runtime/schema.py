"""Persistent schema for the internal BYOK Operator Agent.

The executable, idempotent migration lives here and runs from
``app.database._initialize_database`` (this repository never executes
``migrations/*.sql`` at runtime). ``app/migrations/075_operator_runtime.sql``
mirrors this file as a reviewable reference.
"""
from __future__ import annotations

import sqlite3

RUN_STATES = (
    "queued", "running", "waiting_tool", "waiting_job", "waiting_approval",
    "sleeping", "completed", "failed", "cancelled",
)

TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})

_STEP_TYPES = (
    "model_decision", "tool_call", "wait", "resume", "failure", "finish",
    "approval_request", "action_plan",
)
_STEP_STATUSES = ("succeeded", "failed")
_APPROVAL_STATUSES = ("pending", "approved", "denied", "cancelled")
_WORKFLOW_STATUSES = ("draft", "reviewed", "enabled", "stale")


def _states_sql(states: tuple[str, ...]) -> str:
    return ", ".join(f"'{state}'" for state in states)


def init_operator_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS operator_profiles (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            instructions TEXT NOT NULL DEFAULT '',
            granted_capabilities_json TEXT NOT NULL DEFAULT '[]',
            max_steps INTEGER NOT NULL DEFAULT 12,
            max_consecutive_tool_failures INTEGER NOT NULL DEFAULT 3,
            max_model_failures INTEGER NOT NULL DEFAULT 2,
            max_total_tokens INTEGER NOT NULL DEFAULT 0,
            max_model_decisions INTEGER NOT NULL DEFAULT 0,
            max_network_attempts INTEGER NOT NULL DEFAULT 0,
            no_progress_limit INTEGER NOT NULL DEFAULT 3,
            retrieval_mode TEXT NOT NULL DEFAULT 'hybrid_local',
            retrieval_canary_percent INTEGER NOT NULL DEFAULT 10,
            decision_policy TEXT NOT NULL DEFAULT 'cloud_only',
            needle_canary_percent INTEGER NOT NULL DEFAULT 0,
            needle_read_confidence REAL NOT NULL DEFAULT 0.92,
            needle_write_confidence REAL NOT NULL DEFAULT 0.97,
            needle_sensitive_confidence REAL NOT NULL DEFAULT 0.99,
            archived_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tenant_id, name)
        );

        CREATE TABLE IF NOT EXISTS operator_runs (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL REFERENCES operator_profiles(id),
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ({_states_sql(RUN_STATES)})),
            summary TEXT NOT NULL DEFAULT '',
            context_json TEXT NOT NULL DEFAULT '{{}}',
            current_step INTEGER NOT NULL DEFAULT 0,
            consecutive_tool_failures INTEGER NOT NULL DEFAULT 0,
            model_failures INTEGER NOT NULL DEFAULT 0,
            wake_reason TEXT NOT NULL DEFAULT '',
            wake_at TEXT,
            pending_job_id TEXT NOT NULL DEFAULT '',
            pending_operation_json TEXT NOT NULL DEFAULT '',
            pending_approval_id TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            error_summary TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT NOT NULL DEFAULT '',
            last_provider TEXT NOT NULL DEFAULT '',
            last_protocol TEXT NOT NULL DEFAULT '',
            last_model TEXT NOT NULL DEFAULT '',
            decision_policy TEXT NOT NULL DEFAULT 'cloud_only',
            needle_read_confidence REAL NOT NULL DEFAULT 0.92,
            needle_write_confidence REAL NOT NULL DEFAULT 0.97,
            needle_sensitive_confidence REAL NOT NULL DEFAULT 0.99,
            needle_canary_percent INTEGER NOT NULL DEFAULT 0,
            decision_model_checksum TEXT NOT NULL DEFAULT '',
            tool_schema_fingerprint TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            completed_at TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_operator_run_idempotency
            ON operator_runs(tenant_id, profile_id, idempotency_key)
            WHERE idempotency_key != '';
        CREATE INDEX IF NOT EXISTS idx_operator_runs_scope
            ON operator_runs(tenant_id, project_id, status, updated_at DESC);

        -- A run may be resumed by the API, the runtime worker, or a channel
        -- callback.  A revision CAS protects individual writes, but it cannot
        -- stop two executors from both purchasing a model response before one
        -- of them loses that CAS.  This short, fenced lease is the admission
        -- lock for that expensive external side effect.
        CREATE TABLE IF NOT EXISTS operator_run_executor_leases (
            run_id TEXT PRIMARY KEY REFERENCES operator_runs(id) ON DELETE CASCADE,
            owner_id TEXT NOT NULL,
            fencing_token TEXT NOT NULL,
            epoch INTEGER NOT NULL DEFAULT 1,
            expires_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS operator_workflow_drafts (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            objective_template TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            contract_revisions_json TEXT NOT NULL DEFAULT '{{}}',
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ({_states_sql(_WORKFLOW_STATUSES)})),
            version INTEGER NOT NULL DEFAULT 1,
            reviewed_by TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tenant_id, project_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_operator_workflow_scope
            ON operator_workflow_drafts(tenant_id, project_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_operator_executor_leases_expiry
            ON operator_run_executor_leases(expires_at);

        CREATE TABLE IF NOT EXISTS operator_run_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES operator_runs(id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            step_type TEXT NOT NULL CHECK (step_type IN ({_states_sql(_STEP_TYPES)})),
            status TEXT NOT NULL DEFAULT 'succeeded' CHECK (status IN ({_states_sql(_STEP_STATUSES)})),
            tool_name TEXT NOT NULL DEFAULT '',
            decision_json TEXT NOT NULL DEFAULT '{{}}',
            input_json TEXT NOT NULL DEFAULT '{{}}',
            output_json TEXT NOT NULL DEFAULT '{{}}',
            job_id TEXT,
            approval_id TEXT,
            provider TEXT NOT NULL DEFAULT '',
            protocol TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            usage_exact INTEGER NOT NULL DEFAULT 0,
            provider_attempts INTEGER NOT NULL DEFAULT 0,
            prompt_chars INTEGER NOT NULL DEFAULT 0,
            decision_backend TEXT NOT NULL DEFAULT '',
            decision_confidence REAL,
            decision_risk_class TEXT NOT NULL DEFAULT '',
            needle_latency_ms INTEGER NOT NULL DEFAULT 0,
            needle_schema_fingerprint TEXT NOT NULL DEFAULT '',
            evidence_validation_result TEXT NOT NULL DEFAULT '',
            sensitive_verification_required INTEGER NOT NULL DEFAULT 0,
            fallback_reason TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            error_code TEXT NOT NULL DEFAULT '',
            error_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (run_id, sequence)
        );

        CREATE INDEX IF NOT EXISTS idx_operator_run_steps_run
            ON operator_run_steps(run_id, sequence);

        -- Authoritative outputs for deterministic action-plan bindings.  The
        -- step audit row remains compact for prompts/UI; bindings resolve
        -- through this scoped reference instead of the shortened projection.
        CREATE TABLE IF NOT EXISTS operator_run_result_refs (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES operator_runs(id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            schema_revision TEXT NOT NULL DEFAULT '',
            result_json TEXT NOT NULL,
            result_digest TEXT NOT NULL,
            truncated INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (run_id, step_id)
        );
        CREATE INDEX IF NOT EXISTS idx_operator_run_result_refs_run
            ON operator_run_result_refs(run_id, step_id);

        CREATE TABLE IF NOT EXISTS operator_ai_attempts (
            attempt_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES operator_runs(id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            decision_sequence INTEGER NOT NULL DEFAULT 0,
            ordinal INTEGER NOT NULL DEFAULT 0,
            provider TEXT NOT NULL DEFAULT '',
            profile_id TEXT NOT NULL DEFAULT '',
            protocol TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            network_attempt INTEGER NOT NULL DEFAULT 0,
            outcome TEXT NOT NULL DEFAULT '',
            failure_kind TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            http_attempts INTEGER NOT NULL DEFAULT 0,
            prompt_chars INTEGER NOT NULL DEFAULT 0,
            usage_exact INTEGER NOT NULL DEFAULT 0,
            usage_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (run_id, decision_sequence, ordinal)
        );

        CREATE INDEX IF NOT EXISTS idx_operator_ai_attempts_run
            ON operator_ai_attempts(run_id, decision_sequence, ordinal);

        CREATE TABLE IF NOT EXISTS operator_approvals (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES operator_runs(id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            action TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            payload_ref_json TEXT NOT NULL DEFAULT '{{}}',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ({_states_sql(_APPROVAL_STATUSES)})),
            requested_by TEXT NOT NULL DEFAULT '',
            resolved_by TEXT NOT NULL DEFAULT '',
            resolution_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            consumed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_operator_approvals_scope
            ON operator_approvals(tenant_id, status, created_at DESC);
        """
    )
    _migrate_action_plan_step_type(db)
    # Retrieval is an additive sidecar to Operator state.  Its initializer
    # degrades to normal-table lexical search if a downstream SQLite build
    # lacks FTS5, so startup never depends on an optional extension.
    from app.services.operator_retrieval import init_operator_retrieval_schema
    init_operator_retrieval_schema(db)
    # Additive for databases created before approvals became single-use.
    # An approval is claimed exactly once; consumed_at records when.
    approval_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(operator_approvals)").fetchall()
    }
    if "consumed_at" not in approval_columns:
        db.execute("ALTER TABLE operator_approvals ADD COLUMN consumed_at TEXT")

    profile_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(operator_profiles)").fetchall()
    }
    for name, declaration in (
        ("max_total_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("max_model_decisions", "INTEGER NOT NULL DEFAULT 0"),
        ("max_network_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("no_progress_limit", "INTEGER NOT NULL DEFAULT 3"),
        ("retrieval_mode", "TEXT NOT NULL DEFAULT 'hybrid_local'"),
        ("retrieval_canary_percent", "INTEGER NOT NULL DEFAULT 10"),
        ("decision_policy", "TEXT NOT NULL DEFAULT 'cloud_only'"),
        ("needle_canary_percent", "INTEGER NOT NULL DEFAULT 0"),
        ("needle_read_confidence", "REAL NOT NULL DEFAULT 0.92"),
        ("needle_write_confidence", "REAL NOT NULL DEFAULT 0.97"),
        ("needle_sensitive_confidence", "REAL NOT NULL DEFAULT 0.99"),
        ("archived_at", "TEXT"),
    ):
        if name not in profile_columns:
            db.execute(f"ALTER TABLE operator_profiles ADD COLUMN {name} {declaration}")

    run_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(operator_runs)").fetchall()
    }
    if "pending_operation_json" not in run_columns:
        db.execute("ALTER TABLE operator_runs ADD COLUMN pending_operation_json TEXT NOT NULL DEFAULT ''")
    for name, declaration in (
        ("decision_policy", "TEXT NOT NULL DEFAULT 'cloud_only'"),
        ("needle_read_confidence", "REAL NOT NULL DEFAULT 0.92"),
        ("needle_write_confidence", "REAL NOT NULL DEFAULT 0.97"),
        ("needle_sensitive_confidence", "REAL NOT NULL DEFAULT 0.99"),
        ("needle_canary_percent", "INTEGER NOT NULL DEFAULT 0"),
        ("decision_model_checksum", "TEXT NOT NULL DEFAULT ''"),
        ("tool_schema_fingerprint", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in run_columns:
            db.execute(f"ALTER TABLE operator_runs ADD COLUMN {name} {declaration}")

    attempt_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(operator_ai_attempts)").fetchall()
    }
    if "http_attempts" not in attempt_columns:
        db.execute("ALTER TABLE operator_ai_attempts ADD COLUMN http_attempts INTEGER NOT NULL DEFAULT 0")

    # Additive token accounting for model decision steps. Older deployments
    # keep all existing run/step history; zero values mean that the provider
    # did not expose usage metadata for that step.
    step_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(operator_run_steps)").fetchall()
    }
    for name, declaration in (
        ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cached_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("usage_exact", "INTEGER NOT NULL DEFAULT 0"),
        ("provider_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("prompt_chars", "INTEGER NOT NULL DEFAULT 0"),
        ("decision_backend", "TEXT NOT NULL DEFAULT ''"),
        ("decision_confidence", "REAL"),
        ("decision_risk_class", "TEXT NOT NULL DEFAULT ''"),
        ("needle_latency_ms", "INTEGER NOT NULL DEFAULT 0"),
        ("needle_schema_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("evidence_validation_result", "TEXT NOT NULL DEFAULT ''"),
        ("sensitive_verification_required", "INTEGER NOT NULL DEFAULT 0"),
        ("fallback_reason", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in step_columns:
            db.execute(f"ALTER TABLE operator_run_steps ADD COLUMN {name} {declaration}")


def _migrate_action_plan_step_type(db: sqlite3.Connection) -> None:
    """Rebuild the legacy SQLite CHECK constraint without losing run history.

    SQLite cannot alter a CHECK constraint in place.  Older Lynewright databases
    therefore need a one-time table rebuild before the reducer can persist its
    data-only ``action_plan`` audit row.  The copy is column-intersection based
    so databases from any earlier additive schema remain readable.
    """
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='operator_run_steps'"
    ).fetchone()
    if row is None or "action_plan" in str(row[0] or "").lower():
        return
    db.execute("DROP INDEX IF EXISTS idx_operator_run_steps_run")
    db.execute("ALTER TABLE operator_run_steps RENAME TO operator_run_steps_legacy")
    db.execute(
        f"""
        CREATE TABLE operator_run_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES operator_runs(id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            step_type TEXT NOT NULL CHECK (step_type IN ({_states_sql(_STEP_TYPES)})),
            status TEXT NOT NULL DEFAULT 'succeeded' CHECK (status IN ({_states_sql(_STEP_STATUSES)})),
            tool_name TEXT NOT NULL DEFAULT '',
            decision_json TEXT NOT NULL DEFAULT '{{}}',
            input_json TEXT NOT NULL DEFAULT '{{}}',
            output_json TEXT NOT NULL DEFAULT '{{}}',
            job_id TEXT,
            approval_id TEXT,
            provider TEXT NOT NULL DEFAULT '',
            protocol TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            usage_exact INTEGER NOT NULL DEFAULT 0,
            provider_attempts INTEGER NOT NULL DEFAULT 0,
            prompt_chars INTEGER NOT NULL DEFAULT 0,
            decision_backend TEXT NOT NULL DEFAULT '',
            decision_confidence REAL,
            decision_risk_class TEXT NOT NULL DEFAULT '',
            needle_latency_ms INTEGER NOT NULL DEFAULT 0,
            needle_schema_fingerprint TEXT NOT NULL DEFAULT '',
            evidence_validation_result TEXT NOT NULL DEFAULT '',
            sensitive_verification_required INTEGER NOT NULL DEFAULT 0,
            fallback_reason TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            error_code TEXT NOT NULL DEFAULT '',
            error_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (run_id, sequence)
        )
        """
    )
    new_columns = [
        str(item[1]) for item in db.execute("PRAGMA table_info(operator_run_steps)").fetchall()
    ]
    old_columns = {
        str(item[1]) for item in db.execute("PRAGMA table_info(operator_run_steps_legacy)").fetchall()
    }
    columns = [name for name in new_columns if name in old_columns]
    if columns:
        names = ", ".join(columns)
        db.execute(
            f"INSERT INTO operator_run_steps ({names}) SELECT {names} FROM operator_run_steps_legacy"
        )
    db.execute("DROP TABLE operator_run_steps_legacy")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_operator_run_steps_run "
        "ON operator_run_steps(run_id, sequence)"
    )
