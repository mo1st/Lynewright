import { Button } from "../ui";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import { useDialogFocus } from "../../lib/useDialogFocus";
import type { IntegrationExternalResource, IntegrationManagementConnection, IntegrationManagementResourceType, IntegrationManagementSummary } from "../../types";
import DeclarativeField, { fieldValue } from "./DeclarativeField";
import type { Mutate } from "./sectionTypes";
import { confirmAction } from "../../context/ConfirmContext";

type Props = { integration: IntegrationManagementSummary; connection: IntegrationManagementConnection | null; busy: boolean; mutate: Mutate };
type BindingDraft = { project_id: string; resource_id: string; role: string; ownership_mode: "managed" | "linked" | "observed" };
const EMPTY_BINDING: BindingDraft = { project_id: "", resource_id: "", role: "primary", ownership_mode: "linked" };

function sectionLabel(value: string) { return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function resourceActions(integration: IntegrationManagementSummary, resource: IntegrationExternalResource) { const descriptor = integration.management.resource_types.find((item) => item.type === resource.resource_type); const advertised = new Set(resource.available_actions); return (descriptor?.operations ?? []).filter((action) => ["update", "delete", "archive", "retire", "test"].includes(action) && advertised.has(action) && resource.state === "ACTIVE" && resource.binding?.ownership_mode === "managed"); }
function draftValue(resource: IntegrationExternalResource | null, field: NonNullable<IntegrationManagementResourceType["fields"]>[number]) { if (!resource) return fieldValue(field, {}); if (field.key === "name") return resource.name; if (field.key === "url") return resource.url; return resource.metadata[field.key] ?? field.default ?? ""; }

export default function ResourcesSection({ integration, connection, busy, mutate }: Props) {
  const [resources, setResources] = useState<IntegrationExternalResource[]>([]);
  const [formOpen, setFormOpen] = useState(false);
  const [resourceType, setResourceType] = useState("");
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [editTarget, setEditTarget] = useState<IntegrationExternalResource | null>(null);
  const [bindTarget, setBindTarget] = useState<IntegrationExternalResource | null>(null);
  const [bindForm, setBindForm] = useState<BindingDraft>(EMPTY_BINDING);
  const dialogRef = useRef<HTMLElement>(null);
  useDialogFocus(Boolean(bindTarget), dialogRef, () => setBindTarget(null));
  const refresh = async () => { if (!connection) { setResources([]); return; } const response = await integrationManagementApi.resources(integration.integration.id, { connectionId: connection.id }); setResources(response.items); };
  useEffect(() => { let active = true; void (connection ? integrationManagementApi.resources(integration.integration.id, { connectionId: connection.id }) : Promise.resolve({ items: [] as IntegrationExternalResource[] })).then((response) => { if (active) setResources(response.items); }).catch(() => { if (active) setResources([]); }); return () => { active = false; }; }, [connection, integration.integration.id, integration.integration.enabled]);
  const createTypes = integration.management.resource_types.filter((item) => item.operations.includes("create"));
  const selectedType = integration.management.resource_types.find((item) => item.type === resourceType) ?? createTypes[0];
  const fields = selectedType?.fields ?? [];

  function openCreate() { const type = createTypes[0]; if (!type) return; setEditTarget(null); setResourceType(type.type); setDraft(Object.fromEntries((type.fields ?? []).map((field) => [field.key, field.default ?? (field.type === "boolean" ? false : field.type === "list" ? [] : "")] ))); setFormOpen(true); }
  function openEdit(resource: IntegrationExternalResource) { const type = integration.management.resource_types.find((item) => item.type === resource.resource_type); if (!type) return; setEditTarget(resource); setResourceType(type.type); setDraft(Object.fromEntries((type.fields ?? []).map((field) => [field.key, draftValue(resource, field)]))); setFormOpen(true); }
  function closeForm() { setFormOpen(false); setEditTarget(null); setDraft({}); }
  async function save(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!connection || !selectedType || !fields.length) return; const values = Object.fromEntries(fields.map((field) => [field.key, draft[field.key]])); if (editTarget) { await mutate(`resource:update:${editTarget.id}`, () => integrationManagementApi.resourceAction(integration.integration.id, editTarget.id, { action: "update", arguments: values, expected_revision: editTarget.binding?.revision }), `${selectedType.label} updated and resources refreshed.`); } else { await mutate(`resource:create:${selectedType.type}`, () => integrationManagementApi.createResource(integration.integration.id, connection.id, selectedType.type, values), `${selectedType.label} created and resources refreshed.`); } closeForm(); await refresh(); }
  async function runAction(resource: IntegrationExternalResource, action: string) {
    if (action === "update") { openEdit(resource); return; }
    const destructive = ["delete", "archive", "retire"].includes(action);
    if (destructive && !(await confirmAction({
      title: `${sectionLabel(action)} ${resource.name}?`,
      message: "This action changes the remote resource and cannot be undone here.",
      confirmLabel: `${sectionLabel(action)} resource`, tone: "danger",
    }))) return;
    await mutate(`resource:${resource.id}:${action}`, () => integrationManagementApi.resourceAction(integration.integration.id, resource.id, { action, confirm_destructive: destructive, expected_revision: resource.binding?.revision, idempotency_key: destructive ? `${action}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}` : undefined }), `${sectionLabel(action)} completed for ${resource.name}.`);
    await refresh();
  }
  async function unbind(resource: IntegrationExternalResource) {
    if (!resource.binding || !(await confirmAction({
      title: `Detach ${resource.name}?`,
      message: "The binding will be removed. The remote resource and its history will stay.",
      confirmLabel: "Detach binding", tone: "danger",
    }))) return;
    await mutate(`unbind:${resource.binding.id}`, () => integrationManagementApi.unbind(resource.binding!.id, resource.binding!.revision), "Binding detached; remote resource was not deleted.");
    await refresh();
  }
  async function bind(event: FormEvent<HTMLFormElement>) { event.preventDefault(); if (!bindTarget) return; await mutate(`bind:${bindTarget.id}`, () => integrationManagementApi.bind(bindTarget.id, { ...bindForm, resource_type: bindTarget.resource_type }), "Binding created and queued for verification."); setBindTarget(null); setBindForm(EMPTY_BINDING); await refresh(); }

  if (!connection) return <section className="integration-subsection" aria-label="External resources"><h4>External resources</h4><p className="muted-text">Add and select a connection before discovering external resources.</p></section>;
  return <section className="integration-subsection" aria-label="External resources"><header className="integration-section-header"><div><h4>External resources</h4><p className="muted-text">Resources discovered from the selected connection. Bind only the ones Lynewright should manage or observe.</p></div><div className="button-row">{createTypes.length ? <Button variant="secondary" type="button" disabled={busy || !integration.integration.enabled} onClick={openCreate}>Add {createTypes.length === 1 ? createTypes[0].label.toLowerCase() : "resource"}</Button> : null}<div className="integration-resource-types">{integration.management.resource_types.map((type) => <span key={type.type}>{type.plural_label} · {type.operations.join(", ")}</span>)}</div></div></header>
    {formOpen ? <form className="form-grid" onSubmit={(event) => void save(event)}>{createTypes.length > 1 ? <label htmlFor="resource-create-type">Resource type<select id="resource-create-type" value={resourceType} onChange={(event) => { const type = createTypes.find((item) => item.type === event.target.value); setResourceType(event.target.value); setDraft(Object.fromEntries((type?.fields ?? []).map((field) => [field.key, field.default ?? ""]))); }}>{createTypes.map((type) => <option key={type.type} value={type.type}>{type.label}</option>)}</select></label> : null}{fields.length ? fields.map((field) => <DeclarativeField key={field.key} field={field} id={`resource-field-${field.key.replace(/[^A-Za-z0-9_-]/g, "-")}`} value={fieldValue(field, draft)} disabled={busy || !integration.integration.enabled} onChange={(value) => setDraft((current) => ({ ...current, [field.key]: value }))} />) : <p className="muted-text">This resource does not declare bounded creation fields yet.</p>}<div className="button-row"><Button variant="primary" type="submit" disabled={busy || !integration.integration.enabled || !fields.length}>{editTarget ? `Save ${selectedType?.label ?? "resource"}` : `Create ${selectedType?.label ?? "resource"}`}</Button><Button variant="secondary" type="button" onClick={closeForm}>Cancel</Button></div></form> : null}
    {resources.length ? <div className="table-wrap integration-table-wrap"><table className="responsive-record-table integration-record-table"><thead><tr><th scope="col">Name</th><th scope="col">Type</th><th scope="col">Status</th><th scope="col">Binding</th><th scope="col">Action</th></tr></thead><tbody>{resources.map((resource) => <tr key={resource.id}><td data-label="Resource"><strong>{resource.name}</strong><small className="muted-text">{resource.url || resource.external_id}</small></td><td data-label="Type">{resource.resource_type}</td><td data-label="Status"><span className="integration-status">{resource.state}{resource.remote_status ? ` · ${resource.remote_status}` : ""}</span></td><td data-label="Binding">{resource.binding ? `${resource.binding.state} · ${resource.binding.resource_id}` : "Unbound"}</td><td data-label="Action">{resource.binding ? <div className="integration-actions integration-actions--compact"><div className="integration-actions__primary"><Button variant="secondary" type="button" disabled={busy} onClick={() => void unbind(resource)}>Unbind</Button></div>{resourceActions(integration, resource).length ? <div className="integration-actions__secondary">{resourceActions(integration, resource).map((action) => <Button variant={["delete", "archive", "retire"].includes(action) ? "danger" : "secondary"} key={action} type="button" className={`${["delete", "archive", "retire"].includes(action) ? " " : ""}`} disabled={busy} onClick={() => void runAction(resource, action)}>{sectionLabel(action)}</Button>)}</div> : null}</div> : resource.available_actions.includes("bind") ? <Button variant="secondary" type="button" disabled={busy} onClick={() => { setBindTarget(resource); setBindForm({ ...EMPTY_BINDING, resource_id: resource.id }); }}>Bind</Button> : <span className="muted-text">—</span>}</td></tr>)}</tbody></table></div> : <p className="muted-text">No discovered resources yet. Use Discover on an enabled connection.</p>}
    {bindTarget ? <div className="modal-backdrop" role="presentation"><section ref={dialogRef} className="panel modal-panel" role="dialog" aria-modal="true" aria-labelledby="bind-external-resource-title"><h3 id="bind-external-resource-title">Bind external resource</h3><p className="muted-text">This creates a canonical relationship and does not delete or mutate the remote object.</p><form className="form-grid" onSubmit={(event) => void bind(event)}><label htmlFor="binding-project-id">Project ID<input id="binding-project-id" name="binding-project-id" autoComplete="off" required value={bindForm.project_id} onChange={(event) => setBindForm({ ...bindForm, project_id: event.target.value })} /></label><label htmlFor="binding-resource-id">Canonical resource ID<input id="binding-resource-id" name="binding-resource-id" autoComplete="off" required value={bindForm.resource_id} onChange={(event) => setBindForm({ ...bindForm, resource_id: event.target.value })} /></label><label htmlFor="binding-role">Role<input id="binding-role" name="binding-role" autoComplete="off" required value={bindForm.role} onChange={(event) => setBindForm({ ...bindForm, role: event.target.value })} /></label><label htmlFor="binding-ownership">Ownership<select id="binding-ownership" name="binding-ownership" value={bindForm.ownership_mode} onChange={(event) => setBindForm({ ...bindForm, ownership_mode: event.target.value as BindingDraft["ownership_mode"] })}><option value="linked">Linked</option><option value="observed">Observed</option><option value="managed">Managed</option></select></label><div className="button-row"><Button variant="primary" type="submit" disabled={busy}>Create binding</Button><Button variant="secondary" type="button" onClick={() => setBindTarget(null)}>Cancel</Button></div></form></section></div> : null}
  </section>;
}
