import { Button } from "../ui";
import { useCallback, useEffect, useRef, useState } from "react";
import { describeApiError } from "../../api/client";
import { operatorApi } from "../../api/operatorClient";
import { useProject } from "../../context/ProjectContext";
import { confirmAction } from "../../context/ConfirmContext";
import type { OperatorProfile, OperatorRun, OperatorRunStep } from "../../types";
import { Badge } from "../ui";
import {
  RunStateBadge,
  formatWhen,
  isTerminalRunState,
  isWaitingRunState,
} from "./shared";

const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 90;
const STALE_RUN_MS = 90_000;

type Props = {
  profiles: OperatorProfile[];
  profilesLoading: boolean;
};

/** Same idiom as AIExperienceBuilder's local ids — `crypto.randomUUID` needs a
 *  secure context, which a self-hosted HTTP dashboard does not have. */
function localIdempotencyKey(): string {
  return `ui-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

export function OperatorRunsPanel({ profiles, profilesLoading }: Props) {
  const { projectId, loading: projectLoading } = useProject();
  const [objective, setObjective] = useState("");
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState("");
  const [activeRun, setActiveRun] = useState<OperatorRun | null>(null);
  const [runs, setRuns] = useState<OperatorRun[] | null>(null);
  const [runsError, setRunsError] = useState("");
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const [stepsByRun, setStepsByRun] = useState<Record<string, OperatorRunStep[]>>({});
  const [actionRunId, setActionRunId] = useState<string | null>(null);
  const [pollingPaused, setPollingPaused] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [runMessage, setRunMessage] = useState("");
  const pollTimer = useRef<number | null>(null);

  const enabledProfiles = profiles.filter((profile) => (
    profile.enabled && (!profile.project_id || profile.project_id === projectId)
  ));
  const noEnabledProfile = !profilesLoading && Boolean(projectId) && enabledProfiles.length === 0;

  useEffect(() => {
    if (enabledProfiles.some((profile) => profile.id === selectedProfileId)) return;
    setSelectedProfileId(enabledProfiles[0]?.id ?? "");
  }, [enabledProfiles, selectedProfileId]);

  const loadRuns = useCallback(async () => {
    if (projectLoading || !projectId) {
      setRuns([]);
      setActiveRun(null);
      setRunsError("");
      return;
    }
    try {
      const next = await operatorApi.listRuns(20, projectId);
      setRuns(next);
      setActiveRun((current) => current && next.some((item) => item.id === current.id)
        ? current
        : next.find((item) => !isTerminalRunState(item.status)) ?? null);
      setRunsError("");
    } catch (caught) {
      setRuns(null);
      setRunsError(describeApiError(caught, "Could not load recent runs."));
    }
  }, [projectId, projectLoading]);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  useEffect(() => {
    if (!activeRun || isTerminalRunState(activeRun.status)) {
      setPollingPaused(false);
      return;
    }
    let polls = 0;
    let cancelled = false;
    const tick = async () => {
      polls += 1;
      try {
        const next = await operatorApi.getRun(activeRun.id);
        if (cancelled) return;
        setActiveRun(next);
        if (isTerminalRunState(next.status)) {
          void loadRuns();
          return;
        }
      } catch { /* transient polling errors stay silent; the last state remains shown */ }
      if (polls < MAX_POLLS && !cancelled) {
        pollTimer.current = window.setTimeout(() => void tick(), POLL_INTERVAL_MS);
      } else if (!cancelled) {
        setPollingPaused(true);
      }
    };
    pollTimer.current = window.setTimeout(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (pollTimer.current !== null) window.clearTimeout(pollTimer.current);
    };
  }, [activeRun?.id, activeRun?.status, loadRuns]);

  async function launch() {
    if (!projectId || !selectedProfileId || !objective.trim()) return;
    setLaunching(true); setLaunchError(""); setRunMessage("");
    try {
      // An idempotency key makes a double-click replay-safe instead of
      // launching two identical runs.
      const run = await operatorApi.startRun({
        profile_id: selectedProfileId,
        project_id: projectId,
        objective: objective.trim(),
        idempotency_key: localIdempotencyKey(),
        execution_mode: "queued",
      });
      setPollingPaused(false);
      setActiveRun(run);
      if (!isTerminalRunState(run.status)) setObjective("");
      void loadRuns();
    } catch (caught) {
      setLaunchError(describeApiError(caught, "Could not start the Operator run."));
    } finally {
      setLaunching(false);
    }
  }

  async function cancelRun(runId: string) {
    if (actionRunId) return;
    setActionRunId(runId); setLaunchError("");
    try {
      const run = await operatorApi.cancelRun(runId);
      if (activeRun?.id === runId) setActiveRun(run);
      void loadRuns();
    } catch (caught) {
      setLaunchError(describeApiError(caught, "Could not cancel the run."));
    } finally {
      setActionRunId(null);
    }
  }

  async function resumeRun(runId: string) {
    if (actionRunId) return;
    setActionRunId(runId); setLaunchError("");
    try {
      const run = await operatorApi.resumeRun(runId);
      setPollingPaused(false);
      setActiveRun(run);
      void loadRuns();
    } catch (caught) {
      setLaunchError(describeApiError(caught, "Could not resume the run."));
    } finally {
      setActionRunId(null);
    }
  }

  async function runAgain(run: OperatorRun) {
    if (!projectId || actionRunId) return;
    if (!(await confirmAction({
      title: "Run this objective again?",
      message: "This starts a new run with the same objective. Review the previous result first if it may have changed data.",
      confirmLabel: "Run again",
    }))) return;
    setActionRunId(run.id); setLaunchError("");
    try {
      const next = await operatorApi.startRun({
        profile_id: run.profile_id,
        project_id: projectId,
        objective: run.objective,
        idempotency_key: localIdempotencyKey(),
        execution_mode: "queued",
      });
      setPollingPaused(false);
      setActiveRun(next);
      void loadRuns();
    } catch (caught) {
      setLaunchError(describeApiError(caught, "Could not run this objective again."));
    } finally {
      setActionRunId(null);
    }
  }

  async function clearHistory() {
    if (!projectId || clearing) return;
    if (!(await confirmAction({
      title: "Clear completed run history?",
      message: `This permanently removes all completed, failed, or cancelled runs for ${projectId}. Runs still working or waiting are kept.`,
      confirmLabel: "Clear history",
      tone: "danger",
    }))) return;
    setClearing(true); setLaunchError(""); setRunMessage("");
    try {
      const result = await operatorApi.clearRuns(projectId);
      setRuns((current) => current?.filter((run) => !isTerminalRunState(run.status)) ?? current);
      setStepsByRun({});
      setExpandedRunId(null);
      if (activeRun && isTerminalRunState(activeRun.status)) setActiveRun(null);
      setRunMessage(result.cleared
        ? `Cleared ${result.cleared} run${result.cleared === 1 ? "" : "s"}. Active runs were kept.`
        : "No terminal runs were found. Active runs were kept.");
    } catch (caught) {
      setLaunchError(describeApiError(caught, "Could not clear run history."));
    } finally {
      setClearing(false);
    }
  }

  async function toggleExpanded(runId: string) {
    if (expandedRunId === runId) { setExpandedRunId(null); return; }
    setExpandedRunId(runId);
    if (stepsByRun[runId]) return;
    try {
      const steps = await operatorApi.listRunSteps(runId);
      setStepsByRun((current) => ({ ...current, [runId]: steps }));
    } catch {
      setStepsByRun((current) => ({ ...current, [runId]: [] }));
    }
  }

  const waitingHint = activeRun && !isTerminalRunState(activeRun.status)
    ? waitingExplanation(activeRun.status)
    : "";

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <h3>Run the Operator</h3>
          <p>Launch one objective now and watch it through queued, running, waiting, and done.</p>
        </div>
        <Button variant="secondary" type="button" title="Remove completed, failed, and cancelled runs for this project; live runs are kept" aria-label="Clear completed run history" disabled={clearing || launching || !projectId || runs === null || Boolean(runsError)} onClick={() => void clearHistory()}>
          {clearing ? "Clearing..." : "Clear completed run history"}
        </Button>
      </div>

      {!projectId ? (
        <div className="settings-provider-empty" role="status">
          <strong>Select a project to run the Operator</strong>
          <span>Operator objectives are scoped to one project so website and list data stay private.</span>
        </div>
      ) : noEnabledProfile ? (
        <div className="settings-provider-empty" role="status" data-testid="operator-runs-unconfigured">
          <strong>Internal Operator is not configured</strong>
          <span>Create and enable an Operator profile above before launching objectives.</span>
        </div>
      ) : (
        <form className="settings-form" onSubmit={(event) => { event.preventDefault(); void launch(); }}>
          <div className="settings-grid">
            <label className="alert-field" htmlFor="operator-run-profile">
              <span>Profile</span>
              <select id="operator-run-profile" name="operator_profile" value={selectedProfileId}
                onChange={(event) => setSelectedProfileId(event.target.value)}>
                {enabledProfiles.length === 0 ? <option value="">No enabled profile</option> : null}
                {enabledProfiles.map((profile) => (
                  <option key={profile.id} value={profile.id}>{profile.name}</option>
                ))}
              </select>
            </label>
          </div>
          <label className="alert-field" htmlFor="operator-run-objective">
            <span>Objective</span>
            <textarea id="operator-run-objective" name="operator_objective" autoComplete="off" rows={3} maxLength={8000} required
              placeholder="e.g. Inspect current system health and summarize any problems."
              value={objective} onChange={(event) => setObjective(event.target.value)} />
          </label>
          <div className="action-group">
            <Button variant="primary" type="submit" disabled={launching || !projectId || !selectedProfileId || !objective.trim()}>
              {launching ? "Starting…" : "Run Operator"}
            </Button>
            <Button variant="secondary" type="button" disabled={launching} onClick={() => void loadRuns()}>
              Refresh runs
            </Button>
          </div>
          {launchError ? <p className="error-message" role="alert">{launchError}</p> : null}
        </form>
      )}

      {runMessage ? <p className="status-message" role="status">{runMessage}</p> : null}

      {activeRun ? (
        <div className="connection-card operator-active-run" data-testid="operator-active-run"
          style={{ marginTop: 14 }}>
          <div className="section-heading">
            <div><strong>{activeRun.objective.length > 90 ? `${activeRun.objective.slice(0, 90)}…` : activeRun.objective}</strong></div>
            <RunStateBadge value={activeRun.status} />
          </div>
          <dl className="definition-list">
            <div><dt>Step</dt><dd>{activeRun.current_step}</dd></div>
            <div><dt>Started</dt><dd>{formatWhen(activeRun.started_at ?? activeRun.created_at)}</dd></div>
            {activeRun.wake_at ? <div><dt>Wakes</dt><dd>{formatWhen(activeRun.wake_at)}</dd></div> : null}
            {activeRun.last_model ? <div><dt>Model</dt><dd><code>{activeRun.last_model}</code></dd></div> : null}
            {activeRun.usage_summary ? <div><dt>AI usage</dt><dd>{formatTokenCount(activeRun.usage_summary.total_tokens)} tokens · {activeRun.usage_summary.network_attempts ?? 0} provider attempt{activeRun.usage_summary.network_attempts === 1 ? "" : "s"}{activeRun.usage_summary.unknown_attempts ? ` · ${activeRun.usage_summary.unknown_attempts} unknown` : ""}</dd></div> : null}
            <div><dt>Last update</dt><dd>{runAgeLabel(activeRun)}</dd></div>
          </dl>
          {activeRun.summary ? <p className="operator-run-summary" style={{ margin: "8px 0 0" }}>{runSummaryText(activeRun.summary)}</p> : null}
          {activeRun.error_summary ? (
            <p className="error-message" role="alert">{runFailureText(activeRun)}</p>
          ) : null}
          {waitingHint ? <p className="muted-text">{waitingHint}</p> : null}
          {isStaleRun(activeRun) ? <p className="warning-message" role="status">No update for {runAgeLabel(activeRun)}. The worker may have stopped.</p> : null}
          {pollingPaused ? <p className="muted-text" role="status">Live updates paused after 3 minutes. Refresh runs to continue watching.</p> : null}
          {!isTerminalRunState(activeRun.status) ? (
            <div className="action-group">
              <Button variant="secondary" type="button" disabled={actionRunId !== null} onClick={() => void cancelRun(activeRun.id)}>
                {actionRunId === activeRun.id ? "Updating..." : "Cancel run"}
              </Button>
              {(isWaitingRunState(activeRun.status) || isStaleRun(activeRun)) && activeRun.status !== "waiting_approval"
                ? <Button variant="secondary" type="button" disabled={actionRunId !== null} onClick={() => void resumeRun(activeRun.id)}>Resume now</Button>
                : null}
            </div>
          ) : null}
          {activeRun.status === "failed" ? <div className="action-group"><Button variant="secondary" type="button" disabled={actionRunId !== null} onClick={() => void runAgain(activeRun)}>{actionRunId === activeRun.id ? "Startingâ€¦" : "Run again"}</Button></div> : null}
          {activeRun.status === "waiting_approval" ? (
            <p className="muted-text">Resolve the pending approval below — the run resumes on its own afterwards.</p>
          ) : null}
        </div>
      ) : null}

      {/* With no enabled profile there is no launcher above and no run can
          exist, so an empty history here is a heading and a sentence telling
          the reader to use a control that is not on the page. Runs recorded
          before the profile was disabled still show. */}
      {!noEnabledProfile || (runs !== null && runs.length > 0) ? <>
        <h4 style={{ marginTop: 18 }}>Recent runs</h4>
        {runsError ? <div className="operator-load-error" role="alert"><p className="error-message">{runsError}</p><Button variant="secondary" type="button" onClick={() => void loadRuns()}>Retry run history</Button></div> : null}
        {runs === null && !runsError ? <p className="muted-text">Loading runs…</p> : null}
        {runs !== null && runs.length === 0 ? (
          <p className="muted-text" role="status">No runs yet. Launch an objective above to see it here.</p>
        ) : null}
      </> : null}
      {runs !== null && runs.length > 0 ? (
        <ol className="settings-provider-chain" aria-label="Recent Operator runs">
          {runs.map((run) => (
            <li className="settings-provider-chain-item operator-list-item operator-run-list-item" key={run.id}>
              <div className="settings-provider-copy">
                <div className="settings-provider-title"><strong>{run.objective.slice(0, 80) || run.id}</strong></div>
                <div className="settings-provider-meta">
                  <RunStateBadge value={run.status} />
                  <span aria-hidden="true">·</span>
                  <span>step {run.current_step}</span>
                  {run.usage_summary ? <><span aria-hidden="true">·</span><span>{formatTokenCount(run.usage_summary.total_tokens)} tokens</span></> : null}
                  <span aria-hidden="true">·</span>
                  <span>{formatWhen(run.created_at)}</span>
                  {run.summary ? <span title={run.summary}>{runSummaryText(run.summary).slice(0, 80)}</span> : null}
                  {run.error_summary ? <span title={run.error_summary}>{runErrorSummary(run.error_summary)}</span> : null}
                </div>
                {!isTerminalRunState(run.status) ? <span className="operator-run-freshness">Updated {runAgeLabel(run)}</span> : null}
                {isStaleRun(run) ? <Badge tone="attention">Needs attention</Badge> : null}
                {expandedRunId === run.id ? (
                  <div id={`operator-run-details-${run.id}`}><OperatorRunSteps steps={stepsByRun[run.id]} /></div>
                ) : null}
              </div>
              <div className="inline-actions" aria-label={`Actions for ${run.objective.slice(0, 50)}`}>
                {!isTerminalRunState(run.status)
                  ? <Button variant="secondary" type="button" disabled={actionRunId !== null} onClick={() => void cancelRun(run.id)}>{actionRunId === run.id ? "Updating..." : "Cancel"}</Button>
                  : null}
                {(isWaitingRunState(run.status) || isStaleRun(run)) && run.status !== "waiting_approval"
                  ? <Button variant="secondary" type="button" disabled={actionRunId !== null} onClick={() => void resumeRun(run.id)}>Resume</Button>
                  : null}
                {run.status === "failed" ? <Button variant="secondary" type="button" disabled={actionRunId !== null} onClick={() => void runAgain(run)}>Run again</Button> : null}
                <Button variant="secondary" type="button" aria-expanded={expandedRunId === run.id} aria-controls={`operator-run-details-${run.id}`} onClick={() => void toggleExpanded(run.id)}>
                  {expandedRunId === run.id ? "Hide details" : "Details"}
                </Button>
              </div>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

function OperatorRunSteps({ steps }: { steps: OperatorRunStep[] | undefined }) {
  if (!steps) {
    return <p className="muted-text">Loading step history…</p>;
  }
  if (steps.length === 0) {
    return <p className="muted-text">No step history recorded for this run yet.</p>;
  }
  const recent = steps.slice(-10);
  return (
    <table className="confirmation-table" style={{ minWidth: 0, marginTop: 10 }}>
      <thead>
        <tr><th scope="col">#</th><th scope="col">Step</th><th scope="col">Tool</th><th scope="col">Status</th><th scope="col">Tokens</th><th scope="col">Took</th></tr>
      </thead>
      <tbody>
        {recent.map((step) => (
          <tr key={step.sequence}>
            <td>{step.sequence}</td>
            <td>{step.step_type}{step.error_summary ? <small>{step.error_summary}</small> : null}</td>
            <td><code>{step.tool_name || "—"}</code></td>
            <td><Badge tone={step.status === "succeeded" ? "positive" : "critical"}>{step.status}</Badge></td>
            <td>{step.input_tokens || step.output_tokens ? `${((step.input_tokens || 0) + (step.output_tokens || 0)).toLocaleString()}` : "—"}</td>
            <td>{step.duration_ms ? `${Math.round(step.duration_ms / 100) / 10}s` : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function waitingExplanation(status: string): string {
  switch (status) {
    case "waiting_tool": return "Waiting on an external tool call to finish.";
    case "waiting_job": return "Waiting on a background job — the Operator sleeps until it completes.";
    case "sleeping": return "Nothing to do right now; the Operator wakes at its next scheduled time.";
    default: return "";
  }
}

function runFailureText(run: OperatorRun): string {
  const code = run.error_code ? ` (${run.error_code})` : "";
  const raw = run.error_summary || "The Operator stopped before it could finish.";
  if (/project[_ ]?id.*required/i.test(raw)) {
    return `Run failed${code}: Website context was missing. New runs now use the selected project; choose “Run again” to retry it.`;
  }
  return `Run failed${code}: ${raw}`;
}

function runSummaryText(summary: string): string {
  return /contains no action/i.test(summary)
    ? "No action was requested; the Operator only reported back."
    : summary;
}

function runErrorSummary(summary: string): string {
  return /project[_ ]?id.*required/i.test(summary)
    ? "Website scope missing — run again"
    : summary.slice(0, 80);
}

function isStaleRun(run: OperatorRun): boolean {
  if (isTerminalRunState(run.status)) return false;
  const updated = parseRunTimestamp(run.updated_at || run.created_at);
  return Number.isFinite(updated) && Date.now() - updated > STALE_RUN_MS;
}

function runAgeLabel(run: OperatorRun): string {
  const updated = parseRunTimestamp(run.updated_at || run.created_at);
  if (!Number.isFinite(updated)) return formatWhen(run.updated_at || run.created_at);
  const seconds = Math.max(0, Math.round((Date.now() - updated) / 1000));
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return `${hours}h ago`;
}

/** API responses from older workers may omit optional usage fields while a
 * terminal transition is being persisted. Keep the dashboard render-safe and
 * show a deterministic zero rather than crashing the whole Operator page. */
function formatTokenCount(value: number | null | undefined): string {
  const count = Number(value);
  return Number.isFinite(count) && count >= 0 ? count.toLocaleString() : "0";
}

function parseRunTimestamp(value: string): number {
  const raw = String(value || "").trim();
  if (!raw) return Number.NaN;
  // SQLite CURRENT_TIMESTAMP is UTC but has no offset. Treat it as UTC rather
  // than letting each browser interpret it in its own local timezone.
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(raw)
    ? `${raw.replace(" ", "T")}Z`
    : raw;
  return Date.parse(normalized);
}
