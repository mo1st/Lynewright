"""Operator automation: scheduled, event, and condition triggers (Session E).

E owns triggers and schedules only. The flow is always:

    trigger -> reserve durable firing -> persist operator objective request
            -> (future integration) Operator wakes -> works -> persists -> sleeps

No LLM is needed to stay alive: every decision here is deterministic SQLite +
pure functions. Reuses the existing platform store/event patterns rather than
introducing a second scheduler engine — one small asyncio tick loop joins the
other background schedulers in ``main.py``.

Firing dedupe contract: a firing row with UNIQUE(tenant_id, automation_id,
trigger_identity) is reserved *before* any side effect. Scheduler restarts,
event redelivery, and multi-process ticks therefore cannot duplicate an
objective request. Schedule identities are the due epoch; event identities are
the platform event id; condition identities are the rising-edge timestamp.
"""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from app.services.operator_conditions import (
    evaluate_conditions,
    register_default_resolvers,
    resolve_metrics,
    validate_condition_spec,
)
from app.services.operator_objectives import OperatorObjectiveService
from app.services.operator_schedule import (
    advance_due_at,
    describe_schedule,
    initial_due_at,
    parse_schedule,
)
from app.services.platform_capabilities import CapabilityActor
from app.services.platform_services import (
    EventService,
    PlatformNotFoundError,
    PlatformStore,
    PlatformValidationError,
)
from app.services.tenant_schema import DEFAULT_TENANT_ID


TRIGGER_TYPES: tuple[str, ...] = ("schedule", "event", "condition")
FIRING_DISPATCHED = "dispatched"
FIRING_SUPPRESSED_COOLDOWN = "suppressed_cooldown"
FIRING_SKIPPED_MISSED = "skipped_missed"
FIRING_FAILED = "failed"
MAX_COOLDOWN_SECONDS = 7 * 24 * 3600
_MAX_PATTERNS = 8
_MAX_TEMPLATE_CHARS = 8_000
_MAX_CATCHUP_WINDOWS = 50
_MAX_ACTION_PAYLOAD_BYTES = 4 * 1024
DeliveryHook = Callable[..., None]
_delivery_hook: DeliveryHook | None = None


def register_delivery_hook(hook: DeliveryHook) -> Callable[[], bool]:
    """Register optional delivery for automation failures."""
    global _delivery_hook
    if not callable(hook):
        raise TypeError("Delivery hook must be callable.")
    previous = _delivery_hook
    _delivery_hook = hook

    def unregister() -> bool:
        global _delivery_hook
        if _delivery_hook is not hook:
            return False
        _delivery_hook = previous
        return True

    return unregister


class _RenderValues(dict):
    """format_map helper: unknown placeholders stay literal instead of raising."""

    def __missing__(self, key: str) -> str:  # pragma: no cover - trivial
        return "{" + key + "}"


class OperatorAutomationService:
    """CRUD + trigger evaluation for operator automations."""

    def __init__(
        self,
        store: PlatformStore | None = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        objectives: OperatorObjectiveService | None = None,
        events: EventService | None = None,
        now: Callable[[], float] = time.time,
        metric_resolvers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
    ) -> None:
        self.store = store or PlatformStore()
        self.tenant_id = tenant_id
        self.now = now
        self.events = events or EventService(self.store, tenant_id=self.tenant_id)
        self.objectives = objectives or OperatorObjectiveService(
            self.store, tenant_id=self.tenant_id, events=self.events
        )
        self.metric_resolvers = metric_resolvers if metric_resolvers is not None else {}
        if metric_resolvers is None:
            register_default_resolvers(self.metric_resolvers, self.store, tenant_id=self.tenant_id)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self.store.connection() as db:
            self.ensure_schema(db)
            yield db

    @staticmethod
    def ensure_schema(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS operator_automations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                trigger_type TEXT NOT NULL,
                trigger_config_json TEXT NOT NULL,
                objective_template TEXT NOT NULL,
                action_json TEXT NOT NULL DEFAULT '',
                objective_title TEXT NOT NULL DEFAULT '',
                notification_policy_json TEXT NOT NULL DEFAULT '{}',
                cooldown_seconds INTEGER NOT NULL DEFAULT 0,
                last_triggered_at TEXT,
                last_triggered_epoch REAL,
                next_due_at_epoch REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_operator_automations_scope
                ON operator_automations(tenant_id, project_id, enabled, trigger_type);
            CREATE TABLE IF NOT EXISTS operator_automation_firings (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                automation_id TEXT NOT NULL,
                trigger_identity TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                request_id TEXT,
                triggered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, automation_id, trigger_identity)
            );
            CREATE INDEX IF NOT EXISTS idx_operator_firings_audit
                ON operator_automation_firings(tenant_id, automation_id, triggered_at DESC);
            CREATE TABLE IF NOT EXISTS operator_automation_state (
                tenant_id TEXT NOT NULL,
                automation_id TEXT NOT NULL,
                state_key TEXT NOT NULL,
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, automation_id, state_key)
            );
            """
        )
        # Deployed databases created before last_triggered_epoch existed keep
        # their rows; cooldown logic treats a missing epoch as "never fired".
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(operator_automations)").fetchall()}
        if "last_triggered_epoch" not in columns:
            db.execute("ALTER TABLE operator_automations ADD COLUMN last_triggered_epoch REAL")
        if "action_json" not in columns:
            db.execute("ALTER TABLE operator_automations ADD COLUMN action_json TEXT NOT NULL DEFAULT ''")

    # ------------------------------------------------------------------ CRUD

    def create(
        self,
        *,
        name: str,
        trigger_type: str,
        trigger_config: dict[str, Any],
        objective_template: str = "",
        action: dict[str, Any] | None = None,
        project_id: str | None = None,
        objective_title: str = "",
        notification_policy: dict[str, Any] | None = None,
        cooldown_seconds: int = 0,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if not str(name or "").strip() or len(name) > 120:
            raise PlatformValidationError("Automation names must be 1-120 characters.")
        if trigger_type not in TRIGGER_TYPES:
            raise PlatformValidationError(f"Trigger type must be one of: {', '.join(TRIGGER_TYPES)}.")
        text = str(objective_template or "").strip()
        if bool(text) == (action is not None):
            raise PlatformValidationError(
                "Automations require exactly one of an objective_template or an action."
            )
        if text and len(text) > _MAX_TEMPLATE_CHARS:
            raise PlatformValidationError("Automations require an objective template up to 8,000 characters.")
        action_json = self._validate_action(action) if action is not None else ""
        normalized_config, schedule = self._validate_trigger(trigger_type, trigger_config)
        policy_json = self._safe_json(notification_policy or {}, label="notification_policy")
        cooldown = self._validate_cooldown(cooldown_seconds)
        automation_id = f"oam_{secrets.token_hex(12)}"
        now = self.now()
        next_due = initial_due_at(schedule, now) if schedule is not None else None
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO operator_automations
                (id,tenant_id,project_id,name,enabled,trigger_type,trigger_config_json,
                 objective_template,action_json,objective_title,notification_policy_json,cooldown_seconds,next_due_at_epoch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    automation_id, self.tenant_id, project_id, str(name).strip(), int(bool(enabled)),
                    trigger_type, self._safe_json(normalized_config, label="trigger_config"),
                    text, action_json, str(objective_title or "").strip()[:200], policy_json, cooldown, next_due,
                ),
            )
        created = self.get(automation_id)
        if schedule is not None:
            created["schedule"] = describe_schedule(schedule)
        return created

    def _validate_action(self, action: Any) -> str:
        """``action`` = {"contract", "operation", "payload"}. The operation must
        declare ``"schedulable": true`` in its own schema (checked through
        operator_dispatch.find_schedulable_operation, the way the tool runtime
        discovers operations) -- an automation can never call an arbitrary one.
        """
        if not isinstance(action, dict):
            raise PlatformValidationError("action must be an object with contract, operation and payload.")
        if action.get("line_id") is not None:
            line_id = str(action.get("line_id") or "").strip()
            payload = action.get("payload") or {}
            if not line_id or len(line_id) > 160 or not isinstance(payload, dict):
                raise PlatformValidationError("A line action needs a line_id and object payload.")
            from app.services.line_engine import ProductionLineStore

            line = ProductionLineStore(self.store, tenant_id=self.tenant_id).get_line(line_id)
            if not line["enabled"]:
                raise PlatformValidationError("This production line is disabled.")
            encoded = self._safe_json(payload, label="line action payload")
            if len(encoded.encode("utf-8")) > _MAX_ACTION_PAYLOAD_BYTES:
                raise PlatformValidationError("action payload must be at most 4 KB of JSON.")
            return self._safe_json({"line_id": line_id, "payload": payload}, label="action")
        contract = str(action.get("contract") or "").strip()
        operation = str(action.get("operation") or "").strip()
        if not contract or not operation:
            raise PlatformValidationError("action requires both contract and operation.")
        payload = action.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise PlatformValidationError("action payload must be an object.")
        encoded_payload = self._safe_json(payload, label="action payload")
        if len(encoded_payload.encode("utf-8")) > _MAX_ACTION_PAYLOAD_BYTES:
            raise PlatformValidationError("action payload must be at most 4 KB of JSON.")
        from app.services.operator_dispatch import find_schedulable_operation

        if find_schedulable_operation(contract, operation) is None:
            raise PlatformValidationError("This operation cannot be scheduled.")
        return self._safe_json({"contract": contract, "operation": operation, "payload": payload}, label="action")

    def update(self, automation_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get(automation_id)
        sets: list[str] = ["updated_at=CURRENT_TIMESTAMP"]
        params: list[Any] = []
        if "name" in patch:
            name = str(patch.get("name") or "").strip()
            if not name or len(name) > 120:
                raise PlatformValidationError("Automation names must be 1-120 characters.")
            sets.append("name=?"); params.append(name)
        if "enabled" in patch:
            sets.append("enabled=?"); params.append(int(bool(patch["enabled"])))
        if "objective_template" in patch:
            text = str(patch.get("objective_template") or "").strip()
            if len(text) > _MAX_TEMPLATE_CHARS:
                raise PlatformValidationError("Automations allow an objective template up to 8,000 characters.")
            sets.append("objective_template=?"); params.append(text)
        if "action" in patch:
            action = patch.get("action")
            if action is not None:
                sets.append("action_json=?"); params.append(self._validate_action(action))
            else:
                sets.append("action_json=?"); params.append("")
        if "objective_title" in patch:
            sets.append("objective_title=?"); params.append(str(patch["objective_title"] or "").strip()[:200])
        if "notification_policy" in patch:
            sets.append("notification_policy_json=?")
            params.append(self._safe_json(patch.get("notification_policy") or {}, label="notification_policy"))
        if "cooldown_seconds" in patch:
            sets.append("cooldown_seconds=?"); params.append(self._validate_cooldown(patch["cooldown_seconds"]))
        new_schedule = None
        if "trigger_config" in patch:
            normalized, new_schedule = self._validate_trigger(current["trigger_type"], patch.get("trigger_config"))
            sets.append("trigger_config_json=?")
            params.append(self._safe_json(normalized, label="trigger_config"))
        next_text = str(patch.get("objective_template", current.get("objective_template") or "")).strip()
        next_action = patch.get("action", current.get("action"))
        if bool(next_text) == (next_action is not None):
            raise PlatformValidationError("Automations require exactly one of an objective_template or an action.")
        params.append(automation_id)
        params.append(self.tenant_id)
        with self._connection() as db, db:
            cursor = db.execute(
                f"""UPDATE operator_automations SET {", ".join(sets)}
                WHERE id=? AND tenant_id=?""",
                params,
            )
            if cursor.rowcount != 1:
                raise PlatformNotFoundError("Operator automation not found.")
            if new_schedule is not None:
                # A changed schedule restarts its calendar from "now"; the old
                # due time belongs to a configuration that no longer exists.
                db.execute(
                    "UPDATE operator_automations SET next_due_at_epoch=? WHERE id=? AND tenant_id=?",
                    (initial_due_at(new_schedule, self.now()), automation_id, self.tenant_id),
                )
        updated = self.get(automation_id)
        if new_schedule is not None:
            updated["schedule"] = describe_schedule(new_schedule)
        return updated

    def get(self, automation_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM operator_automations WHERE id=? AND tenant_id=?",
                (automation_id, self.tenant_id),
            ).fetchone()
        if row is None:
            raise PlatformNotFoundError("Operator automation not found.")
        return self._public(row)

    def list(self, *, project_id: str | None = None, enabled: bool | None = None, trigger_type: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM operator_automations WHERE tenant_id=?"
        params: list[Any] = [self.tenant_id]
        if project_id is not None:
            query += " AND project_id=?"
            params.append(project_id)
        if enabled is not None:
            query += " AND enabled=?"
            params.append(int(enabled))
        if trigger_type is not None:
            if trigger_type not in TRIGGER_TYPES:
                raise PlatformValidationError(f"Unknown trigger type: {trigger_type!r}")
            query += " AND trigger_type=?"
            params.append(trigger_type)
        query += " ORDER BY created_at DESC, id DESC"
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        return [self._public(row) for row in rows]

    def delete(self, automation_id: str) -> None:
        with self._connection() as db, db:
            cursor = db.execute(
                "DELETE FROM operator_automations WHERE id=? AND tenant_id=?",
                (automation_id, self.tenant_id),
            )
            if cursor.rowcount != 1:
                raise PlatformNotFoundError("Operator automation not found.")

    def list_firings(self, automation_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connection() as db:
            rows = db.execute(
                """SELECT * FROM operator_automation_firings
                WHERE tenant_id=? AND automation_id=?
                ORDER BY triggered_at DESC, id DESC LIMIT ?""",
                (self.tenant_id, automation_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------- schedule triggers

    def process_due(self, *, now: float | None = None) -> dict[str, Any]:
        """Fire every due schedule automation at most once, safely across restarts."""
        self._reconcile_action_firings()
        current = self.now() if now is None else float(now)
        fired: list[str] = []
        suppressed: list[str] = []
        failed: list[str] = []
        missed_windows = 0
        for automation in self.list(enabled=True, trigger_type="schedule"):
            try:
                if automation["next_due_at_epoch"] is None:
                    continue
                status, skipped = self._process_schedule_automation(automation, current)
            except Exception:
                failed.append(automation["id"])
                continue
            missed_windows += skipped
            if status == FIRING_DISPATCHED:
                fired.append(automation["id"])
            elif status == FIRING_SUPPRESSED_COOLDOWN:
                suppressed.append(automation["id"])
            elif status == FIRING_FAILED:
                failed.append(automation["id"])
        return {"fired": fired, "suppressed": suppressed, "failed": failed, "missed_windows": missed_windows}

    def _process_schedule_automation(self, automation: dict[str, Any], current: float) -> tuple[str, int]:
        due = float(automation["next_due_at_epoch"])
        if due > current:
            return ("not_due", 0)
        schedule = self._normalized_schedule(automation)
        # Collect every window that came due, fire only the latest, and record
        # the earlier ones as skipped: a long outage produces one run and
        # truthful history, not a flood of catch-up runs.
        windows: list[float] = []
        cursor = due
        while cursor <= current and len(windows) < _MAX_CATCHUP_WINDOWS:
            windows.append(cursor)
            cursor = advance_due_at(schedule, cursor, current)
        for missed in windows[:-1]:
            self._record_firing(automation, f"schedule:{int(missed)}", FIRING_SKIPPED_MISSED,
                                reason="missed_window", request_id=None)
        skipped = max(0, len(windows) - 1)
        identity = f"schedule:{int(windows[-1])}"
        reserved = self._reserve_firing(automation, identity)
        if not reserved:
            # Another process (or a previous life of this one) already fired
            # this exact window; just move the cursor forward.
            self._advance_cursor(automation, current, schedule)
            return ("deduplicated", skipped)
        outcome = self._dispatch(
            automation,
            trigger_type="schedule",
            trigger_identity=identity,
            render_context={"triggered_at": self._iso_from_epoch(windows[-1]), "due_at": self._iso_from_epoch(windows[-1])},
            request_dedupe_key=f"auto:{automation['id']}:schedule:{int(windows[-1])}",
            context=None,
        )
        self._advance_cursor(automation, current, schedule)
        return (outcome["status"], skipped)

    def _normalized_schedule(self, automation: dict[str, Any]) -> Any:
        cached = automation.get("_normalized_schedule")
        if cached is not None:
            return cached
        schedule = parse_schedule(automation["trigger_config"])
        automation["_normalized_schedule"] = schedule
        return schedule


    # ---------------------------------------------------------- event triggers

    def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Evaluate one platform event against event-triggered automations.

        Idempotent per (automation, event id): redelivery reserves nothing new.
        Never raises; event delivery must not break publishers.
        """
        delivered: list[str] = []
        suppressed: list[str] = []
        failed: list[str] = []
        ignored = 0
        event_tenant = str(event.get("tenant_id") or "") or None
        if event_tenant is not None and event_tenant != self.tenant_id:
            return {"delivered": delivered, "suppressed": suppressed, "failed": failed, "ignored": -1}
        event_id = str(event.get("id") or "").strip()
        event_type = str(event.get("type") or "").strip()
        if not event_id or not event_type:
            return {"delivered": delivered, "suppressed": suppressed, "failed": failed, "ignored": -1}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        for automation in self.list(enabled=True, trigger_type="event"):
            config = automation["trigger_config"]
            patterns = config.get("patterns") if isinstance(config.get("patterns"), list) else []
            if not any(_matches_pattern(str(pattern), event_type) for pattern in patterns):
                continue
            identity = f"event:{event_id}"
            if not self._reserve_firing(automation, identity):
                ignored += 1
                continue
            outcome = self._dispatch(
                automation,
                trigger_type="event",
                trigger_identity=identity,
                render_context={
                    "event_type": event_type,
                    "event_id": event_id,
                    "triggered_at": self._now_iso(),
                },
                context={"event": {"id": event_id, "type": event_type, "payload": payload}},
                request_dedupe_key=f"auto:{automation['id']}:event:{event_id}",
            )
            if outcome["status"] == FIRING_DISPATCHED:
                delivered.append(automation["id"])
            elif outcome["status"] == FIRING_SUPPRESSED_COOLDOWN:
                suppressed.append(automation["id"])
            elif outcome["status"] == FIRING_FAILED:
                failed.append(automation["id"])
        return {"delivered": delivered, "suppressed": suppressed, "failed": failed, "ignored": ignored}

    def attach_event_listener(self, bus: Any) -> Callable[[], None]:
        """Subscribe this service to an existing CoreEventBus instance.

        Optional fast path: durable-event draining (see drain_recent_events)
        already covers correctness, so nothing needs to hold a reference to a
        process-wide bus for automations to fire.
        """

        def listener(event: dict[str, Any]) -> None:
            try:
                self.handle_event(event)
            except Exception:
                pass  # a broken automation must never break event dispatch

        return bus.subscribe("*", listener)

    def drain_recent_events(self, *, limit: int = 200) -> dict[str, Any]:
        """Feed the most recent durable platform events through handle_event.

        Every publish path persists to platform_events before notifying
        subscribers, so tailing that log catches all events without needing a
        shared in-process bus. Replay is safe: firing identity is the event id,
        and the UNIQUE constraint makes redelivery a no-op.
        """
        try:
            events = self.events.list(limit=max(1, min(int(limit), 500)))
        except Exception:
            return {"delivered": [], "suppressed": [], "failed": [], "ignored": -1}
        delivered: list[str] = []
        suppressed: list[str] = []
        failed: list[str] = []
        ignored = 0
        for event in reversed(events):  # oldest first so ordering is preserved
            try:
                outcome = self.handle_event(event)
            except Exception:
                ignored += 1
                continue
            delivered.extend(outcome.get("delivered", []))
            suppressed.extend(outcome.get("suppressed", []))
            failed.extend(outcome.get("failed", []))
            if outcome.get("ignored", 0) > 0:
                ignored += int(outcome["ignored"])
        return {
            "delivered": delivered, "suppressed": suppressed,
            "failed": failed, "ignored": ignored,
        }

    # ------------------------------------------------------- condition triggers

    def evaluate_condition(self, automation_id: str, *, now: float | None = None) -> dict[str, Any]:
        """Resolve metrics and evaluate one condition automation.

        This is a real evaluation tick: baselines advance here exactly as they
        do in the batch path, so ``changed``/``percent_change_gt`` measure
        movement between consecutive evaluations no matter who drives them.
        Dispatch is left to the batch path.
        """
        current = self.now() if now is None else float(now)
        result, _state = self._evaluate_condition_tick(self.get(automation_id), now=current)
        result["evaluated_at_epoch"] = current
        return result

    def _evaluate_condition_tick(self, automation: dict[str, Any], *, now: float) -> tuple[dict[str, Any], dict[str, Any]]:
        spec = automation["trigger_config"]
        scope = {"tenant_id": self.tenant_id, "project_id": automation.get("project_id")}
        resolved = {name: value for name, (value, _origin) in resolve_metrics(spec, self.metric_resolvers, scope).items()}
        state = self._load_state(automation["id"], "conditions")
        baselines = state.get("baselines", {})
        previously_triggered = bool(state.get("last_triggered"))
        result = evaluate_conditions(spec, resolved, baselines=baselines)
        new_state = {
            "baselines": result["next_baselines"],
            "last_triggered": result["triggered"],
            "last_evaluated_at_epoch": now,
            "not_evaluated": result["not_evaluated"],
        }
        # Persist observations regardless of outcome so edges and percent
        # changes are measured between consecutive evaluations.
        self._save_state(automation["id"], "conditions", new_state)
        return result, {"previously_triggered": previously_triggered}

    def process_condition_automations(self, *, now: float | None = None) -> dict[str, Any]:
        """Rising-edge + cooldown dispatch for every enabled condition automation."""
        current = self.now() if now is None else float(now)
        fired: list[str] = []
        suppressed: list[str] = []
        failed: list[str] = []
        for automation in self.list(enabled=True, trigger_type="condition"):
            try:
                outcome = self._evaluate_and_maybe_dispatch(automation, now=current)
            except Exception:
                self._record_firing(automation, f"condition:error:{int(current)}", FIRING_FAILED,
                                    reason="evaluation_error", request_id=None)
                failed.append(automation["id"])
                continue
            if outcome == FIRING_DISPATCHED:
                fired.append(automation["id"])
            elif outcome == FIRING_SUPPRESSED_COOLDOWN:
                suppressed.append(automation["id"])
            elif outcome == FIRING_FAILED:
                failed.append(automation["id"])
        return {"fired": fired, "suppressed": suppressed, "failed": failed}

    def _evaluate_and_maybe_dispatch(self, automation: dict[str, Any], *, now: float) -> str:
        result, edge = self._evaluate_condition_tick(automation, now=now)
        previously_triggered = bool(edge["previously_triggered"])
        rising_edge = result["triggered"] and not previously_triggered
        if not rising_edge:
            return "not_triggered"
        identity = f"condition:{int(now)}"
        if not self._reserve_firing(automation, identity):
            return "deduplicated"
        outcome = self._dispatch(
            automation,
            trigger_type="condition",
            trigger_identity=identity,
            render_context={"triggered_at": self._iso_from_epoch(now)},
            context={"condition_results": result["results"]},
            request_dedupe_key=None,
        )
        return outcome["status"]

    # ------------------------------------------------------------ dispatch path

    def _dispatch(
        self,
        automation: dict[str, Any],
        *,
        trigger_type: str,
        trigger_identity: str,
        render_context: dict[str, Any],
        context: dict[str, Any] | None,
        request_dedupe_key: str | None,
    ) -> dict[str, Any]:
        """Reserve done; apply cooldown, then either dispatch the automation's
        action directly or create the durable objective request."""
        if not self._cooldown_elapsed(automation):
            self._mark_firing(automation, trigger_identity, FIRING_SUPPRESSED_COOLDOWN, reason="cooldown_active")
            self._touch_last_triggered(automation)
            return {"status": FIRING_SUPPRESSED_COOLDOWN}
        if automation.get("action"):
            return self._dispatch_action(automation, trigger_type=trigger_type, trigger_identity=trigger_identity)
        values = _RenderValues({
            "automation_id": automation["id"],
            "automation_name": automation["name"],
            "project_id": automation.get("project_id") or "",
            "trigger_type": trigger_type,
            **render_context,
        })
        try:
            objective_text = automation["objective_template"].format_map(values)
        except Exception:
            objective_text = automation["objective_template"]
        try:
            request = self.objectives.request(
                objective_text,
                project_id=automation.get("project_id"),
                source=trigger_type if trigger_type in {"schedule", "event", "condition"} else "manual",
                title=automation.get("objective_title") or automation["name"],
                trigger_type=trigger_type,
                trigger_identity=trigger_identity,
                automation_id=automation["id"],
                context=context,
                dedupe_key=request_dedupe_key,
            )
        except Exception:
            self._mark_firing(automation, trigger_identity, FIRING_FAILED, reason="objective_request_failed")
            return {"status": FIRING_FAILED}
        self._mark_firing(automation, trigger_identity, FIRING_DISPATCHED,
                          reason="", request_id=request["id"])
        self._touch_last_triggered(automation)
        try:
            self.events.publish({
                "type": "operator.automation_fired",
                "payload": {
                    "automation_id": automation["id"],
                    "automation_name": automation["name"],
                    "trigger_type": trigger_type,
                    "trigger_identity": trigger_identity,
                    "request_id": request["id"],
                    "project_id": automation.get("project_id"),
                    "notification_policy": automation.get("notification_policy"),
                },
            })
        except Exception:
            pass
        return {"status": FIRING_DISPATCHED, "request_id": request["id"], "created": request.get("created", True)}

    def _dispatch_action(
        self, automation: dict[str, Any], *, trigger_type: str, trigger_identity: str,
    ) -> dict[str, Any]:
        """Drive the automation's contract action directly, in the background --
        no objective request, so a step that needs no judgement never spends a
        model call.

        The actor is granted the operation's own declared capability plus the
        declaring integration's own requested capabilities (Phase 16j): an
        operation's implementation routinely needs facades -- storage, media
        -- the tool-layer capability alone never covers, and the integration's
        own granted-capability check (the SDK's *other* principal) still
        intersects this, so the automation can never act with more authority
        than the integration itself was given.
        """
        action = automation["action"]
        if action.get("line_id"):
            try:
                from app.services.line_engine import ProductionLineEngine

                run = ProductionLineEngine(self.store, tenant_id=self.tenant_id).start(
                    str(action["line_id"]), variables=dict(action.get("payload") or {}),
                    project_id=str(automation.get("project_id") or ""),
                )
            except Exception:
                self._mark_firing(automation, trigger_identity, FIRING_FAILED, reason="line_action_failed")
                return {"status": FIRING_FAILED}
            self._mark_firing(automation, trigger_identity, FIRING_DISPATCHED,
                              reason="line_action", request_id=str(run["id"]))
            self._touch_last_triggered(automation)
            try:
                self.events.publish({"type": "operator.automation_fired", "payload": {
                    "automation_id": automation["id"], "automation_name": automation["name"],
                    "trigger_type": trigger_type, "trigger_identity": trigger_identity,
                    "line_id": action["line_id"], "line_run_id": run["id"],
                    "project_id": automation.get("project_id"),
                    "notification_policy": automation.get("notification_policy"),
                }})
            except Exception:
                pass
            return {"status": FIRING_DISPATCHED, "request_id": run["id"]}
        contract = str(action.get("contract") or "")
        operation = str(action.get("operation") or "")
        payload = dict(action.get("payload") or {})
        from app.services.operator_dispatch import find_schedulable_operation

        declared = find_schedulable_operation(contract, operation)
        if declared is None:
            self._mark_firing(automation, trigger_identity, FIRING_FAILED, reason="contract_action_unschedulable")
            return {"status": FIRING_FAILED}
        integration_id = str(declared.get("integration_id") or "")
        try:
            from app.services.integration_registry import IntegrationRegistry

            requested = IntegrationRegistry(self.store, tenant_id=self.tenant_id).get(integration_id)["requested_capabilities"]
        except Exception:
            self._mark_firing(automation, trigger_identity, FIRING_FAILED, reason="contract_action_integration_unknown")
            return {"status": FIRING_FAILED}
        actor = CapabilityActor(
            client_id=f"automation:{automation['id']}", name=f"automation:{automation['id']}",
            client_type="automation", permission_mode="restricted",
            granted_capabilities=frozenset({declared["caller_capability"], *requested}),
        )

        def on_failure(exc: BaseException) -> None:
            # The primary firing row already says "dispatched" by the time a
            # background failure can happen; a second row (never colliding,
            # since its identity carries ":error") is what makes the failure
            # visible at all.
            reason = f"contract_action_error: {str(exc)[:300]}"
            self._record_firing(automation, f"{trigger_identity}:error", FIRING_FAILED, reason=reason, request_id=None)
            self._notify_action_failure(automation, trigger_identity=trigger_identity, reason=reason)

        try:
            from app.services import line_runner

            run_key = line_runner.run_key_for(
                contract, operation, payload,
                scope=f"automation:{automation['id']}:{trigger_identity}",
            )
            line_runner.schedule_step(
                contract=contract, operation=operation, payload=payload,
                run_key=run_key, project_id=automation.get("project_id") or "", actor=actor,
                tenant_id=self.tenant_id, store=self.store, on_failure=on_failure,
                notify_failure=False,
            )
        except Exception:
            self._mark_firing(automation, trigger_identity, FIRING_FAILED, reason="contract_action_failed")
            return {"status": FIRING_FAILED}
        self._mark_firing(
            automation, trigger_identity, FIRING_DISPATCHED,
            reason="contract_action", request_id=run_key,
        )
        self._touch_last_triggered(automation)
        try:
            self.events.publish({
                "type": "operator.automation_fired",
                "payload": {
                    "automation_id": automation["id"],
                    "automation_name": automation["name"],
                    "trigger_type": trigger_type,
                    "trigger_identity": trigger_identity,
                    "contract": contract,
                    "operation": operation,
                    "project_id": automation.get("project_id"),
                    "notification_policy": automation.get("notification_policy"),
                },
            })
        except Exception:
            pass
        return {"status": FIRING_DISPATCHED}

    def _reconcile_action_firings(self) -> None:
        """Surface durable action failures that arrived after dispatch."""
        try:
            from app.services import line_runner

            with self._connection() as db:
                rows = db.execute(
                    """SELECT automation_id,trigger_identity,request_id,reason FROM operator_automation_firings
                       WHERE tenant_id=? AND status=? AND reason IN ('contract_action','line_action') AND request_id<>''
                       ORDER BY triggered_at,id LIMIT 500""",
                    (self.tenant_id, FIRING_DISPATCHED),
                ).fetchall()
            automations = {item["id"]: item for item in self.list(enabled=None)}
            for row in rows:
                automation = automations.get(str(row["automation_id"]))
                if automation is None:
                    continue
                try:
                    if str(row["reason"]) == "line_action":
                        from app.services.line_engine import ProductionLineEngine

                        run = ProductionLineEngine(self.store, tenant_id=self.tenant_id).get_run(str(row["request_id"]))
                    else:
                        run = line_runner.get_run(str(row["request_id"]), tenant_id=self.tenant_id, store=self.store)
                except Exception:
                    continue
                if run.get("status") != "failed":
                    continue
                identity = f"{row['trigger_identity']}:error"
                if not self._reserve_firing(automation, identity):
                    continue
                reason = f"contract_action_error: {str(run.get('error') or 'step failed')[:300]}"
                self._mark_firing(automation, identity, FIRING_FAILED, reason=reason)
                self._notify_action_failure(
                    automation, trigger_identity=str(row["trigger_identity"]), reason=reason,
                )
        except Exception:
            # The durable run remains available for the next scheduler tick.
            return

    def _notify_action_failure(self, automation: dict[str, Any], *, trigger_identity: str, reason: str) -> None:
        """Send an optional best-effort notification for an action failure."""
        hook = _delivery_hook
        if hook is None:
            return
        try:
            hook(
                tenant_id=self.tenant_id,
                project_id=str(automation.get("project_id") or ""),
                set_key=f"{automation['id']}:{trigger_identity}",
                text=f"Automatische Aufgabe {automation['name']} fehlgeschlagen: {reason}",
            )
        except Exception:
            pass

    # ---------------------------------------------------------------- internals

    def _validate_trigger(self, trigger_type: str, config: Any) -> tuple[dict[str, Any], Any]:
        if not isinstance(config, dict):
            raise PlatformValidationError("Trigger config must be an object.")
        if trigger_type == "schedule":
            schedule = parse_schedule(config)
            return describe_schedule(schedule), schedule
        if trigger_type == "event":
            patterns_raw = config.get("patterns")
            if not isinstance(patterns_raw, list) or not patterns_raw:
                raise PlatformValidationError("Event automations require a 'patterns' list.")
            if len(patterns_raw) > _MAX_PATTERNS:
                raise PlatformValidationError(f"Event automations accept at most {_MAX_PATTERNS} patterns.")
            patterns: list[str] = []
            for raw in patterns_raw:
                pattern = str(raw or "").strip()
                if not _valid_event_pattern(pattern):
                    raise PlatformValidationError(
                        f"Invalid event pattern {raw!r}; use exact types, 'namespace.*', or '*'."
                    )
                if pattern not in patterns:
                    patterns.append(pattern)
            return {"patterns": patterns}, None
        return validate_condition_spec(config), None

    def _cooldown_elapsed(self, automation: dict[str, Any]) -> bool:
        cooldown = int(automation.get("cooldown_seconds") or 0)
        last_epoch = automation.get("last_triggered_epoch")
        if cooldown <= 0 or last_epoch is None:
            return True
        # Epoch-based so injected clocks and real clocks behave identically;
        # wall-clock timestamps are kept for audit display only.
        try:
            age = self.now() - float(last_epoch)
        except (TypeError, ValueError):
            return True
        return age >= cooldown

    def _advance_cursor(self, automation: dict[str, Any], now: float, schedule: Any) -> None:
        due = automation["next_due_at_epoch"]
        base = float(due) if due is not None else now
        nxt = base
        while nxt <= now:
            nxt = advance_due_at(schedule, nxt, now)
        with self._connection() as db, db:
            db.execute(
                "UPDATE operator_automations SET next_due_at_epoch=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                (nxt, automation["id"], self.tenant_id),
            )

    def _reserve_firing(self, automation: dict[str, Any], identity: str) -> bool:
        """Insert-before-effect dedupe: True means this caller owns the firing."""
        with self._connection() as db, db:
            try:
                db.execute(
                    """INSERT INTO operator_automation_firings
                    (id,tenant_id,automation_id,trigger_identity,status)
                    VALUES (?,?,?,?,?)""",
                    (f"fir_{secrets.token_hex(12)}", self.tenant_id, automation["id"], identity[:160], "reserved"),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def _mark_firing(self, automation: dict[str, Any], identity: str, status: str, *, reason: str, request_id: str | None = None) -> None:
        with self._connection() as db, db:
            db.execute(
                """UPDATE operator_automation_firings
                SET status=?, reason=?, request_id=?
                WHERE tenant_id=? AND automation_id=? AND trigger_identity=?""",
                (status, str(reason)[:200], request_id, self.tenant_id, automation["id"], identity[:160]),
            )

    def _record_firing(self, automation: dict[str, Any], identity: str, status: str, *, reason: str, request_id: str | None) -> None:
        with self._connection() as db, db:
            db.execute(
                """INSERT OR IGNORE INTO operator_automation_firings
                (id,tenant_id,automation_id,trigger_identity,status,reason,request_id)
                VALUES (?,?,?,?,?,?,?)""",
                (f"fir_{secrets.token_hex(12)}", self.tenant_id, automation["id"],
                 identity[:160], status, str(reason)[:200], request_id),
            )

    def _touch_last_triggered(self, automation: dict[str, Any]) -> None:
        with self._connection() as db, db:
            db.execute(
                """UPDATE operator_automations
                SET last_triggered_at=CURRENT_TIMESTAMP,last_triggered_epoch=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=?""",
                (self.now(), automation["id"], self.tenant_id),
            )

    def _load_state(self, automation_id: str, key: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT state_json FROM operator_automation_state WHERE tenant_id=? AND automation_id=? AND state_key=?",
                (self.tenant_id, automation_id, key),
            ).fetchone()
        try:
            return json.loads(row["state_json"]) if row is not None else {}
        except (TypeError, ValueError):
            return {}

    def _save_state(self, automation_id: str, key: str, state: dict[str, Any]) -> None:
        encoded = self._safe_json(state, label="automation_state")
        with self._connection() as db, db:
            db.execute(
                """INSERT INTO operator_automation_state (tenant_id,automation_id,state_key,state_json)
                VALUES (?,?,?,?)
                ON CONFLICT(tenant_id,automation_id,state_key)
                DO UPDATE SET state_json=excluded.state_json,updated_at=CURRENT_TIMESTAMP""",
                (self.tenant_id, automation_id, key, encoded),
            )

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        value["trigger_config"] = json.loads(value.pop("trigger_config_json") or "{}")
        value["notification_policy"] = json.loads(value.pop("notification_policy_json") or "{}")
        action_json = value.pop("action_json", "") or ""
        value["action"] = json.loads(action_json) if action_json else None
        if value.get("trigger_type") == "schedule":
            try:
                value["schedule"] = describe_schedule(parse_schedule(value["trigger_config"]))
            except PlatformValidationError:
                value["schedule"] = None
        return value

    @staticmethod
    def _validate_cooldown(value: Any) -> int:
        try:
            cooldown = int(value)
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError("cooldown_seconds must be an integer.") from exc
        if not 0 <= cooldown <= MAX_COOLDOWN_SECONDS:
            raise PlatformValidationError(f"cooldown_seconds must be 0..{MAX_COOLDOWN_SECONDS}.")
        return cooldown

    @staticmethod
    def _safe_json(value: Any, *, label: str) -> str:
        try:
            encoded = json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            raise PlatformValidationError(f"{label} must be JSON serializable.") from exc
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise PlatformValidationError(f"{label} exceeds the 64 KiB limit.")
        return encoded

    @staticmethod
    def _iso_from_epoch(epoch: float) -> str:
        from datetime import datetime, timezone as dt_timezone
        return datetime.fromtimestamp(float(epoch), tz=dt_timezone.utc).isoformat()

    def _now_iso(self) -> str:
        return self._iso_from_epoch(self.now())


# ------------------------------------------------------------- default templates

AUTOMATION_TEMPLATES: dict[str, dict[str, Any]] = {
    "daily_commerce_review": {
        "name": "Daily commerce review",
        "trigger_type": "schedule",
        "trigger_config": {"kind": "daily", "time": "12:00", "timezone": "UTC"},
        "objective_title": "Daily commerce review",
        "objective_template": (
            "Review the last 24 hours of publication and operational "
            "results for project {project_id}. Identify important anomalies and opportunities. "
            "Do not publish, spend money or change sensitive settings without policy approval. "
            "Return a concise factual summary followed by recommendations."
        ),
    },
    "failed_publication_investigation": {
        "name": "Failed publication investigation",
        "trigger_type": "event",
        "trigger_config": {"patterns": ["job.failed"]},
        "objective_title": "Investigate failed publication",
        "objective_template": (
            "A job failed in project {project_id} (event {event_id}, type {event_type}). "
            "Inspect provider health and job state, diagnose the failure, and attempt only "
            "policy-permitted remediation. Summarize cause and resolution."
        ),
    },
}


def _load_integration_templates() -> None:
    root = Path(__file__).resolve().parents[1] / "integrations"
    for path in sorted(root.glob("*/data/automations/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and all(key in value for key in ("name", "trigger_type", "trigger_config")):
            AUTOMATION_TEMPLATES.setdefault(path.stem, value)


_load_integration_templates()


def create_from_template(
    service: OperatorAutomationService,
    template_key: str,
    *,
    project_id: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    template = AUTOMATION_TEMPLATES.get(template_key)
    if template is None:
        raise PlatformNotFoundError(f"Unknown automation template: {template_key!r}")
    payload: dict[str, Any] = {**template, "project_id": project_id}
    payload.update(overrides or {})
    return service.create(
        name=payload["name"],
        trigger_type=payload["trigger_type"],
        trigger_config=payload["trigger_config"],
        objective_template=payload["objective_template"],
        action=payload.get("action"),
        project_id=payload.get("project_id"),
        objective_title=payload.get("objective_title", ""),
        cooldown_seconds=payload.get("cooldown_seconds", 0),
        enabled=payload.get("enabled", True),
    )


# --------------------------------------------------------------- scheduler loop

def run_operator_automation_tick(
    service: OperatorAutomationService | None = None,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> OperatorAutomationService:
    """Run one deterministic operator-automation iteration for one tenant.

    Extracted from the async loop so an existing frozen startup scheduler can
    host the tick (plan v5.3 forbids a second scheduler loop at startup); the
    service instance is created lazily and returned for reuse by the host.
    The lazily created service is bound to ``tenant_id`` — the hosted loop
    calls this once per active tenant with a per-tenant cached instance, so
    automations of different tenants never share state.
    """
    active = service
    if active is None:
        active = OperatorAutomationService(tenant_id=tenant_id)
    elif active.tenant_id != tenant_id:
        raise ValueError(
            f"Automation service is bound to tenant {active.tenant_id!r}, not {tenant_id!r}."
        )
    active.process_due()
    active.process_condition_automations()
    active.drain_recent_events()
    return active


async def run_operator_automation_scheduler(
    service: OperatorAutomationService | None = None,
    *,
    tick_seconds: float | None = None,
) -> None:
    """Standalone background tick loop.

    Mirrors email_scheduler's isolation rules: one failing iteration (or a
    failing automation inside an iteration) is logged and retried on the next
    tick — never propagated, never allowed to kill sibling schedulers. Startup
    wiring hosts this tick inside run_sync_scheduler instead of creating an
    additional asyncio task here.
    """
    import asyncio

    from app.config import get_settings
    from app.services.application_logging import clear_loop_failures, log_loop_failure

    settings = get_settings()
    interval = tick_seconds if tick_seconds is not None else max(5.0, float(settings.operator_automation_tick_seconds))
    active = service
    if active is None:
        from app.services.operator_objectives import OperatorObjectiveService as _OOS
        active = OperatorAutomationService(objectives=_OOS())

    while True:
        try:
            active = run_operator_automation_tick(active)
            clear_loop_failures(category="operator_automation", source="operator_automation_scheduler")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_loop_failure(
                category="operator_automation", source="operator_automation_scheduler",
                message="Operator automation iteration failed.", exc=exc, tick_seconds=interval,
            )
        await asyncio.sleep(interval)


def _matches_pattern(pattern: str, event_type: str) -> bool:
    """Exact/prefix matching consistent with IntegrationAutomationBridge._matches."""
    return pattern == "*" or pattern == event_type or (
        pattern.endswith(".*") and event_type.startswith(pattern[:-1])
    )


def _valid_event_pattern(pattern: str) -> bool:
    if pattern == "*":
        return True
    body = pattern[:-2] if pattern.endswith(".*") else pattern
    if not body or len(pattern) > 100:
        return False
    parts = body.split(".")
    return all(part and part.replace("_", "a").replace("-", "a").isalnum() and part[0].isalpha() for part in parts)


__all__ = [
    "AUTOMATION_TEMPLATES",
    "FIRING_DISPATCHED",
    "OperatorAutomationService",
    "TRIGGER_TYPES",
    "attach_default_event_listener",
    "create_from_template",
    "run_operator_automation_scheduler",
]


def attach_default_event_listener(bus: Any, service: OperatorAutomationService | None = None) -> Callable[[], None]:
    """Wire operator automations to a live CoreEventBus (used by main.py)."""
    active = service
    if active is None:
        active = OperatorAutomationService()
    unsubscribe = active.attach_event_listener(bus)

    def stop() -> None:
        try:
            unsubscribe()
        except Exception:
            pass

    return stop
