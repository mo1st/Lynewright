import { Button, Dialog, Table } from "../ui";
import { useEffect, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { api, describeApiError } from "../../api/client";
import { navigate } from "../../router";
import { settingsPath } from "../../navigation";
import { useProject } from "../../context/ProjectContext";
import { operatorApi } from "../../api/operatorClient";
import type { AIProviderTaskSettings, OperatorDecisionModel, OperatorProfile } from "../../types";
import { Badge } from "../ui";
import { formatWhen } from "./shared";
import { PUBLIC_OPERATOR_CAPABILITIES } from "./operatorCapabilities";
import { confirmAction } from "../../context/ConfirmContext";

/** Conservative capability palette offered by the UI. Defaults mirror the
 *  runtime's DEFAULT_OPERATOR_CAPABILITIES; everything listed is a known
 *  public capability the backend will actually accept. */
const CAPABILITY_OPTIONS = PUBLIC_OPERATOR_CAPABILITIES;

const DEFAULT_CAPABILITIES = ["system:read", "projects:read", "jobs:read", "jobs:create"];

const LIMIT_PRESETS = [
  { key: "conservative", label: "Conservative", steps: 12, toolFailures: 3, modelFailures: 2 },
  { key: "extended", label: "Extended task", steps: 40, toolFailures: 6, modelFailures: 4 },
  { key: "maximum", label: "Maximum bounded", steps: 100, toolFailures: 20, modelFailures: 10 },
] as const;

type ProfileForm = {
  name: string;
  projectId: string;
  instructions: string;
  enabled: boolean;
  capabilities: string[];
  maxSteps: number;
  maxConsecutiveToolFailures: number;
  maxModelFailures: number;
  maxTotalTokens: number;
  maxModelDecisions: number;
  maxNetworkAttempts: number;
  noProgressLimit: number;
  retrievalMode: "lexical" | "hybrid_local";
  retrievalCanaryPercent: number;
  decisionPolicy: "cloud_only" | "needle_shadow" | "needle_assisted" | "needle_only";
  needleCanaryPercent: number;
  needleReadConfidence: number;
  needleWriteConfidence: number;
  needleSensitiveConfidence: number;
};

function emptyForm(projectId: string): ProfileForm {
  return {
    name: "",
    projectId,
    instructions: "",
    enabled: true,
    capabilities: [...DEFAULT_CAPABILITIES],
    maxSteps: 12,
    maxConsecutiveToolFailures: 3,
    maxModelFailures: 2,
    maxTotalTokens: 0,
    maxModelDecisions: 0,
    maxNetworkAttempts: 0,
    noProgressLimit: 3,
    retrievalMode: "hybrid_local",
    retrievalCanaryPercent: 10,
    decisionPolicy: "cloud_only",
    needleCanaryPercent: 0,
    needleReadConfidence: 0.92,
    needleWriteConfidence: 0.97,
    needleSensitiveConfidence: 0.99,
  };
}

type Props = {
  profiles: OperatorProfile[];
  loading: boolean;
  loadError: string;
  onRefresh: () => Promise<void>;
};

export function OperatorSettingsPanel({ profiles, loading, loadError, onRefresh }: Props) {
  const { projectId, loading: projectLoading } = useProject();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<ProfileForm>(() => emptyForm(projectId));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [aiRoute, setAiRoute] = useState<AIProviderTaskSettings | null>(null);
  const [decisionModel, setDecisionModel] = useState<OperatorDecisionModel | null>(null);

  useEffect(() => {
    if (projectLoading || !projectId) {
      setAiRoute(null);
      setDecisionModel(null);
      return;
    }
    let cancelled = false;
    // Read-only BYOK state. The key itself lives in AI settings and is never
    // displayed here — configured/not-configured only.
    api.getAIProviderSettings()
      .then((result) => {
        if (!cancelled) {
          setAiRoute(result.providers.find((item) => item.task_type === "operator_agent") ?? null);
        }
      })
      .catch(() => { /* surfaced through the not-configured branch below */ });
    operatorApi.getDecisionModel()
      .then((result) => {
        if (!cancelled) setDecisionModel(result);
      })
      .catch(() => {
        if (!cancelled) setDecisionModel(null);
      });
    return () => { cancelled = true; };
  }, [projectId, projectLoading]);

  // The composer used to be permanently expanded, so an install with no
  // Operator at all still rendered a full profile form — capabilities, limits
  // and all — under an empty state whose own copy said "or define your own
  // below". Everything downstream of it started fifteen hundred pixels later.
  const [composerOpen, setComposerOpen] = useState(false);

  useEffect(() => {
    if (!composerOpen) setForm((current) => ({ ...current, projectId }));
  }, [composerOpen, projectId]);

  // Deliberately leaves `message`/`error` alone: save() closes the composer
  // immediately after setting the confirmation, and clearing it here would
  // swallow the only feedback that the save happened.
  function closeComposer() {
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

  function beginEdit(profile: OperatorProfile) {
    setComposerOpen(true);
    setEditingId(profile.id);
    setForm({
      name: profile.name,
      projectId: profile.project_id ?? "",
      instructions: profile.instructions ?? "",
      enabled: profile.enabled,
      capabilities: [...(profile.granted_capabilities ?? [])],
      maxSteps: profile.max_steps,
      maxConsecutiveToolFailures: profile.max_consecutive_tool_failures,
      maxModelFailures: profile.max_model_failures,
      maxTotalTokens: profile.max_total_tokens ?? 0,
      maxModelDecisions: profile.max_model_decisions ?? 0,
      maxNetworkAttempts: profile.max_network_attempts ?? 0,
      noProgressLimit: profile.no_progress_limit ?? 3,
      retrievalMode: profile.retrieval_mode ?? "hybrid_local",
      retrievalCanaryPercent: profile.retrieval_canary_percent ?? 10,
      decisionPolicy: profile.decision_policy ?? "cloud_only",
      needleCanaryPercent: profile.needle_canary_percent ?? 0,
      needleReadConfidence: profile.needle_read_confidence ?? 0.92,
      needleWriteConfidence: profile.needle_write_confidence ?? 0.97,
      needleSensitiveConfidence: profile.needle_sensitive_confidence ?? 0.99,
    });
    setError("");
    setMessage("");
  }

  async function save() {
    if (!form.projectId.trim()) {
      setError("Select a project before saving an Operator profile.");
      return;
    }
    setBusy(true); setError(""); setMessage("");
    try {
      const payload = {
        name: form.name.trim(),
        project_id: form.projectId.trim(),
        enabled: form.enabled,
        instructions: form.instructions,
        granted_capabilities: form.capabilities,
        max_steps: form.maxSteps,
        max_consecutive_tool_failures: form.maxConsecutiveToolFailures,
        max_model_failures: form.maxModelFailures,
        max_total_tokens: form.maxTotalTokens,
        max_model_decisions: form.maxModelDecisions,
        max_network_attempts: form.maxNetworkAttempts,
        no_progress_limit: form.noProgressLimit,
        retrieval_mode: form.retrievalMode,
        retrieval_canary_percent: form.retrievalCanaryPercent,
        decision_policy: form.decisionPolicy,
        needle_canary_percent: form.needleCanaryPercent,
        needle_read_confidence: form.needleReadConfidence,
        needle_write_confidence: form.needleWriteConfidence,
        needle_sensitive_confidence: form.needleSensitiveConfidence,
      };
      if (editingId) await operatorApi.updateProfile(editingId, payload);
      else await operatorApi.createProfile(payload);
      await onRefresh();
      setMessage(editingId ? "Operator profile saved." : "Operator profile created.");
      closeComposer();
    } catch (caught) {
      setError(describeApiError(caught, "Could not save the Operator profile."));
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled(profile: OperatorProfile) {
    setBusy(true); setError(""); setMessage("");
    try {
      await operatorApi.updateProfile(profile.id, { enabled: !profile.enabled });
      await onRefresh();
    } catch (caught) {
      setError(describeApiError(caught, "Could not change the Operator profile."));
    } finally {
      setBusy(false);
    }
  }

  async function removeProfile(profile: OperatorProfile) {
    if (!(await confirmAction({ title: `Remove ${profile.name}?`, message: "The profile will be archived. Existing runs and history stay linked; active runs must finish first.", confirmLabel: "Remove profile", tone: "danger" }))) return;
    setBusy(true); setError(""); setMessage("");
    try { await operatorApi.deleteProfile(profile.id); await onRefresh(); setMessage("Operator profile removed. Run history is kept."); }
    catch (caught) { setError(describeApiError(caught, "Could not remove the Operator profile.")); }
    finally { setBusy(false); }
  }

  async function bootstrap() {
    // The API requires a real project (it 404s on an unknown one). A brand-new
    // tenant may have no project selected yet, so say that plainly instead of
    // sending a request that is certain to fail.
    if (!projectId) {
      setError("Select a project before setting up the Internal Operator.");
      return;
    }
    setBusy(true); setError(""); setMessage("");
    try {
      await operatorApi.bootstrap(projectId);
      await onRefresh();
      setMessage("Default Internal Operator profile created.");
    } catch (caught) {
      setError(describeApiError(
        caught,
        `Could not create the default Operator profile for project "${projectId}". ` +
        "Check that the project exists and that you have access to it.",
      ));
    } finally {
      setBusy(false);
    }
  }

  const toggleCapability = (capability: string) => {
    setForm((current) => ({
      ...current,
      capabilities: current.capabilities.includes(capability)
        ? current.capabilities.filter((item) => item !== capability)
        : [...current.capabilities, capability],
    }));
  };

  const selectedLimitPreset = LIMIT_PRESETS.find((preset) => (
    preset.steps === form.maxSteps
    && preset.toolFailures === form.maxConsecutiveToolFailures
    && preset.modelFailures === form.maxModelFailures
  ))?.key ?? "custom";

  const applyLimitPreset = (key: string) => {
    const preset = LIMIT_PRESETS.find((item) => item.key === key);
    if (!preset) return;
    setForm((current) => ({
      ...current,
      maxSteps: preset.steps,
      maxConsecutiveToolFailures: preset.toolFailures,
      maxModelFailures: preset.modelFailures,
    }));
  };

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <h3>Agent controls</h3>
          <p>Choose agent roles, permissions, and execution limits. Telegram can use any enabled role below.</p>
        </div>
      </div>

      {message ? <p className="status-message" role="status">{message}</p> : null}
      {error ? <p className="error-message" role="alert">{error}</p> : null}

      <div className="connection-grid">
        <div className="connection-card" data-testid="operator-byok-state">
          <div className="section-heading"><div><strong>Operator AI route</strong><p>Which model the Operator thinks with.</p></div>
            {aiRoute ? (
              aiRoute.enabled && aiRoute.model
                ? <Badge tone="positive">Configured</Badge>
                : <Badge tone="attention">Not active</Badge>
            ) : <Badge tone="neutral">Not configured</Badge>}
          </div>
          {aiRoute ? (
            <dl className="definition-list">
              <div><dt>Route</dt><dd>{aiRoute.provider === "default" ? "General default provider" : aiRoute.provider}</dd></div>
              <div><dt>Model</dt><dd>{aiRoute.model || "—"}</dd></div>
              <div><dt>Status</dt><dd>{aiRoute.enabled ? "Enabled" : "Disabled"}</dd></div>
            </dl>
          ) : (
            <p className="muted-text">No dedicated model yet — configure one before launching objectives.</p>
          )}
          <div className="action-group">
            <Button variant="secondary" type="button" onClick={() => navigate(settingsPath("providers"))}>
              Open AI task routing
            </Button>
          </div>
        </div>
        <div className="connection-card" data-testid="operator-local-decision-model">
          <div className="section-heading"><div><strong>Local decision model</strong><p>Needle proposes operations locally; Lynewright remains the authority.</p></div>
            {decisionModel ? (
              <Badge tone={decisionModel.healthy ? "positive" : "attention"}>
                {decisionModel.installed ? (decisionModel.healthy ? "Healthy" : "Needs attention") : "Not installed"}
              </Badge>
            ) : <Badge tone="neutral">Unavailable</Badge>}
          </div>
          {decisionModel ? (
            <dl className="definition-list">
              <div><dt>Version</dt><dd>{decisionModel.package_version} · generation {decisionModel.generation}</dd></div>
              <div><dt>Global mode</dt><dd>{decisionModel.global_mode}</dd></div>
              <div><dt>Engine checksum</dt><dd><code>{decisionModel.engine_checksum === "unknown" ? "Not verified" : `${decisionModel.engine_checksum.slice(0, 16)}…`}</code></dd></div>
              <div><dt>Judge route</dt><dd>{decisionModel.judge_route_ready ? `${decisionModel.judge_provider || "configured"} · ${decisionModel.judge_model || "model set"}` : "Not ready"}</dd></div>
              <div><dt>Control readiness</dt><dd>{decisionModel.ready_for_control ? "Ready" : decisionModel.ready_for_shadow ? "Shadow-ready" : "Needs verification"}</dd></div>
              <div><dt>Acceptance / fallback</dt><dd>{Math.round(decisionModel.acceptance_rate * 100)}% / {Math.round(decisionModel.fallback_rate * 100)}%</dd></div>
              <div><dt>Latency</dt><dd>P50 {decisionModel.p50_latency_ms} ms · P95 {decisionModel.p95_latency_ms} ms</dd></div>
              <div><dt>Cloud tokens avoided</dt><dd>{decisionModel.cloud_tokens_avoided.toLocaleString()}</dd></div>
            </dl>
          ) : (
            <p className="muted-text">Local-model telemetry is unavailable on this deployment.</p>
          )}
          {profiles.length > 0 ? (
            <div className="operator-local-policy-summary" style={{ marginTop: 12 }}>
              <strong>Profile policy</strong>
              <div className="definition-list" style={{ marginTop: 6 }}>
                {profiles.map((profile) => (
                  <div key={profile.id}>
                    <strong>{profile.name}</strong>
                    <span>
                      {profile.decision_policy ?? "cloud_only"} · canary {profile.needle_canary_percent ?? 0}% ·
                      floors {profile.needle_read_confidence ?? 0.92}/{profile.needle_write_confidence ?? 0.97}/{profile.needle_sensitive_confidence ?? 0.99}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <h4 style={{ marginTop: 18 }}>Agent roles</h4>
      {loading ? <p className="muted-text">Loading profiles…</p> : null}
      {loadError ? <p className="error-message" role="alert">{describeLoadError(loadError)}</p> : null}

      {!loading && !loadError && profiles.length === 0 ? (
        <div className="ui-page-state ui-page-state--empty is-compact" role="status" data-testid="operator-no-profile">
          <div className="ui-page-state__icon" aria-hidden="true"><ShieldAlert size={20} /></div>
          <div>
            <h4>Internal Operator is not configured</h4>
            <p>Create an enabled default profile with conservative limits, or define your own.</p>
          </div>
          <div className="inline-actions">
            <Button variant="secondary" type="button" disabled={busy} onClick={() => void bootstrap()}>Set up default Operator</Button>
            <Button variant="secondary" type="button" disabled={busy} onClick={beginCreate}>Define your own</Button>
          </div>
        </div>
      ) : null}

      {profiles.length > 0 ? (
        <Table label="Operator profiles" rows={profiles} getRowKey={(profile) => profile.id} columns={[
          { key: "name", header: "Profile", render: (profile) => <><strong>{profile.name}</strong><small className="muted-text">{profile.project_id || "All projects"}</small></> },
          { key: "status", header: "Status", render: (profile) => <Badge tone={profile.enabled ? "positive" : "neutral"}>{profile.enabled ? "Enabled" : "Disabled"}</Badge> },
          { key: "policy", header: "Policy", render: (profile) => <><strong>{profile.decision_policy ?? "cloud_only"}</strong><small className="muted-text">{profile.granted_capabilities.length} permissions · {profile.max_steps} steps · {profile.max_total_tokens ? `${profile.max_total_tokens.toLocaleString()} tokens` : "unlimited tokens"}</small></> },
          { key: "updated", header: "Updated", render: (profile) => formatWhen(profile.updated_at) },
        ]} rowActions={(profile) => <div className="action-group"><Button size="sm" variant="secondary" disabled={busy} onClick={() => void toggleEnabled(profile)}>{profile.enabled ? "Disable" : "Enable"}</Button><Button size="sm" variant="secondary" disabled={busy} onClick={() => beginEdit(profile)}>Edit</Button><Button size="sm" variant="danger" disabled={busy} onClick={() => void removeProfile(profile)}>Remove</Button></div>} />
      ) : null}

      {!composerOpen && profiles.length > 0 ? (
        <div className="action-group" style={{ marginTop: 14 }}>
          <Button variant="secondary" type="button" disabled={busy} onClick={beginCreate}>Add an Operator profile</Button>
        </div>
      ) : null}

      {composerOpen ? <Dialog open title={editingId ? "Edit Operator profile" : "Add an agent role"} description="Set the profile scope, permissions, limits, and decision policy." onClose={closeComposer} className="operator-profile-dialog">
        <form
        className="settings-form"
        onSubmit={(event) => { event.preventDefault(); void save(); }}
      >
        <div className="section-heading">
          <span className="eyebrow">Profile and policy</span>
          <Button variant="secondary" type="button" disabled={busy} onClick={closeComposer}>Cancel</Button>
        </div>
        <label className="alert-field" htmlFor="operator-profile-name">
          <span>Name</span>
          <input id="operator-profile-name" name="operator_profile_name" autoComplete="off" value={form.name} required maxLength={120}
            placeholder="e.g. Store operator" onChange={(event) => setForm({ ...form, name: event.target.value })} />
        </label>
        <label className="alert-field" htmlFor="operator-profile-project">
          <span>Project scope (required)</span>
          <input id="operator-profile-project" name="operator_project_id" autoComplete="off" value={form.projectId} placeholder={projectId || "Select a project"} required
            onChange={(event) => setForm({ ...form, projectId: event.target.value })} />
          <small>Access stays inside this project so shared resources and task data do not leak across projects.</small>
        </label>
        <label className="alert-field" htmlFor="operator-profile-instructions">
          <span>Standing instructions</span>
          <textarea id="operator-profile-instructions" name="operator_instructions" autoComplete="off" value={form.instructions} rows={4} maxLength={8000}
            placeholder="How this Operator should work, what it may decide alone, and what always needs you."
            onChange={(event) => setForm({ ...form, instructions: event.target.value })} />
        </label>
        <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
          <legend style={{ fontWeight: 700, fontSize: ".8rem", marginBottom: 6 }}>Role permissions</legend>
          <div className="action-group" style={{ marginBottom: 8 }}>
            <Button variant="secondary" type="button" onClick={() => setForm({ ...form, capabilities: [...CAPABILITY_OPTIONS] })}>
              Grant all current permissions
            </Button>
            <Button variant="secondary" type="button" onClick={() => setForm({ ...form, capabilities: [] })}>
              Clear permissions
            </Button>
          </div>
          <div className="operator-capability-grid">
            {CAPABILITY_OPTIONS.map((capability) => (
              <label className="checkbox-row" key={capability}>
                <input type="checkbox" name={`operator_capability_${capability.replace(":", "_")}`} autoComplete="off" checked={form.capabilities.includes(capability)}
                  onChange={() => toggleCapability(capability)} />
                <code>{capability}</code>
              </label>
            ))}
          </div>
          <small className="muted-text">Remove a permission to prevent that kind of action. The agent can never exceed this list, whatever a model decides.</small>
        </fieldset>
        <label className="alert-field" htmlFor="operator-limit-preset">
          <span>Execution budget</span>
          <select id="operator-limit-preset" name="operator_limit_preset" value={selectedLimitPreset}
            onChange={(event) => applyLimitPreset(event.target.value)}>
            <option value="custom">Custom</option>
            {LIMIT_PRESETS.map((preset) => (
              <option key={preset.key} value={preset.key}>
                {preset.label} — {preset.steps} steps, {preset.toolFailures} tool failures, {preset.modelFailures} model failures
              </option>
            ))}
          </select>
          <small>Choose a preset or edit the exact limits below. Hard ceilings prevent an unattended agent from running without bound.</small>
        </label>
        <div className="settings-grid">
          <label className="alert-field" htmlFor="operator-max-steps">
            <span>Max steps per run</span>
            <input id="operator-max-steps" name="operator_max_steps" type="number" autoComplete="off" min={1} max={100} value={form.maxSteps}
              onChange={(event) => setForm({ ...form, maxSteps: Number(event.target.value) || 1 })} />
          </label>
          <label className="alert-field" htmlFor="operator-max-tool-failures">
            <span>Max consecutive tool failures</span>
            <input id="operator-max-tool-failures" name="operator_max_tool_failures" type="number" autoComplete="off" min={1} max={20} value={form.maxConsecutiveToolFailures}
              onChange={(event) => setForm({ ...form, maxConsecutiveToolFailures: Number(event.target.value) || 1 })} />
          </label>
          <label className="alert-field" htmlFor="operator-max-model-failures">
            <span>Max model failures</span>
            <input id="operator-max-model-failures" name="operator_max_model_failures" type="number" autoComplete="off" min={1} max={10} value={form.maxModelFailures}
              onChange={(event) => setForm({ ...form, maxModelFailures: Number(event.target.value) || 1 })} />
          </label>
          <label className="alert-field" htmlFor="operator-max-total-tokens">
            <span>Total token budget (0 = unlimited)</span>
            <input id="operator-max-total-tokens" name="operator_max_total_tokens" type="number" autoComplete="off" min={0} max={200000} value={form.maxTotalTokens}
              onChange={(event) => setForm({ ...form, maxTotalTokens: Math.max(0, Number(event.target.value) || 0) })} />
          </label>
          <label className="alert-field" htmlFor="operator-max-model-decisions">
            <span>Max model decisions (0 = unlimited)</span>
            <input id="operator-max-model-decisions" name="operator_max_model_decisions" type="number" autoComplete="off" min={0} max={100} value={form.maxModelDecisions}
              onChange={(event) => setForm({ ...form, maxModelDecisions: Math.max(0, Number(event.target.value) || 0) })} />
          </label>
          <label className="alert-field" htmlFor="operator-max-network-attempts">
            <span>Max provider attempts (0 = unlimited)</span>
            <input id="operator-max-network-attempts" name="operator_max_network_attempts" type="number" autoComplete="off" min={0} max={200} value={form.maxNetworkAttempts}
              onChange={(event) => setForm({ ...form, maxNetworkAttempts: Math.max(0, Number(event.target.value) || 0) })} />
          </label>
          <label className="alert-field" htmlFor="operator-no-progress-limit">
            <span>Repeated-read limit</span>
            <input id="operator-no-progress-limit" name="operator_no_progress_limit" type="number" autoComplete="off" min={1} max={10} value={form.noProgressLimit}
              onChange={(event) => setForm({ ...form, noProgressLimit: Math.max(1, Number(event.target.value) || 1) })} />
          </label>
          <label className="alert-field" htmlFor="operator-retrieval-mode">
            <span>Evidence retrieval</span>
            <select id="operator-retrieval-mode" name="operator_retrieval_mode" value={form.retrievalMode}
              onChange={(event) => setForm({ ...form, retrievalMode: event.target.value as "lexical" | "hybrid_local" })}>
              <option value="hybrid_local">Hybrid local (recommended)</option>
              <option value="lexical">Lexical only</option>
            </select>
          </label>
          <label className="alert-field" htmlFor="operator-retrieval-canary">
            <span>Hybrid rollout (%)</span>
            <input id="operator-retrieval-canary" name="operator_retrieval_canary_percent" type="number" autoComplete="off" min={0} max={100} value={form.retrievalCanaryPercent}
              onChange={(event) => setForm({ ...form, retrievalCanaryPercent: Math.min(100, Math.max(0, Number(event.target.value) || 0)) })} />
          </label>
          <label className="alert-field" htmlFor="operator-decision-policy">
            <span>Local decision model</span>
            <select id="operator-decision-policy" name="operator_decision_policy" value={form.decisionPolicy}
              onChange={(event) => setForm({ ...form, decisionPolicy: event.target.value as ProfileForm["decisionPolicy"] })}>
              <option value="cloud_only">Cloud only</option>
              <option value="needle_shadow">Needle shadow (measure only)</option>
              <option value="needle_assisted">Needle assisted (canary ceiling)</option>
              <option value="needle_only">Needle only (testing)</option>
            </select>
            <small>Global deployment mode remains the emergency ceiling. Sensitive actions still require cloud verification and normal approval.</small>
          </label>
          <label className="alert-field" htmlFor="operator-needle-canary">
            <span>Needle canary (%)</span>
            <input id="operator-needle-canary" name="operator_needle_canary_percent" type="number" autoComplete="off" min={0} max={100} value={form.needleCanaryPercent}
              onChange={(event) => setForm({ ...form, needleCanaryPercent: Math.min(100, Math.max(0, Number(event.target.value) || 0)) })} />
          </label>
          <label className="alert-field" htmlFor="operator-needle-read-confidence">
            <span>Read confidence floor</span>
            <input id="operator-needle-read-confidence" name="operator_needle_read_confidence" type="number" autoComplete="off" min={0} max={1} step={0.01} value={form.needleReadConfidence}
              onChange={(event) => setForm({ ...form, needleReadConfidence: Math.min(1, Math.max(0, Number(event.target.value) || 0)) })} />
          </label>
          <label className="alert-field" htmlFor="operator-needle-write-confidence">
            <span>Write confidence floor</span>
            <input id="operator-needle-write-confidence" name="operator_needle_write_confidence" type="number" autoComplete="off" min={0} max={1} step={0.01} value={form.needleWriteConfidence}
              onChange={(event) => setForm({ ...form, needleWriteConfidence: Math.min(1, Math.max(0, Number(event.target.value) || 0)) })} />
          </label>
          <label className="alert-field" htmlFor="operator-needle-sensitive-confidence">
            <span>Sensitive confidence floor</span>
            <input id="operator-needle-sensitive-confidence" name="operator_needle_sensitive_confidence" type="number" autoComplete="off" min={0} max={1} step={0.01} value={form.needleSensitiveConfidence}
              onChange={(event) => setForm({ ...form, needleSensitiveConfidence: Math.min(1, Math.max(0, Number(event.target.value) || 0)) })} />
          </label>
        </div>
        <label className="checkbox-row" htmlFor="operator-profile-enabled">
          <input id="operator-profile-enabled" name="operator_profile_enabled" type="checkbox" autoComplete="off" checked={form.enabled}
            onChange={(event) => setForm({ ...form, enabled: event.target.checked })} />
          Enabled — disabled roles cannot be used by Telegram, runs, or automations
        </label>
        <div className="action-group">
          <Button variant="primary" type="submit" disabled={busy || !form.name.trim() || !form.projectId.trim()}>
            {busy ? "Saving…" : editingId ? "Save changes" : "Create profile"}
          </Button>
        </div>
        </form>
      </Dialog> : null}
    </section>
  );
}

function describeLoadError(raw: string): string {
  // The management API ships in parallel (plan §10); until then a 404 here is
  // expected configuration absence, not a broken dashboard.
  return raw.includes("404") || raw.toLowerCase().includes("not found")
    ? "The Operator management API is not available on this deployment yet."
    : raw;
}
