import { useCallback, useEffect, useState } from "react";
import { api, describeApiError, type ManagedMedia } from "../../api/client";
import { Button, Dialog, Section, Table, useToast } from "../ui";

function bytes(value: number): string { return value < 1_000_000 ? `${Math.max(1, Math.round(value / 1000))} KB` : `${(value / 1_000_000).toFixed(1)} MB`; }

export function MediaManagementPanel() {
  const { addToast } = useToast();
  const [items, setItems] = useState<ManagedMedia[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [target, setTarget] = useState<ManagedMedia | null>(null);
  const [busy, setBusy] = useState(false);
  const reload = useCallback(async () => {
    setLoading(true); setError("");
    try { setItems((await api.listManagedMedia()).items); }
    catch (caught) { setError(describeApiError(caught, "Managed media could not be loaded.")); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void reload(); }, [reload]);
  async function remove() {
    if (!target) return;
    setBusy(true); setError("");
    try { await api.deleteManagedMedia(target.id); setTarget(null); await reload(); addToast({ tone: "success", message: "Media removed." }); }
    catch (caught) { setError(describeApiError(caught, "Media could not be removed.")); }
    finally { setBusy(false); }
  }
  return <div className="page-stack">
    <Section title="Managed media" description="Remove generated images and files stored by integrations. This does not remove line run history.">
      {error && !target ? <p role="alert" className="form-error">{error}</p> : null}
      <Table label="Managed media" rows={items} loading={loading} empty="No managed media stored." getRowKey={(item) => item.id} columns={[
        { key: "name", header: "File", render: (item) => <strong>{item.name}</strong> },
        { key: "type", header: "Type", render: (item) => item.content_type },
        { key: "size", header: "Size", render: (item) => bytes(item.bytes) },
        { key: "owner", header: "Integration", render: (item) => item.integration_id || "—" },
      ]} rowActions={(item) => <Button variant="danger" size="sm" onClick={() => setTarget(item)}>Remove</Button>} />
    </Section>
    <Dialog open={Boolean(target)} title={`Remove ${target?.name ?? "media"}?`} description="The stored file and its media links will be removed. Run history remains." onClose={() => setTarget(null)}>
      {target ? <div className="page-stack">{error ? <p role="alert" className="form-error">{error}</p> : null}<p>{target.content_type} · {bytes(target.bytes)}</p><footer className="dialog-actions"><Button variant="danger" busy={busy} onClick={() => void remove()}>Remove media</Button><Button variant="secondary" onClick={() => setTarget(null)}>Cancel</Button></footer></div> : null}
    </Dialog>
  </div>;
}
