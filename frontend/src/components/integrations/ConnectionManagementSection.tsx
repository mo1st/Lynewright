import { Button } from "../ui";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import type { CredentialMetadata } from "../../api/client";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationManagementConnection, IntegrationOAuthStatus } from "../../types";
import CredentialReferencePicker from "./CredentialReferencePicker";
import type { Mutate } from "./sectionTypes";
import { confirmAction } from "../../context/ConfirmContext";

export type ConnectionDraft = {
  name: string;
  endpoint_url: string;
  internal_endpoint_ref: string;
  origin_kind: "public" | "internal_endpoint";
  credential_refs: string;
  project_id: string;
};

type Props = {
  integrationId: string;
  connections: IntegrationManagementConnection[];
  selectedConnectionId: string;
  integrationEnabled: boolean;
  oauthSupported: boolean;
  busy: boolean;
  credentialOptions: CredentialMetadata[];
  mutate: Mutate;
  onSelect: (connectionId: string) => void;
  onDiscover: (connectionId: string) => void;
};

const EMPTY_DRAFT: ConnectionDraft = { name: "", endpoint_url: "", internal_endpoint_ref: "", origin_kind: "public", credential_refs: "", project_id: "" };
const OAUTH_HANDOFF_KEY = "lynewright.integration-management.oauth-handoff";

function credentialNames(value: string) {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function oauthCredentialRef(integrationId: string, connectionId: string, kind: "access" | "refresh") {
  return `oauth.${integrationId}.${connectionId}.${kind}`.replace(/[^A-Za-z0-9_.-]/g, "-").slice(0, 240);
}

export default function ConnectionManagementSection({ integrationId, connections, selectedConnectionId, integrationEnabled, oauthSupported, busy, credentialOptions, mutate, onSelect, onDiscover }: Props) {
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ConnectionDraft>(EMPTY_DRAFT);
  const [oauthStatuses, setOauthStatuses] = useState<Record<string, IntegrationOAuthStatus>>({});
  const [connectionItems, setConnectionItems] = useState(connections);
  const selectedConnection = connectionItems.find((connection) => connection.id === selectedConnectionId) ?? null;

  useEffect(() => { setConnectionItems(connections); }, [connections]);
  useEffect(() => { let active = true; void integrationManagementApi.connections(integrationId).then((response) => { if (active) setConnectionItems(response.items); }).catch(() => undefined); return () => { active = false; }; }, [integrationId]);

  useEffect(() => {
    if (!oauthSupported || !selectedConnectionId) return;
    let active = true;
    void integrationManagementApi.oauthStatus(integrationId, selectedConnectionId).then((status) => {
      if (active) setOauthStatuses((current) => ({ ...current, [selectedConnectionId]: status }));
    }).catch(() => undefined);
    return () => { active = false; };
  }, [integrationId, oauthSupported, selectedConnectionId]);

  const set = (key: keyof ConnectionDraft, value: string) => setDraft((current) => ({ ...current, [key]: value }));
  const openAdd = () => { setEditingId(null); setDraft(EMPTY_DRAFT); setFormOpen(true); };
  const openEdit = (connection: IntegrationManagementConnection) => {
    setEditingId(connection.id);
    setDraft({ name: connection.name, endpoint_url: connection.endpoint_url, internal_endpoint_ref: connection.internal_endpoint_ref, origin_kind: connection.origin_kind === "internal_endpoint" ? "internal_endpoint" : "public", credential_refs: connection.credential_refs.join(", "), project_id: connection.project_id ?? "" });
    setFormOpen(true);
  };

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const credential_refs = credentialNames(draft.credential_refs);
    await mutate(editingId ? `connection-save:${editingId}` : "connection-create", async () => {
      if (editingId) await integrationManagementApi.configureConnection(integrationId, editingId, { ...(draft.origin_kind === "public" ? { endpoint_url: draft.endpoint_url } : {}), credential_refs, project_id: draft.project_id || undefined });
      else await integrationManagementApi.createConnection(integrationId, { name: draft.name, endpoint_url: draft.origin_kind === "public" ? draft.endpoint_url : "", internal_endpoint_ref: draft.origin_kind === "internal_endpoint" ? draft.internal_endpoint_ref : "", origin_kind: draft.origin_kind, credential_refs, project_id: draft.project_id || undefined });
      setFormOpen(false); setEditingId(null);
    }, editingId ? "Connection updated." : "Connection added.");
  }

  async function test(connectionId: string) {
    if (!integrationEnabled) return;
    await mutate(`connection-test:${connectionId}`, () => integrationManagementApi.testConnection(integrationId, connectionId), "Connection test completed.");
  }

  async function toggle(connection: IntegrationManagementConnection) {
    await mutate(`connection-toggle:${connection.id}`, () => connection.enabled ? integrationManagementApi.disableConnection(integrationId, connection.id) : integrationManagementApi.enableConnection(integrationId, connection.id), connection.enabled ? "Connection disabled." : "Connection enabled.");
  }

  async function remove(connection: IntegrationManagementConnection) {
    if (!(await confirmAction({
      title: `Remove ${connection.name}?`,
      message: "The connection and its mappings will be removed. Detach active bindings first. Remote resources and stored credentials will stay.",
      confirmLabel: "Remove connection", tone: "danger",
    }))) return;
    await mutate(`connection-remove:${connection.id}`, () => integrationManagementApi.removeConnection(integrationId, connection.id), "Connection removed. Remote resources and credentials were not deleted.");
  }

  async function authorize(connectionId: string) {
    const callbackUrl = new URL(window.location.href); callbackUrl.search = "";
    const handoff = { integrationId, connectionId, returnUrl: callbackUrl.toString(), accessCredentialRef: oauthCredentialRef(integrationId, connectionId, "access"), refreshCredentialRef: oauthCredentialRef(integrationId, connectionId, "refresh") };
    window.sessionStorage.setItem(OAUTH_HANDOFF_KEY, JSON.stringify(handoff));
    try {
      const started = await integrationManagementApi.oauthBegin(integrationId, connectionId, handoff.returnUrl);
      window.location.assign(started.authorization_url);
    } catch { window.sessionStorage.removeItem(OAUTH_HANDOFF_KEY); }
  }
  async function refreshOAuth(connectionId: string) {
    await mutate(`oauth:${connectionId}`, () => integrationManagementApi.oauthRefresh(integrationId, connectionId), "OAuth token refreshed.");
  }

  return <section className="integration-subsection" aria-label="Connection management">
    <header className="panel-header"><div><h4>Manage connections</h4><p className="muted-text">Choose one connection for discovery, health, OAuth, and remote resource actions.</p></div><Button variant="secondary" type="button" disabled={busy} onClick={openAdd}>Add connection</Button></header>
    {formOpen ? <form className="form-grid" onSubmit={(event) => void save(event)}>
      <label htmlFor="connection-name">Name<input id="connection-name" name="connection-name" autoComplete="off" required disabled={Boolean(editingId)} value={draft.name} onChange={(event) => set("name", event.target.value)} /></label>
      {!editingId ? <label htmlFor="connection-origin">Origin<select id="connection-origin" name="connection-origin" value={draft.origin_kind} onChange={(event) => set("origin_kind", event.target.value)}><option value="public">Public endpoint</option><option value="internal_endpoint">Internal endpoint reference</option></select></label> : null}
      {draft.origin_kind === "public" ? <label htmlFor="connection-endpoint">Endpoint URL<input id="connection-endpoint" name="connection-endpoint" required type="url" autoComplete="url" value={draft.endpoint_url} onChange={(event) => set("endpoint_url", event.target.value)} /></label> : <label htmlFor="connection-internal-ref">Internal endpoint reference<input id="connection-internal-ref" name="connection-internal-ref" required autoComplete="off" value={draft.internal_endpoint_ref} onChange={(event) => set("internal_endpoint_ref", event.target.value)} /></label>}
      {credentialOptions.length ? <CredentialReferencePicker id="connection-stored-credentials" selected={credentialNames(draft.credential_refs)} options={credentialOptions} onChange={(values) => set("credential_refs", values.join(", "))} /> : <p className="muted-text">No stored credentials are available. Add one from Credential management before connecting.</p>}
      <details className="settings-advanced form-grid__wide"><summary><strong>Advanced credential reference</strong><span>Use an existing reference not shown in the picker</span></summary><label htmlFor="connection-credential-refs">Credential refs<input id="connection-credential-refs" name="credential-refs" autoComplete="off" value={draft.credential_refs} onChange={(event) => set("credential_refs", event.target.value)} placeholder="existing-ref, another-ref" /><small className="muted-text">Reference names only; secret values are never accepted here.</small></label></details>
      <label htmlFor="connection-project">Project scope (optional)<input id="connection-project" name="connection-project" autoComplete="off" value={draft.project_id} onChange={(event) => set("project_id", event.target.value)} /></label>
      <div className="button-row"><Button variant="primary" type="submit" disabled={busy}>{editingId ? "Save changes" : "Add connection"}</Button><Button variant="secondary" type="button" onClick={() => setFormOpen(false)}>Cancel</Button></div>
    </form> : null}
    {connectionItems.length ? <div className="table-wrap integration-table-wrap"><table className="responsive-record-table integration-record-table"><thead><tr><th scope="col">Name</th><th scope="col">Endpoint</th><th scope="col">Health</th><th scope="col">Actions</th></tr></thead><tbody>{connectionItems.map((connection) => <tr key={connection.id}><td data-label="Connection"><strong>{connection.name}</strong>{connection.id === selectedConnectionId ? <small className="integration-selected">Selected</small> : null}</td><td data-label="Endpoint" className="mono-text">{connection.origin_kind === "internal_endpoint" ? connection.internal_endpoint_ref : connection.endpoint_url || "—"}</td><td data-label="Health"><span className="integration-status">{String(connection.health?.state ?? "UNKNOWN")}</span>{connection.last_error ? <small className="form-error">{connection.last_error}</small> : null}</td><td data-label="Actions"><div className="integration-actions"><div className="integration-actions__primary"><Button variant="secondary" type="button" disabled={busy} onClick={() => onSelect(connection.id)}>{connection.id === selectedConnectionId ? "Selected" : "Select"}</Button><Button variant="secondary" type="button" disabled={busy || !integrationEnabled || !connection.enabled} onClick={() => onDiscover(connection.id)}>Discover</Button></div><div className="integration-actions__secondary"><Button variant="secondary" type="button" disabled={busy} onClick={() => openEdit(connection)}>Edit</Button><Button variant="secondary" type="button" disabled={busy || !integrationEnabled || !connection.enabled} onClick={() => void test(connection.id)}>Test</Button><Button variant="secondary" type="button" disabled={busy} onClick={() => void toggle(connection)}>{connection.enabled ? "Disable" : "Enable"}</Button><Button variant="danger" type="button"  disabled={busy} onClick={() => void remove(connection)}>Remove</Button></div></div></td></tr>)}</tbody></table></div> : <p className="muted-text">No connections configured. Add one to start.</p>}
    {oauthSupported ? <section className="integration-subsection" aria-label="OAuth"><h4>OAuth</h4><p className="muted-text">Authorization is owned by the selected connection. Tokens are never displayed.</p>{selectedConnection ? <div className="button-row"><span>{oauthStatuses[selectedConnection.id]?.state ?? "Loading…"}{oauthStatuses[selectedConnection.id]?.scope ? ` · ${oauthStatuses[selectedConnection.id]?.scope}` : ""}</span>{oauthStatuses[selectedConnection.id]?.state === "AUTHORIZED" || oauthStatuses[selectedConnection.id]?.state === "EXPIRED" ? <><Button variant="secondary" type="button" disabled={busy || !integrationEnabled || !selectedConnection.enabled} onClick={() => void refreshOAuth(selectedConnection.id)}>Refresh</Button><Button variant="secondary" type="button" disabled={busy || !integrationEnabled || !selectedConnection.enabled} onClick={() => void authorize(selectedConnection.id)}>Re-authorize</Button></> : <Button variant="secondary" type="button" disabled={busy || !integrationEnabled || !selectedConnection.enabled} onClick={() => void authorize(selectedConnection.id)}>Authorize</Button>}</div> : <p className="muted-text">Add a connection before authorizing OAuth.</p>}</section> : null}
  </section>;
}
