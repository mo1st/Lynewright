import { useEffect, useState } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";

type Props = { integrationId: string; fallback: Record<string, unknown>; enabled: boolean; manual?: boolean; lastChecked?: string | null };
function displayValue(value: unknown, fallback: string) { return typeof value === "string" && value.trim() ? value : fallback; }

export default function HealthDiagnosticsSection({ integrationId, fallback, enabled, manual = false, lastChecked = null }: Props) {
  const [health, setHealth] = useState<Record<string, unknown> | null>(null);
  const [diagnostics, setDiagnostics] = useState<Record<string, unknown> | null>(null);
  const [attentionCount, setAttentionCount] = useState(0);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  useEffect(() => { if (!enabled) { setHealth(fallback); setDiagnostics(null); setAttentionCount(0); return; } let active = true; void Promise.allSettled([integrationManagementApi.health(integrationId), integrationManagementApi.diagnostics(integrationId), integrationManagementApi.attention(integrationId)]).then(([healthResult, diagnosticsResult, attentionResult]) => { if (!active) return; setHealth(healthResult.status === "fulfilled" ? healthResult.value : fallback); setDiagnostics(diagnosticsResult.status === "fulfilled" ? diagnosticsResult.value : null); setAttentionCount(attentionResult.status === "fulfilled" ? attentionResult.value.items.length : 0); }); return () => { active = false; }; }, [enabled, fallback, integrationId]);
  const source = health ?? fallback;
  const status = displayValue(source.status ?? source.state, "UNKNOWN").toUpperCase();
  const normalManualState = manual && ["NOT_EXECUTABLE", "NOT_REGISTERED"].includes(status);
  const message = normalManualState ? "Manual integrations execute through the selected connection; a compiled package runtime is not expected." : displayValue(source.message ?? source.last_error, status === "HEALTHY" || status === "OK" ? "The integration is responding normally." : "Review the connection and diagnostics details.");
  const statusLabel = normalManualState ? `${status} · normal` : status;
  const checkedAt = source.checked_at ?? source.last_tested_at ?? source.updated_at ?? lastChecked;
  return <section className="integration-subsection" aria-label="Health and diagnostics"><header className="panel-header"><div><h4>Health</h4><p className="muted-text">Current control-plane status for this integration.</p></div><span className={`integration-status integration-status--${normalManualState ? "healthy" : status.toLowerCase()}`}>{statusLabel}</span></header><p>{message}</p><p className="muted-text">Last tested: {displayValue(checkedAt, "Not tested yet")}{attentionCount ? ` · ${attentionCount} attention item${attentionCount === 1 ? "" : "s"}` : ""}</p><details className="settings-advanced" open={diagnosticsOpen} onToggle={(event) => setDiagnosticsOpen(event.currentTarget.open)}><summary><strong>Advanced diagnostics</strong><span>Show structured troubleshooting details</span></summary><pre className="json-preview">{JSON.stringify(diagnostics ?? source, null, 2)}</pre></details></section>;
}
