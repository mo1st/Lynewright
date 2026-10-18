import { Button, Dialog, Table } from "../ui";
import { useCallback, useEffect, useState } from "react";
import { useProject } from "../../context/ProjectContext";
import { describeApiError } from "../../api/client";
import { operatorApi } from "../../api/operatorClient";
import type { OperatorAutomation } from "../../types";
import { Badge } from "../ui";
import { formatWhen } from "./shared";
import { confirmAction } from "../../context/ConfirmContext";

const TRIGGER_TYPES = ["schedule", "event", "condition"] as const;
type TriggerType = (typeof TRIGGER_TYPES)[number];

const SCHEDULE_KINDS = ["daily", "weekly", "interval"] as const;
type ScheduleKind = (typeof SCHEDULE_KINDS)[number];

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;

/** Exactly the operators the backend condition validator accepts. */
const CONDITION_OPERATORS = ["eq", "neq", "gt", "gte", "lt", "lte", "in", "changed", "percent_change_gt"] as const;

const MAX_INTERVAL_MINUTES = 7 * 24 * 60; // backend cap: 7 days in seconds

function defaultTimezone(): string {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; }
  catch { return "UTC"; }
}

type ClauseDraft = { metric: string; op: string; value: string };

type AutomationForm = {
  name: string;
  triggerType: TriggerType;
  scheduleKind: ScheduleKind;
  time: string;
  timezone: string;
  days: string[];
  intervalMinutes: number;
  patternsText: string;
  clauses: ClauseDraft[];
  objectiveTitle: string;
  objectiveTemplate: string;
  cooldownMinutes: number;
  limitToProject: boolean;
  enabled: boolean;
};

function emptyForm(projectId: string): AutomationForm {
  return {
    name: "",
    triggerType: "schedule",
    scheduleKind: "daily",
    time: "09:00",
    timezone: defaultTimezone(),
    days: ["mon", "tue", "wed", "thu", "fri"],
    intervalMinutes: 60,
    patternsText: "",
    clauses: [{ metric: "", op: "gt", value: "" }],
    objectiveTitle: "",
    objectiveTemplate: "",
    cooldownMinutes: 0,
    limitToProject: Boolean(projectId),
    enabled: true,
  };
}

function buildTriggerConfig(form: AutomationForm): Record<string, unknown> {
  if (form.triggerType === "schedule") {
    if (form.scheduleKind === "interval") {
      return { kind: "interval", interval_seconds: Math.max(1, form.intervalMinutes) * 60 };
    }
    if (form.scheduleKind === "weekly") {
      return { kind: "weekly", days: form.days, time: form.time, timezone: form.timezone };
    }
    return { kind: "daily", time: form.time, timezone: form.timezone };
  }
  if (form.triggerType === "event") {
    return {
      patterns: form.patternsText.split("\n").map((line) => line.trim()).filter(Boolean).slice(0, 8),
    };
  }
  return {
    all: form.clauses
      .filter((clause) => clause.metric.trim())
      .map((clause) => {
        const raw = clause.value.trim();
        const asNumber = Number(raw);
        const value = raw !== "" && !Number.isNaN(asNumber) ? asNumber : raw;
        return { metric: clause.metric.trim(), op: clause.op, value };
      }),
  };
}

function parseForm(automation: OperatorAutomation): AutomationForm {
  const config = (automation.trigger_config ?? {}) as Record<string, unknown>;
  const schedule = "kind" in config ? config : null;
  const kind = schedule ? String(config.kind) : "";
  const clauses = Array.isArray(config.all) ? (config.all as Array<{ metric?: string; op?: string; value?: unknown }>) : [];
  const patterns = Array.isArray(config.patterns) ? (config.patterns as string[]) : [];
  return {
    name: automation.name,
    triggerType: (TRIGGER_TYPES.includes(automation.trigger_type as TriggerType)
      ? automation.trigger_type : "schedule") as TriggerType,
    scheduleKind: (SCHEDULE_KINDS.includes(kind as ScheduleKind) ? kind : "daily") as ScheduleKind,
    time: schedule && "time" in config ? String(config.time) : "09:00",
    timezone: schedule && "timezone" in config ? String(config.timezone) : defaultTimezone(),
    days: schedule && Array.isArray(config.days) ? (config.days as string[]) : ["mon", "wed", "fri"],
    intervalMinutes: Math.max(1, Math.round(Number(schedule && "interval_seconds" in config ? Number(config.interval_seconds) : 3600) / 60)),
    patternsText: patterns.join("\n"),
    clauses: clauses.length
      ? clauses.map((clause) => ({
        metric: String(clause.metric ?? ""),
        op: String(clause.op ?? "gt"),
        value: clause.value === undefined ? "" : String(clause.value),
      }))
      : [{ metric: "", op: "gt", value: "" }],
    objectiveTitle: automation.objective_title ?? "",
    objectiveTemplate: automation.objective_template ?? "",
    cooldownMinutes: Math.round((automation.cooldown_seconds ?? 0) / 60),
    limitToProject: Boolean(automation.project_id),
    enabled: automation.enabled,
  };
}

function describeTrigger(automation: OperatorAutomation): string {
  const config = (automation.trigger_config ?? {}) as Record<string, unknown>;
  if ("kind" in config) {
    if (config.kind === "daily") return `daily at ${config.time} (${config.timezone})`;
    if (config.kind === "weekly") return `weekly on ${(config.days as string[]).join(", ")} at ${config.time} (${config.timezone})`;
    if (config.kind === "interval") return `every ${Math.round(Number(config.interval_seconds) / 60)} min`;
  }
  if ("patterns" in config) return `events matching ${(config.patterns as string[]).join(", ")}`;
  if ("all" in config) return `${(config.all as unknown[]).length} condition clause(s)`;
  return automation.trigger_type;
}

export function OperatorAutomationPanel() {
  const { projectId, loading: projectLoading } = useProject();
  const [automations, setAutomations] = useState<OperatorAutomation[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<AutomationForm>(() => emptyForm(projectId));

  const load = useCallback(async () => {
    if (projectLoading || !projectId) {
      setAutomations([]);
      setLoadError("");
      return;
    }
    try {
      setAutomations(await operatorApi.listAutomations(projectId));
      setLoadError("");
    } catch (caught) {
      setAutomations(null);
      setLoadError(describeApiError(caught, "Could not load automations."));
    }
  }, [projectId, projectLoading]);

  useEffect(() => { void load(); }, [load]);

  // Same reason as the profile composer: an empty automations list still
  // rendered the whole trigger/schedule/objective form, which is most of this
  // panel's height and none of its use before the Operator itself exists.
  const [composerOpen, setComposerOpen] = useState(false);

  function reset() {
    setComposerOpen(false);
    setEditingId(null);
    setForm(emptyForm(projectId));
  }

  function beginCreate() {
    setComposerOpen(true);
    setEditingId(null);
    setForm(emptyForm(projectId));
    setError("");
    setMessage("");
  }

  async function save() {
    const validation = validate(form);
    if (validation) { setError(validation); return; }
    setBusy(true); setError(""); setMessage("");
    try {
      const payload = {
        name: form.name.trim(),
        trigger_type: form.triggerType,
        trigger_config: buildTriggerConfig(form),
        objective_template: form.objectiveTemplate,
        objective_title: form.objectiveTitle.trim(),
        project_id: form.limitToProject && projectId ? projectId : null,
        cooldown_seconds: Math.max(0, form.cooldownMinutes) * 60,
        enabled: form.enabled,
      };
      if (editingId) await operatorApi.updateAutomation(editingId, payload);
      else await operatorApi.createAutomation(payload);
      await load();
      setMessage(editingId ? "Automation saved." : "Automation created.");
      reset();
    } catch (caught) {
      setError(describeApiError(caught, "Could not save this automation."));
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled(automation: OperatorAutomation) {
    setBusy(true); setError(""); setMessage("");
    try {
      await operatorApi.updateAutomation(automation.id, { enabled: !automation.enabled });
      await load();
    } catch (caught) {
      setError(describeApiError(caught, "Could not change this automation."));
    } finally {
      setBusy(false);
    }
  }

  async function remove(automation: OperatorAutomation) {
    if (!(await confirmAction({
      title: `Delete the "${automation.name}" automation?`,
      message: "It stops running immediately.",
      confirmLabel: "Delete automation",
      tone: "danger",
    }))) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await operatorApi.deleteAutomation(automation.id);
      if (editingId === automation.id) reset();
      await load();
    } catch (caught) {
      setError(describeApiError(caught, "Could not delete this automation."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <h3>Operator automations</h3>
          <p>Objectives that fire on a schedule, an event, or a metric condition — without anyone opening Lynewright.</p>
        </div>
        <Button variant="secondary" type="button" onClick={() => void load()} disabled={busy}>Refresh</Button>
      </div>

      {message ? <p className="status-message" role="status">{message}</p> : null}
      {error ? <p className="error-message" role="alert">{error}</p> : null}
      {loadError ? <p className="error-message" role="alert">{loadError}</p> : null}
      {automations === null && !loadError ? <p className="muted-text">Loading automations…</p> : null}
      {automations !== null && automations.length === 0 ? (
        <p className="muted-text" role="status">No automations yet — manual runs need none.</p>
      ) : null}

      {!composerOpen ? (
        <div className="action-group" style={{ marginTop: 14 }}>
          <Button variant="secondary" type="button" disabled={busy} onClick={beginCreate}>Add an automation</Button>
        </div>
      ) : null}

      {automations !== null && automations.length > 0 ? (
        <Table label="Operator automations" rows={automations} getRowKey={(automation) => automation.id} columns={[
          { key: "name", header: "Automation", render: (automation) => <><strong>{automation.name}</strong><small className="muted-text">{automation.objective_title || describeTrigger(automation)}</small></> },
          { key: "trigger", header: "Trigger", render: (automation) => describeTrigger(automation) },
          { key: "schedule", header: "Next / last run", render: (automation) => automation.next_due_at_epoch ? `Next ${formatWhen(automation.next_due_at_epoch)}` : automation.last_triggered_at ? `Last ${formatWhen(automation.last_triggered_at)}` : "Not run yet" },
          { key: "status", header: "Status", render: (automation) => <Badge tone={automation.enabled ? "positive" : "neutral"}>{automation.enabled ? "Enabled" : "Paused"}</Badge> },
        ]} rowActions={(automation) => <div className="action-group"><Button size="sm" variant="secondary" disabled={busy} onClick={() => void toggleEnabled(automation)}>{automation.enabled ? "Pause" : "Enable"}</Button><Button size="sm" variant="secondary" disabled={busy} onClick={() => { setComposerOpen(true); setEditingId(automation.id); setForm(parseForm(automation)); setError(""); setMessage(""); }}>Edit</Button><Button size="sm" variant="danger" disabled={busy} onClick={() => void remove(automation)}>Delete</Button></div>} />
      ) : null}

      {composerOpen ? <Dialog open title={editingId ? "Edit automation" : "Add an automation"} description="Choose when it runs and the objective it should carry out." onClose={reset} className="operator-automation-dialog">
        <form className="settings-form"
        onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <div className="section-heading">
          <span className="eyebrow">Trigger and objective</span>
          <Button variant="secondary" type="button" disabled={busy} onClick={reset}>Cancel</Button>
        </div>
        <label className="alert-field" htmlFor="operator-automation-name">
          <span>Name</span>
          <input id="operator-automation-name" name="operator_automation_name" autoComplete="off" value={form.name} required maxLength={120}
            placeholder="e.g. Daily performance summary"
            onChange={(event) => setForm({ ...form, name: event.target.value })} />
        </label>

        <div className="settings-grid">
          <label className="alert-field" htmlFor="operator-automation-trigger">
            <span>Trigger</span>
            <select id="operator-automation-trigger" name="operator_trigger_type" value={form.triggerType}
              onChange={(event) => setForm({ ...form, triggerType: event.target.value as TriggerType })}>
              {TRIGGER_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
            </select>
          </label>
          {form.triggerType === "schedule" ? (
            <label className="alert-field" htmlFor="operator-schedule-kind">
              <span>Schedule kind</span>
              <select id="operator-schedule-kind" name="operator_schedule_kind" value={form.scheduleKind}
                onChange={(event) => setForm({ ...form, scheduleKind: event.target.value as ScheduleKind })}>
                {SCHEDULE_KINDS.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
              </select>
            </label>
          ) : null}
        </div>

        {form.triggerType === "schedule" && form.scheduleKind !== "interval" ? (
          <>
            <div className="settings-grid">
              <label className="alert-field" htmlFor="operator-schedule-time">
                <span>Time of day</span>
                <input id="operator-schedule-time" name="operator_schedule_time" type="time" value={form.time} required
                  onChange={(event) => setForm({ ...form, time: event.target.value })} />
              </label>
              <label className="alert-field" htmlFor="operator-schedule-tz">
                <span>Timezone</span>
                <input id="operator-schedule-tz" name="operator_schedule_timezone" autoComplete="off" value={form.timezone} required maxLength={64}
                  placeholder="Europe/Berlin"
                  onChange={(event) => setForm({ ...form, timezone: event.target.value })} />
              </label>
            </div>
            {form.scheduleKind === "weekly" ? (
              <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
                <legend style={{ fontWeight: 700, fontSize: ".8rem", marginBottom: 6 }}>Days</legend>
                <div className="settings-grid">
                  {DAYS.map((day) => (
                    <label className="checkbox-row" key={day}>
                      <input type="checkbox" name={`operator_schedule_day_${day}`} checked={form.days.includes(day)}
                        onChange={() => setForm((current) => ({
                          ...current,
                          days: current.days.includes(day)
                            ? current.days.filter((item) => item !== day)
                            : [...current.days, day],
                        }))} />
                        {day}
                    </label>
                  ))}
                </div>
              </fieldset>
            ) : null}
          </>
        ) : null}

        {form.triggerType === "schedule" && form.scheduleKind === "interval" ? (
          <label className="alert-field" htmlFor="operator-interval-minutes">
            <span>Every (minutes)</span>
            <input id="operator-interval-minutes" name="operator_interval_minutes" type="number" min={1} max={MAX_INTERVAL_MINUTES} required
              value={form.intervalMinutes}
              onChange={(event) => setForm({ ...form, intervalMinutes: Number(event.target.value) || 1 })} />
            <small>Minimum one minute; the backend never fires faster than it can safely check.</small>
          </label>
        ) : null}

        {form.triggerType === "event" ? (
          <label className="alert-field" htmlFor="operator-event-patterns">
            <span>Event patterns (one per line, max 8)</span>
            <textarea id="operator-event-patterns" name="operator_event_patterns" autoComplete="off" rows={3} value={form.patternsText}
              placeholder={"resource.updated\nproject.*"}
              onChange={(event) => setForm({ ...form, patternsText: event.target.value })} />
            <small>Exact event types, a namespace.* prefix, or * for everything.</small>
          </label>
        ) : null}

        {form.triggerType === "condition" ? (
          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend style={{ fontWeight: 700, fontSize: ".8rem", marginBottom: 6 }}>
              Conditions — every clause must hold
            </legend>
            {form.clauses.map((clause, index) => (
              <div className="settings-grid" key={index} style={{ marginBottom: 8 }}>
                <label className="alert-field">
                  <span>Metric</span>
                  <input name={`operator_condition_metric_${index}`} autoComplete="off" value={clause.metric} placeholder="e.g. orders_yesterday"
                    onChange={(event) => updateClause(index, { ...clause, metric: event.target.value })} />
                </label>
                <label className="alert-field">
                  <span>Operator</span>
                  <select name={`operator_condition_operator_${index}`} value={clause.op}
                    onChange={(event) => updateClause(index, { ...clause, op: event.target.value })}>
                    {CONDITION_OPERATORS.map((op) => <option key={op} value={op}>{op}</option>)}
                  </select>
                </label>
                <label className="alert-field">
                  <span>Value</span>
                  <input name={`operator_condition_value_${index}`} autoComplete="off" value={clause.value}
                    onChange={(event) => updateClause(index, { ...clause, value: event.target.value })} />
                </label>
                <div style={{ alignSelf: "end" }}>
                  <Button variant="secondary" type="button" disabled={form.clauses.length <= 1} onClick={() => setForm((current) => ({ ...current, clauses: current.clauses.filter((_, i) => i !== index), }))}>Remove</Button>
                </div>
              </div>
            ))}
            <Button variant="secondary" type="button" disabled={form.clauses.length >= 8} onClick={() => setForm((current) => ({ ...current, clauses: [...current.clauses, { metric: "", op: "gt", value: "" }], }))}>Add clause</Button>
          </fieldset>
        ) : null}

        <label className="alert-field" htmlFor="operator-objective-template">
          <span>Objective template</span>
          <textarea id="operator-objective-template" name="operator_objective_template" autoComplete="off" rows={4} maxLength={8000} required
            value={form.objectiveTemplate}
            placeholder={"Inspect the previous 24 hours of event and metric activity.\nSummarize important changes and problems."}
            onChange={(event) => setForm({ ...form, objectiveTemplate: event.target.value })} />
          <small>Sent to the Operator each time the trigger fires.</small>
        </label>
        <div className="settings-grid">
          <label className="alert-field" htmlFor="operator-objective-title">
            <span>Objective title (optional)</span>
            <input id="operator-objective-title" name="operator_objective_title" autoComplete="off" value={form.objectiveTitle} maxLength={200}
              placeholder="Daily analysis"
              onChange={(event) => setForm({ ...form, objectiveTitle: event.target.value })} />
          </label>
          <label className="alert-field" htmlFor="operator-cooldown-minutes">
            <span>Cooldown (minutes)</span>
            <input id="operator-cooldown-minutes" name="operator_cooldown_minutes" type="number" min={0} value={form.cooldownMinutes}
              onChange={(event) => setForm({ ...form, cooldownMinutes: Number(event.target.value) || 0 })} />
            <small>Minimum gap between two firings.</small>
          </label>
        </div>
        <div className="settings-grid">
          <label className="checkbox-row" htmlFor="operator-automation-project-limit">
            <input id="operator-automation-project-limit" name="operator_limit_to_project" type="checkbox" checked={form.limitToProject}
              disabled={!projectId}
              onChange={(event) => setForm({ ...form, limitToProject: event.target.checked })} />
            Limit to current project{projectId ? ` (${projectId})` : ""}
          </label>
          <label className="checkbox-row" htmlFor="operator-automation-enabled">
            <input id="operator-automation-enabled" name="operator_automation_enabled" type="checkbox" checked={form.enabled}
              onChange={(event) => setForm({ ...form, enabled: event.target.checked })} />
            Enabled
          </label>
        </div>
        <div className="action-group">
          <Button variant="primary" type="submit" disabled={busy || !form.name.trim() || !form.objectiveTemplate.trim()}>
            {busy ? "Saving…" : editingId ? "Save changes" : "Create automation"}
          </Button>
        </div>
        </form>
      </Dialog> : null}
    </section>
  );

  function updateClause(index: number, next: ClauseDraft) {
    setForm((current) => ({
      ...current,
      clauses: current.clauses.map((item, i) => (i === index ? next : item)),
    }));
  }
}

function validate(form: AutomationForm): string {
  if (!form.name.trim()) return "Give the automation a name.";
  if (!form.objectiveTemplate.trim()) return "Write the objective the Operator should run.";
  if (form.triggerType === "schedule") {
    if (form.scheduleKind !== "interval") {
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(form.time)) return "Use a valid HH:MM time.";
      if (!form.timezone.trim()) return "Name a timezone so the schedule fires at your local time.";
    }
    if (form.scheduleKind === "weekly" && form.days.length === 0) {
      return "Pick at least one day for a weekly schedule.";
    }
  }
  if (form.triggerType === "event" && form.patternsText.split("\n").filter((line) => line.trim()).length === 0) {
    return "Add at least one event pattern.";
  }
  if (form.triggerType === "condition"
    && !form.clauses.some((clause) => clause.metric.trim())) {
    return "Add at least one condition metric.";
  }
  return "";
}
