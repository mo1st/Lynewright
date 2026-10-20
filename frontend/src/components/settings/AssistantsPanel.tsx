import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, describeApiError, type AutomationPlan, type PlatformMcpClient } from "../../api/client";
import { confirmAction } from "../../context/ConfirmContext";
import { Badge, Button, Checkbox, CopyField, Dialog, EmptyState, FormField, Input, Section, Table, useToast } from "../ui";
import styles from "./AssistantsPanel.module.css";

const TOOL_SCOPES = [
  { key: "automation:read", label: "Read plan previews and approval status" },
  { key: "integrations:discover", label: "Search installed operation schemas" },
  { key: "automation:plan", label: "Create or revise automation plans" },
  { key: "automation:provision", label: "Apply a plan after an administrator approves it" },
  { key: "lines:read", label: "Read lines" },
  { key: "runs:read", label: "Read runs and outputs" },
  { key: "runs:start", label: "Start runs" },
  { key: "runs:cancel", label: "Cancel runs" },
  { key: "runs:retry", label: "Retry failed stages" },
  { key: "reviews:read", label: "Read waiting reviews" },
  { key: "reviews:decide", label: "Decide reviews when a line allows it" },
] as const;

function relativeTime(value: string | null): string {
  if (!value) return "Never";
  const time = new Date(value.includes("T") ? value : `${value.replace(" ", "T")}Z`).getTime();
  if (!Number.isFinite(time)) return value;
  const minutes = Math.round((time - Date.now()) / 60_000);
  if (Math.abs(minutes) < 1) return "Just now";
  if (Math.abs(minutes) < 60) return `${Math.abs(minutes)} min ${minutes < 0 ? "ago" : "from now"}`;
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return `${Math.abs(hours)} hr ${hours < 0 ? "ago" : "from now"}`;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(time);
}

export function AssistantsPanel({ projectId }: { projectId: string }) {
  const { addToast } = useToast();
  const [clients, setClients] = useState<PlatformMcpClient[]>([]);
  const [plans, setPlans] = useState<AutomationPlan[]>([]);
  const [mcpUrl, setMcpUrl] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [audience, setAudience] = useState<"mcp" | "api">("mcp");
  const [scopes, setScopes] = useState<string[]>(["lines:read", "runs:read", "runs:start", "reviews:read"]);
  const [created, setCreated] = useState<(PlatformMcpClient & { access_token: string }) | null>(null);
  const [busy, setBusy] = useState(false);
  const reload = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const [clientsResult, plansResult] = await Promise.allSettled([
        api.listPlatformMcpClients(),
        projectId && typeof api.listAutomationPlans === "function" ? api.listAutomationPlans(projectId) : Promise.resolve({ items: [] }),
      ]);
      if (clientsResult.status === "fulfilled") {
        setClients(clientsResult.value.clients);
        setMcpUrl(clientsResult.value.mcp_url);
      } else {
        setError(describeApiError(clientsResult.reason, "Assistant clients could not be loaded."));
      }
      if (plansResult.status === "fulfilled") {
        setPlans(plansResult.value.items);
      } else {
        setError(describeApiError(plansResult.reason, "Automation plans could not be loaded."));
      }
    }
    catch (caught) { setError(describeApiError(caught, "Assistant settings could not be loaded.")); }
    finally { setLoading(false); }
  }, [projectId]);
  useEffect(() => { void reload(); }, [reload]);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const result = await api.createPlatformMcpClient({
        name: name.trim(), client_type: "mcp", audience, permission_mode: "custom", granted_capabilities: scopes,
      });
      setCreated(result); setCreateOpen(false); setName("");
      await reload();
      addToast({ tone: "success", message: "Assistant client created. Copy its token now; it will not be shown again." });
    } catch (caught) { setError(describeApiError(caught, "Assistant client could not be created.")); }
    finally { setBusy(false); }
  }

  async function remove(client: PlatformMcpClient) {
    if (!(await confirmAction({ title: `Remove ${client.name}?`, message: "The assistant token will stop working immediately.", confirmLabel: "Remove assistant", tone: "danger" }))) return;
    try { await api.deletePlatformMcpClient(client.id); await reload(); addToast({ tone: "success", message: "Assistant client removed." }); }
    catch (caught) { setError(describeApiError(caught, "Assistant client could not be removed.")); }
  }

  async function approvePlan(plan: AutomationPlan) {
    const confirmed = await confirmAction({
      title: `Approve ${plan.line_preview.name}?`,
      message: `This approves revision ${plan.revision} for ${plan.objective}. The assistant may create this disabled line in the selected project. It will not enable or run the line.`,
      confirmLabel: "Approve preview",
    });
    if (!confirmed) return;
    try {
      const updated = await api.authorizeAutomationPlan(plan.id, {
        project_id: plan.project_id, revision: plan.revision, plan_hash: plan.plan_hash,
      });
      setPlans((current) => current.map((item) => item.id === updated.id ? updated : item));
      addToast({ tone: "success", message: "Plan revision approved. The assistant can now apply the disabled preview line." });
    } catch (caught) {
      setError(describeApiError(caught, "Plan revision could not be approved."));
    }
  }

  const columns = [
    { key: "name", header: "Assistant", render: (client: PlatformMcpClient) => <div className={styles.clientIdentity}><strong>{client.name}</strong><small><Badge tone={client.enabled ? "success" : "neutral"}>{client.enabled ? "Enabled" : "Disabled"}</Badge><Badge tone="neutral">{client.audience === "api" ? "REST API" : "MCP"}</Badge>{client.project_ids.join(", ") || "No project access"}</small></div> },
    { key: "last-used", header: "Last used", render: (client: PlatformMcpClient) => <time title={client.last_authenticated_at || "Never connected"}>{relativeTime(client.last_authenticated_at)}</time> },
  ];

  return <div className={styles.panel}>
    <Section title="Assistants" description="Connect an assistant with selected line and automation tools.">
      {error ? <p role="alert" className={styles.error}>{error}</p> : null}
      <p className={styles.explainer}>Assistant access is limited to the selected project. Plan creation and applying an approved preview are separate permissions; applying creates a disabled line for you to review.</p>
      {mcpUrl ? <div className={styles.connection}>
        <CopyField label="MCP connection URL" value={mcpUrl} />
        {!mcpUrl.toLowerCase().startsWith("https://") ? <p className={styles.connectionWarning}><Badge tone="warning">HTTPS needed for web assistants</Badge> Add a TLS proxy or tunnel before connecting an assistant over the internet.</p> : null}
      </div> : null}
      {clients.length ? <Table label="Connected assistant clients" columns={columns} rows={clients} getRowKey={(client) => client.id} loading={loading} rowActions={(client) => <Button variant="danger" size="sm" onClick={() => void remove(client)}>Remove</Button>} /> : loading ? <p role="status">Loading assistant clients…</p> : <EmptyState title="No assistants connected" description="Add an MCP client to let an assistant use selected production-line tools." />}
      <div className={styles.actions}><Button variant="primary" onClick={() => { setError(""); setCreateOpen(true); }}>Add assistant</Button></div>
    </Section>

    <Section title="Automation plans" description="Review the exact line definition before an assistant can add it to this project.">
      <div className={styles.actions}><Button variant="secondary" onClick={() => void reload()} busy={loading}>Refresh plans</Button></div>
      {plans.length ? <div className={styles.form}>{plans.map((plan) => <article key={plan.id}>
        <h3>{plan.line_preview.name}</h3>
        <p>{plan.objective}</p>
        <p>Revision {plan.revision} · {plan.status} · {plan.operation_snapshots.length} installed operation{plan.operation_snapshots.length === 1 ? "" : "s"}</p>
        <details><summary>Review line preview and operations</summary>
          <h4>Constraints</h4><pre>{JSON.stringify(plan.constraints, null, 2)}</pre>
          <ul>{plan.operation_snapshots.map((operation) => <li key={operation.operation_id}><code>{operation.contract}.{operation.operation}</code> · {operation.capability} · {operation.risk_class}</li>)}</ul>
          <pre>{JSON.stringify(plan.line_preview.definition, null, 2)}</pre>
        </details>
        {plan.status === "ready" && !plan.line_preview.persisted ? <p role="status">The preview line was removed.</p>
          : plan.status === "ready" && plan.line_preview.enabled ? <p role="status">The preview line exists and is enabled.</p>
            : plan.status === "ready" ? <p role="status">Preview line created. The line is not enabled.</p>
          : plan.authorization.approved ? <p role="status">Approved by {plan.authorization.approved_by}. The assistant can apply this revision.</p>
            : plan.expires_at_epoch <= Date.now() / 1000 ? <p role="status">This plan expired. Ask the assistant to create a new revision before approval.</p>
              : <Button variant="primary" onClick={() => void approvePlan(plan)}>Approve revision</Button>}
      </article>)}</div> : loading ? <p role="status">Loading automation plans…</p> : <EmptyState title="No automation plans" description="Plans created by an assistant will appear here for review." />}
    </Section>

    <Dialog open={createOpen} title="Add assistant" description="Choose its connection type and tools. Its access token is shown once after creation." onClose={() => setCreateOpen(false)}>
      <form className={styles.form} onSubmit={(event) => void create(event)}>
        {error ? <p role="alert" className={styles.error}>{error}</p> : null}
        <FormField label="Assistant name" htmlFor="assistant-client-name" required><Input id="assistant-client-name" value={name} onChange={(event) => setName(event.target.value)} required maxLength={120} autoComplete="off" /></FormField>
        <fieldset className={styles.scopeList}><legend>Connection type</legend>
          <label><input type="radio" name="assistant-audience" checked={audience === "mcp"} onChange={() => setAudience("mcp")} /> MCP client</label>
          <label><input type="radio" name="assistant-audience" checked={audience === "api"} onChange={() => setAudience("api")} /> REST API client</label>
        </fieldset>
        <fieldset className={styles.scopeList}><legend>Allowed tools</legend>{TOOL_SCOPES.map((scope) => <Checkbox key={scope.key} label={scope.label} checked={scopes.includes(scope.key)} onChange={(event) => setScopes((current) => event.target.checked ? [...current, scope.key] : current.filter((item) => item !== scope.key))} />)}</fieldset>
        <p className={styles.muted}>This client is limited to the current project: {projectId || "No project selected"}.</p>
        <footer className={styles.dialogActions}><Button variant="primary" type="submit" busy={busy} disabled={!projectId}>Create client</Button><Button variant="secondary" type="button" onClick={() => setCreateOpen(false)}>Cancel</Button></footer>
      </form>
    </Dialog>

    <Dialog open={Boolean(created)} title="Assistant connected" description="Copy the access token now. Lynewright will not show it again." onClose={() => setCreated(null)}>
      {created ? <div className={styles.form}>
        <p>{created.audience === "api" ? "Send requests to this REST endpoint with the token as a Bearer credential." : "Enter this connection URL and token in your MCP client."}</p>
        <CopyField label={created.audience === "api" ? "REST automation endpoint" : "MCP connection URL"} value={created.audience === "api" ? mcpUrl.replace(/\/mcp$/, "/api/automation") : mcpUrl} />
        <CopyField label="One-time access token" value={created.access_token} />
        <p className={styles.muted}>Keep the token private. Remove this client here to revoke it.</p>
        <footer className={styles.dialogActions}><Button variant="primary" onClick={() => setCreated(null)}>Done</Button></footer>
      </div> : null}
    </Dialog>
  </div>;
}
