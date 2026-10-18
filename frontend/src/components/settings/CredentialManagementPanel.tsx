import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, describeApiError, type CredentialMetadata, type CredentialReference } from "../../api/client";
import { Badge, Button, Dialog, FormField, Input, Section, Select, Table, useToast } from "../ui";

export function CredentialManagementPanel() {
  const { addToast } = useToast();
  const [items, setItems] = useState<CredentialMetadata[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [target, setTarget] = useState<CredentialMetadata | null>(null);
  const [references, setReferences] = useState<CredentialReference[]>([]);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("token");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true); setError("");
    try { setItems(await api.getCredentials()); }
    catch (caught) { setError(describeApiError(caught, "Credentials could not be loaded.")); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void reload(); }, [reload]);

  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try { await api.saveCredential({ name: name.trim(), kind, secret }); setFormOpen(false); setName(""); setSecret(""); await reload(); addToast({ tone: "success", message: "Credential saved. Its secret is not shown again." }); }
    catch (caught) { setError(describeApiError(caught, "Credential could not be saved.")); }
    finally { setBusy(false); }
  }
  async function openRemove(item: CredentialMetadata) {
    setTarget(item); setError(""); setReferences([]);
    try { setReferences(await api.getCredentialReferences(item.name)); }
    catch (caught) { setError(describeApiError(caught, "Credential references could not be checked.")); }
  }
  async function remove() {
    if (!target || references.length) return;
    setBusy(true); setError("");
    try { await api.deleteCredential(target.name); setTarget(null); await reload(); addToast({ tone: "success", message: "Credential removed." }); }
    catch (caught) { setError(describeApiError(caught, "Credential could not be removed.")); }
    finally { setBusy(false); }
  }

  return <div className="page-stack">
    <Section title="Credentials" description="Store named secret references. Secret values are write-only." actions={<Button variant="primary" onClick={() => { setName(""); setSecret(""); setFormOpen(true); }}>Add credential</Button>}>
      {error && !target && !formOpen ? <p role="alert" className="form-error">{error}</p> : null}
      <Table label="Stored credentials" rows={items} loading={loading} empty="No credentials stored." getRowKey={(item) => item.name} columns={[
        { key: "name", header: "Name", render: (item) => <strong>{item.name}</strong> },
        { key: "kind", header: "Kind", render: (item) => <Badge tone="neutral">{item.kind || "Token"}</Badge> },
        { key: "updated", header: "Updated", render: (item) => item.updated_at ? new Date(item.updated_at).toLocaleDateString() : "—" },
      ]} rowActions={(item) => <Button variant="danger" size="sm" onClick={() => void openRemove(item)}>Remove</Button>} />
    </Section>
    <Dialog open={formOpen} title="Add credential" description="Only the named reference is visible after saving. The secret value cannot be retrieved." onClose={() => setFormOpen(false)}>
      <form className="page-stack" onSubmit={(event) => void save(event)}>
        {error ? <p role="alert" className="form-error">{error}</p> : null}
        <FormField label="Name" htmlFor="credential-name" required><Input id="credential-name" pattern="[A-Za-z][A-Za-z0-9_.-]{0,127}" value={name} onChange={(event) => setName(event.target.value)} required /></FormField>
        <FormField label="Kind" htmlFor="credential-kind"><Select id="credential-kind" value={kind} onChange={(event) => setKind(event.target.value)}><option value="token">Token</option><option value="api_key">API key</option><option value="password">Password</option><option value="oauth">OAuth</option></Select></FormField>
        <FormField label="Secret value" htmlFor="credential-secret" required><Input id="credential-secret" type="password" autoComplete="new-password" value={secret} onChange={(event) => setSecret(event.target.value)} required /></FormField>
        <footer className="dialog-actions"><Button variant="primary" type="submit" busy={busy}>Save credential</Button><Button variant="secondary" type="button" onClick={() => setFormOpen(false)}>Cancel</Button></footer>
      </form>
    </Dialog>
    <Dialog open={Boolean(target)} title={`Remove ${target?.name ?? "credential"}?`} description="Credentials are not removed with integrations because they may be shared." onClose={() => setTarget(null)}>
      {target ? <div className="page-stack">
        {error ? <p role="alert" className="form-error">{error}</p> : null}
        {references.length ? <><p>This credential is still used by:</p><ul>{references.map((item) => <li key={`${item.consumer}:${item.label}`}>{item.consumer} · {item.label}</li>)}</ul><p>Detach those references before removing it.</p></> : <p>No consumers reference this credential. Its encrypted value will be deleted.</p>}
        <footer className="dialog-actions"><Button variant="danger" disabled={references.length > 0} busy={busy} onClick={() => void remove()}>Remove credential</Button><Button variant="secondary" onClick={() => setTarget(null)}>Cancel</Button></footer>
      </div> : null}
    </Dialog>
  </div>;
}
