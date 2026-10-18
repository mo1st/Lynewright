"""Bounded operator overrides actually take effect, and approvals are pinned.

Two follow-ups to the operator activation build:

1. ``BOUNDED_SETTINGS`` was empty, and registering keys would have been theatre
   because nothing read ``platform_settings`` by name. These tests prove a
   bounded change now reaches the runtime, and that an out-of-envelope value
   cannot move the runtime even if it reaches storage by another route.
2. Approvals were matched on ``(run_id, tool)`` alone, with the arguments never
   recorded and the approval never consumed. These tests prove an approval
   authorises exactly one execution of exactly the approved arguments.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from app.database import get_connection, init_db
from app.services.operator_runtime import store as opstore
from app.services.operator_settings_bounds import BOUNDED_SETTINGS


# --------------------------------------------------------------- bounded knobs

OPERATOR_KEYS = (
    "platform.operator.automation_tick_seconds",
    "platform.operator.objectives_per_tick",
    "platform.operator.interactions_per_tick",
    "platform.operator.max_tenants_per_tick",
    "platform.operator.remediation_max_attempts",
)


def _store_setting(key: str, value: object) -> None:
    init_db()
    with closing(get_connection()) as db, db:
        db.execute(
            "INSERT INTO platform_settings (key,value_json) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (key, json.dumps(value)),
        )


def _clear_stored_operator_keys() -> None:
    """The SQLite file persists across runs; start every test from env defaults."""
    init_db()
    with closing(get_connection()) as db, db:
        placeholders = ",".join("?" for _ in OPERATOR_KEYS)
        db.execute(
            f"DELETE FROM platform_settings WHERE key IN ({placeholders})", OPERATOR_KEYS
        )


@pytest.fixture(autouse=True)
def _isolated_overrides():
    from app.services.operator_runtime_overrides import invalidate_override_cache

    _clear_stored_operator_keys()
    invalidate_override_cache()
    yield
    _clear_stored_operator_keys()
    invalidate_override_cache()


def test_operator_knobs_are_registered_as_bounded_and_writable() -> None:
    registered = {item.key for item in BOUNDED_SETTINGS.list()}
    for key in OPERATOR_KEYS:
        assert key in registered, f"{key} must be operator-writable"
        assert BOUNDED_SETTINGS.is_operator_writable(key)


def test_enable_flags_are_not_operator_writable() -> None:
    """An agent must not be able to switch its own execution or oversight."""
    for forbidden in (
        "platform.operator.automation_enabled",
        "platform.operator.dispatch_enabled",
    ):
        assert not BOUNDED_SETTINGS.is_operator_writable(forbidden)


def test_stored_override_changes_the_effective_runtime_value() -> None:
    """The whole point: a bounded write must actually reach the runtime."""
    from app.services.operator_runtime_overrides import effective_int, invalidate_override_cache

    assert effective_int("platform.operator.objectives_per_tick", 5) == 5
    _store_setting("platform.operator.objectives_per_tick", 21)
    invalidate_override_cache()
    assert effective_int("platform.operator.objectives_per_tick", 5) == 21


def test_out_of_bounds_stored_value_is_clamped_on_read() -> None:
    """Defence in depth: a value that reaches storage by another route
    (admin, migration, direct SQL) still cannot move the runtime."""
    from app.services.operator_runtime_overrides import effective_int, invalidate_override_cache

    _store_setting("platform.operator.objectives_per_tick", 10_000)  # max is 100
    invalidate_override_cache()
    assert effective_int("platform.operator.objectives_per_tick", 5) == 5, (
        "an out-of-envelope stored value must fall back to the environment default"
    )


def test_unregistered_key_never_overrides_anything() -> None:
    from app.services.operator_runtime_overrides import effective_int, invalidate_override_cache

    _store_setting("platform.operator.not_a_real_knob", 999)
    invalidate_override_cache()
    assert effective_int("platform.operator.not_a_real_knob", 7) == 7


def test_settings_write_invalidates_the_override_cache() -> None:
    """A bounded change is visible on the next tick, not after a restart."""
    from app.services.operator_runtime_overrides import effective_int, invalidate_override_cache
    from app.services.platform_services import PlatformStore, SettingsService

    init_db()
    _store_setting("platform.operator.interactions_per_tick", 11)
    invalidate_override_cache()
    assert effective_int("platform.operator.interactions_per_tick", 10) == 11  # cache warm at 11

    # No manual invalidation here: the write itself must drop the cache.
    SettingsService(PlatformStore()).set(
        {"key": "platform.operator.interactions_per_tick", "value": 43}
    )
    assert effective_int("platform.operator.interactions_per_tick", 10) == 43


# ------------------------------------------------------- approval arg pinning

def _approvals_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE operator_approvals (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '', action TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '', payload_ref_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending', requested_by TEXT NOT NULL DEFAULT '',
            resolved_by TEXT NOT NULL DEFAULT '', resolution_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT,
            consumed_at TEXT
        );
        """
    )
    return db


def _approve(db: sqlite3.Connection, *, approval_id: str, action: str, arguments: object) -> None:
    db.execute(
        """INSERT INTO operator_approvals (id, run_id, action, payload_ref_json, status)
           VALUES (?, 'run_1', ?, ?, 'approved')""",
        (approval_id, action, json.dumps(
            {"tool": action.split(":", 1)[-1],
             "arguments_fingerprint": opstore.arguments_fingerprint(arguments)}
        )),
    )


def test_fingerprint_is_stable_across_key_order() -> None:
    assert opstore.arguments_fingerprint({"a": 1, "b": 2}) == \
        opstore.arguments_fingerprint({"b": 2, "a": 1})
    assert opstore.arguments_fingerprint({"a": 1}) != opstore.arguments_fingerprint({"a": 2})


def test_approval_authorises_only_the_approved_arguments() -> None:
    """Approving publish(draft_123) must not authorise publish(everything)."""
    db = _approvals_db()
    approved_args = {"target": "draft_123"}
    _approve(db, approval_id="ap_1", action="tool:lynewright.publish", arguments=approved_args)

    assert opstore.claim_approved_approval(
        db, "run_1", action="tool:lynewright.publish",
        fingerprint=opstore.arguments_fingerprint({"target": "everything"}),
    ) is False, "different arguments must not be authorised by this approval"

    # The rejected attempt must not have consumed the approval either.
    row = db.execute("SELECT consumed_at FROM operator_approvals WHERE id='ap_1'").fetchone()
    assert row["consumed_at"] is None

    assert opstore.claim_approved_approval(
        db, "run_1", action="tool:lynewright.publish",
        fingerprint=opstore.arguments_fingerprint(approved_args),
    ) is True


def test_approval_is_single_use() -> None:
    """One approval authorises one execution, not unlimited replays."""
    db = _approvals_db()
    args = {"target": "draft_123"}
    _approve(db, approval_id="ap_1", action="tool:lynewright.publish", arguments=args)
    fingerprint = opstore.arguments_fingerprint(args)

    assert opstore.claim_approved_approval(
        db, "run_1", action="tool:lynewright.publish", fingerprint=fingerprint) is True
    assert opstore.claim_approved_approval(
        db, "run_1", action="tool:lynewright.publish", fingerprint=fingerprint) is False, \
        "an approval must not be replayable after it has been consumed"

    row = db.execute("SELECT consumed_at FROM operator_approvals WHERE id='ap_1'").fetchone()
    assert row["consumed_at"] is not None


def test_pending_or_denied_approvals_are_never_claimable() -> None:
    db = _approvals_db()
    args = {"target": "draft_123"}
    fingerprint = opstore.arguments_fingerprint(args)
    for approval_id, status in (("ap_pending", "pending"), ("ap_denied", "denied")):
        db.execute(
            """INSERT INTO operator_approvals (id, run_id, action, payload_ref_json, status)
               VALUES (?, 'run_1', 'tool:lynewright.publish', ?, ?)""",
            (approval_id, json.dumps({"arguments_fingerprint": fingerprint}), status),
        )
    assert opstore.claim_approved_approval(
        db, "run_1", action="tool:lynewright.publish", fingerprint=fingerprint) is False


def test_approval_does_not_cross_runs() -> None:
    db = _approvals_db()
    args = {"target": "draft_123"}
    _approve(db, approval_id="ap_1", action="tool:lynewright.publish", arguments=args)
    assert opstore.claim_approved_approval(
        db, "run_OTHER", action="tool:lynewright.publish",
        fingerprint=opstore.arguments_fingerprint(args)) is False


def test_legacy_approval_without_fingerprint_still_resolves_once() -> None:
    """Runs approved before pinning existed must not dead-lock on upgrade —
    but they are still consumed, so they cannot be replayed."""
    db = _approvals_db()
    db.execute(
        """INSERT INTO operator_approvals (id, run_id, action, payload_ref_json, status)
           VALUES ('ap_legacy', 'run_1', 'tool:lynewright.publish', ?, 'approved')""",
        (json.dumps({"tool": "lynewright.publish"}),),  # no arguments_fingerprint
    )
    fingerprint = opstore.arguments_fingerprint({"anything": True})
    assert opstore.claim_approved_approval(
        db, "run_1", action="tool:lynewright.publish", fingerprint=fingerprint) is True
    assert opstore.claim_approved_approval(
        db, "run_1", action="tool:lynewright.publish", fingerprint=fingerprint) is False


def test_approved_arguments_restore_omitted_provider_fields() -> None:
    """A resumed channel call may omit fields already shown in the approval."""
    db = _approvals_db()
    db.execute(
        """CREATE TABLE operator_run_steps (
            approval_id TEXT, step_type TEXT, sequence INTEGER, decision_json TEXT
        )"""
    )
    approved = {
        "project_id": "demo-project", "account_id": "acct_1",
        "connection_id": "primary", "integration_id": "buffer_1",
        "content": "test", "idempotency_key": "operator-test-001",
    }
    db.execute(
        """INSERT INTO operator_approvals
           (id, run_id, action, payload_ref_json, status)
           VALUES ('ap_subset', 'run_1', 'tool:lynewright.publish',
                   '{\"arguments_fingerprint\": \"unused\"}', 'approved')"""
    )
    db.execute(
        """INSERT INTO operator_run_steps
           (approval_id, step_type, sequence, decision_json)
           VALUES ('ap_subset', 'approval_request', 1, ?)""",
        (json.dumps({"arguments": approved}),),
    )
    generated = {
        "project_id": "demo-project", "account_id": "acct_1",
        "content": "test", "idempotency_key": "operator-test-001",
    }
    assert opstore.approved_arguments_for_tool(
        db, "run_1", action="tool:lynewright.publish", arguments=generated,
    ) == approved

    # A resumed model is not allowed to change the approved target/content;
    # the exact frozen payload is restored instead of creating a second prompt.
    changed = {**generated, "content": "different-after-approval"}
    assert opstore.approved_arguments_for_tool(
        db, "run_1", action="tool:lynewright.publish", arguments=changed,
    ) == approved
