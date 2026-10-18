import { useState } from "react";
import { Button, CopyField, FormField, Select, StatusBadge, useToast } from "../ui";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import { confirmAction } from "../../context/ConfirmContext";
import type { IntegrationManagementSummary } from "../../types";

function relativeTime(value?: string | null) {
  if (!value) return "Not seen yet";
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return "Not available";
  const seconds = Math.round((timestamp - Date.now()) / 1000);
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [["day", 86400], ["hour", 3600], ["minute", 60]];
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, divisor] of units) if (Math.abs(seconds) >= divisor) return formatter.format(Math.round(seconds / divisor), unit);
  return formatter.format(seconds, "second");
}

export default function IntegrationRunnerSection({ integration }: { integration: IntegrationManagementSummary }) {
  const { addToast } = useToast();
  const [format, setFormat] = useState<"compose" | "quadlet">("compose");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const status = integration.availability.runner_status ?? "never_seen";

  async function createToken() {
    setBusy("token"); setError(""); setToken("");
    try {
      const result = await integrationManagementApi.createRunnerToken(integration.integration.id);
      setToken(result.token);
      addToast({ tone: "warning", message: "Copy this runner token now. It is shown only once." });
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Runner token could not be created."); }
    finally { setBusy(""); }
  }

  async function revokeToken() {
    if (!(await confirmAction({ title: "Revoke this runner token?", message: "The runner will lose access until a new token is created.", confirmLabel: "Revoke token", tone: "danger" }))) return;
    setBusy("revoke"); setError(""); setToken("");
    try { await integrationManagementApi.revokeRunnerToken(integration.integration.id); addToast({ tone: "success", message: "Runner token revoked." }); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Runner token could not be revoked."); }
    finally { setBusy(""); }
  }

  async function downloadConfig() {
    setBusy("config"); setError("");
    try {
      const result = await integrationManagementApi.runtimeConfig(integration.integration.id, format);
      const content = result.network_file && result.network_content
        ? `${result.content.trimEnd()}\n\n# ${result.network_file}\n${result.network_content}`
        : result.content;
      const url = URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
      const anchor = document.createElement("a"); anchor.href = url;
      anchor.download = format === "compose" ? "compose.runner.yaml" : "lynewright-runner.container";
      document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
      for (const warning of result.warnings ?? []) addToast({ tone: "warning", message: warning });
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Runtime configuration could not be downloaded."); }
    finally { setBusy(""); }
  }

  return <div className="u3-runner-section">
    <div className="u3-runner-status">
      <div><strong>Runner health</strong><p><StatusBadge status={status === "never_seen" ? "not_connected" : status} /></p></div>
      {integration.availability.runner_last_seen_at ? <div><strong>Runner last seen</strong><p><time dateTime={integration.availability.runner_last_seen_at} title={new Date(integration.availability.runner_last_seen_at).toLocaleString()}>{relativeTime(integration.availability.runner_last_seen_at)}</time></p></div> : null}
    </div>
    <section aria-labelledby="u3-runner-token-title">
      <h3 id="u3-runner-token-title">Runner access</h3>
      <p>Create or rotate a token for this runner. Copy it now; the full token cannot be shown again.</p>
      <Button variant="secondary" busy={busy === "token"} onClick={() => void createToken()}>{busy === "token" ? "Creating token…" : "Create or rotate token"}</Button>
      <Button variant="danger" busy={busy === "revoke"} onClick={() => void revokeToken()}>Revoke token</Button>
      {token ? <div className="u3-runner-token"><p role="alert"><strong>Shown once:</strong> copy this token to the runner’s secret store now.</p><CopyField label="One-time runner token" value={token} /></div> : null}
    </section>
    <section aria-labelledby="u3-runner-config-title">
      <h3 id="u3-runner-config-title">Runtime configuration</h3>
      <p>Download a Compose or Quadlet file, then apply it on the machine that runs this integration.</p>
      <div className="u3-inline-form"><FormField label="Runtime format" htmlFor="u3-runner-format"><Select id="u3-runner-format" value={format} onChange={(event) => setFormat(event.target.value as "compose" | "quadlet")}><option value="compose">Compose</option><option value="quadlet">Quadlet</option></Select></FormField><Button variant="secondary" busy={busy === "config"} onClick={() => void downloadConfig()}>{busy === "config" ? "Preparing…" : "Download runtime config"}</Button></div>
    </section>
    <ol className="u3-runner-steps"><li>Store the one-time token in the runner secret file.</li><li>Connect the API service to the runner network, then build the runner base if the downloaded file requires it.</li><li>Apply the runtime configuration and start the runner.</li></ol>
    {error ? <p className="form-error" role="alert">{error}</p> : null}
  </div>;
}
