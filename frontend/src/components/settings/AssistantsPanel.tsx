import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, describeApiError, type PlatformMcpClient } from "../../api/client";
import { confirmAction } from "../../context/ConfirmContext";
import { Badge, Button, Checkbox, CopyField, Dialog, EmptyState, FormField, Input, Section, Table, useToast } from "../ui";
import styles from "./AssistantsPanel.module.css";

const TOOL_SCOPES = [
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
  const [mcpUrl, setMcpUrl] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["lines:read", "runs:read", "runs:start", "reviews:read"]);
  const [created, setCreated] = useState<(PlatformMcpClient & { access_token: string }) | null>(null);
  const [busy, setBusy] = useState(false);
  const reload = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const result = await api.listPlatformMcpClients();
      setClients(result.clients);
      setMcpUrl(result.mcp_url);
    }
    catch (caught) { setError(describeApiError(caught, "Assistant clients could not be loaded.")); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void reload(); }, [reload]);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const result = await api.createPlatformMcpClient({
        name: name.trim(), client_type: "mcp", permission_mode: "custom", granted_capabilities: scopes,
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

  const columns = [
    { key: "name", header: "Assistant", render: (client: PlatformMcpClient) => <div className={styles.clientIdentity}><strong>{client.name}</strong><small><Badge tone={client.enabled ? "success" : "neutral"}>{client.enabled ? "Enabled" : "Disabled"}</Badge>{client.project_ids.join(", ") || "No project access"}</small></div> },
    { key: "last-used", header: "Last used", render: (client: PlatformMcpClient) => <time title={client.last_authenticated_at || "Never connected"}>{relativeTime(client.last_authenticated_at)}</time> },
  ];

  return <div className={styles.panel}>
    <Section title="Assistants" description="Connect an MCP client to start lines and review their results.">
      {error ? <p role="alert" className={styles.error}>{error}</p> : null}
      <p className={styles.explainer}>Assistant tools use the selected project and the same line runner, review queue, and account permissions as the dashboard.</p>
      {mcpUrl ? <div className={styles.connection}>
        <CopyField label="MCP connection URL" value={mcpUrl} />
        {!mcpUrl.toLowerCase().startsWith("https://") ? <p className={styles.connectionWarning}><Badge tone="warning">HTTPS needed for web assistants</Badge> Add a TLS proxy or tunnel before connecting an assistant over the internet.</p> : null}
      </div> : null}
      {clients.length ? <Table label="Connected assistant clients" columns={columns} rows={clients} getRowKey={(client) => client.id} loading={loading} rowActions={(client) => <Button variant="danger" size="sm" onClick={() => void remove(client)}>Remove</Button>} /> : loading ? <p role="status">Loading assistant clients…</p> : <EmptyState title="No assistants connected" description="Add an MCP client to let an assistant use selected production-line tools." />}
      <div className={styles.actions}><Button variant="primary" onClick={() => { setError(""); setCreateOpen(true); }}>Add assistant</Button></div>
    </Section>

    <Dialog open={createOpen} title="Add assistant" description="Choose the tools this client can use. Its access token is shown once after creation." onClose={() => setCreateOpen(false)}>
      <form className={styles.form} onSubmit={(event) => void create(event)}>
        {error ? <p role="alert" className={styles.error}>{error}</p> : null}
        <FormField label="Assistant name" htmlFor="assistant-client-name" required><Input id="assistant-client-name" value={name} onChange={(event) => setName(event.target.value)} required maxLength={120} autoComplete="off" /></FormField>
        <fieldset className={styles.scopeList}><legend>Allowed tools</legend>{TOOL_SCOPES.map((scope) => <Checkbox key={scope.key} label={scope.label} checked={scopes.includes(scope.key)} onChange={(event) => setScopes((current) => event.target.checked ? [...current, scope.key] : current.filter((item) => item !== scope.key))} />)}</fieldset>
        <p className={styles.muted}>This client is limited to the current project: {projectId || "No project selected"}.</p>
        <footer className={styles.dialogActions}><Button variant="primary" type="submit" busy={busy} disabled={!projectId}>Create client</Button><Button variant="secondary" type="button" onClick={() => setCreateOpen(false)}>Cancel</Button></footer>
      </form>
    </Dialog>

    <Dialog open={Boolean(created)} title="Assistant connected" description="Copy the access token now. Lynewright will not show it again." onClose={() => setCreated(null)}>
      {created ? <div className={styles.form}>
        <p>Enter this connection URL and token in your MCP client.</p>
        <CopyField label="MCP connection URL" value={mcpUrl} />
        <CopyField label="One-time access token" value={created.access_token} />
        <p className={styles.muted}>Keep the token private. Remove this client here to revoke it.</p>
        <footer className={styles.dialogActions}><Button variant="primary" onClick={() => setCreated(null)}>Done</Button></footer>
      </div> : null}
    </Dialog>
  </div>;
}
