import { Button } from "../ui";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationMappingPreview, IntegrationOpenApiOperation, IntegrationOpenApiResourceMapping } from "../../types";
import type { Mutate } from "./sectionTypes";
import { confirmAction } from "../../context/ConfirmContext";

export type OpenApiResourceMappingDraft = {
  resource_type: string;
  label: string;
  plural_label: string;
  list_operation_id: string;
  get_operation_id: string;
  create_operation_id: string;
  update_operation_id: string;
  delete_operation_id: string;
  get_external_id_target: string;
  update_external_id_target: string;
  delete_external_id_target: string;
  items_path: string;
  id_path: string;
  name_path: string;
  parent_id_path: string;
  url_path: string;
  status_path: string;
};

const EMPTY_DRAFT: OpenApiResourceMappingDraft = {
  resource_type: "", label: "", plural_label: "", list_operation_id: "", get_operation_id: "",
  create_operation_id: "", update_operation_id: "", delete_operation_id: "", get_external_id_target: "",
  update_external_id_target: "", delete_external_id_target: "", items_path: "", id_path: "", name_path: "",
  parent_id_path: "", url_path: "", status_path: "",
};

type Props = {
  integrationId: string;
  connectionId: string;
  integrationEnabled: boolean;
  busy: boolean;
  operations: IntegrationOpenApiOperation[];
  mutate: Mutate;
};

function payload(draft: OpenApiResourceMappingDraft, mappingId?: string | null) {
  return {
    ...(mappingId ? { mapping_id: mappingId } : {}),
    ...draft,
    get_operation_id: draft.get_operation_id || null,
    create_operation_id: draft.create_operation_id || null,
    update_operation_id: draft.update_operation_id || null,
    delete_operation_id: draft.delete_operation_id || null,
    get_external_id_target: draft.get_external_id_target || null,
    update_external_id_target: draft.update_external_id_target || null,
    delete_external_id_target: draft.delete_external_id_target || null,
    parent_id_path: draft.parent_id_path || null,
    url_path: draft.url_path || null,
    status_path: draft.status_path || null,
    display_fields: [], metadata_paths: {}, pagination: null, enabled: true,
  };
}

function operationLabel(operation: IntegrationOpenApiOperation) {
  return `${operation.operation_id} · ${operation.method} ${operation.path}`;
}

export default function OpenApiResourceMappingsSection({ integrationId, connectionId, integrationEnabled, busy, operations, mutate }: Props) {
  const [mappings, setMappings] = useState<IntegrationOpenApiResourceMapping[]>([]);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<OpenApiResourceMappingDraft>(EMPTY_DRAFT);
  const [preview, setPreview] = useState<IntegrationMappingPreview | null>(null);

  const refresh = async () => {
    const response = await integrationManagementApi.openApiResourceMappings(integrationId);
    setMappings(response.items);
  };
  useEffect(() => {
    let active = true;
    void integrationManagementApi.openApiResourceMappings(integrationId).then((response) => {
      if (active) setMappings(response.items);
    }).catch(() => { if (active) setMappings([]); });
    return () => { active = false; };
  }, [integrationId]);

  const set = (key: keyof OpenApiResourceMappingDraft, value: string) => setDraft((current) => ({ ...current, [key]: value }));
  const reset = () => { setFormOpen(false); setEditingId(null); setDraft(EMPTY_DRAFT); setPreview(null); };
  const edit = (mapping: IntegrationOpenApiResourceMapping) => {
    setEditingId(mapping.mapping_id);
    setDraft({
      resource_type: mapping.resource_type, label: mapping.label, plural_label: mapping.plural_label,
      list_operation_id: mapping.list_operation_id, get_operation_id: mapping.get_operation_id ?? "",
      create_operation_id: mapping.create_operation_id ?? "", update_operation_id: mapping.update_operation_id ?? "",
      delete_operation_id: mapping.delete_operation_id ?? "", get_external_id_target: mapping.get_external_id_target ?? "",
      update_external_id_target: mapping.update_external_id_target ?? "", delete_external_id_target: mapping.delete_external_id_target ?? "",
      items_path: mapping.items_path, id_path: mapping.id_path, name_path: mapping.name_path,
      parent_id_path: mapping.parent_id_path ?? "", url_path: mapping.url_path ?? "", status_path: mapping.status_path ?? "",
    });
    setPreview(null); setFormOpen(true);
  };

  const readOperations = operations.filter((operation) => operation.approved && !operation.destructive && ["GET", "HEAD"].includes(operation.method.toUpperCase()));
  const approvedOperations = operations.filter((operation) => operation.approved);
  async function previewMapping() {
    const result = await mutate(
      "openapi-mapping-preview",
      () => integrationManagementApi.previewOpenApiResourceMapping(integrationId, connectionId, payload(draft, editingId), true),
      "Live mapping preview completed. Review the projection before saving.",
    );
    if (result && "projected_resources" in result) setPreview(result as IntegrationMappingPreview);
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await mutate(
      "openapi-mapping",
      () => editingId
        ? integrationManagementApi.updateOpenApiResourceMapping(integrationId, editingId, payload(draft, editingId))
        : integrationManagementApi.createOpenApiResourceMapping(integrationId, payload(draft)),
      editingId ? "Resource mapping updated. Enable the integration, then Discover on a connection." : "Resource mapping saved. Enable the integration, then Discover on a connection.",
    );
    reset(); await refresh();
  }
  async function remove(mapping: IntegrationOpenApiResourceMapping) {
    if (!(await confirmAction({
      title: `Remove the ${mapping.plural_label} mapping?`,
      message: "The mapping and its related resource actions will be deleted. Detach active bindings first; the remote resources remain.",
      confirmLabel: "Remove mapping", tone: "danger",
    }))) return;
    await mutate(`openapi-mapping-delete:${mapping.mapping_id}`, () => integrationManagementApi.deleteOpenApiResourceMapping(integrationId, mapping.mapping_id), "Resource mapping deleted.");
    await refresh();
  }

  const suggestion = (key: keyof IntegrationMappingPreview["suggestions"], field: keyof OpenApiResourceMappingDraft) => preview ? <div className="mapping-suggestions"><strong>Suggested {field.replace(/_/g, " ")}</strong>{preview.suggestions[key].slice(0, 4).map((value) => <Button variant="secondary" type="button" key={value.path} title={value.reason} onClick={() => set(field, value.path)}>{value.path} ({value.score})</Button>)}</div> : null;

  return <section className="integration-subsection" aria-label="OpenAPI resource mappings">
    <header className="panel-header"><div><h4>Resource mappings</h4><p className="muted-text">Describe resources with approved operation IDs. Live preview invokes only the approved read operation and writes no inventory.</p></div><Button variant="secondary" type="button" disabled={busy} onClick={() => { if (formOpen) reset(); else setFormOpen(true); }}>{formOpen ? "Close" : "Add mapping"}</Button></header>
    {integrationEnabled ? <p className="muted-text">Disable this integration before changing mappings.</p> : null}
    {formOpen ? <form className="form-grid" onSubmit={(event) => void save(event)}>
      <label htmlFor="mapping-resource-type">Resource type<input id="mapping-resource-type" name="resource-type" autoComplete="off" required pattern="[a-z][a-z0-9_]*" value={draft.resource_type} onChange={(event) => set("resource_type", event.target.value)} /></label>
      <label htmlFor="mapping-label">Label<input id="mapping-label" name="resource-label" autoComplete="off" required value={draft.label} onChange={(event) => set("label", event.target.value)} /></label>
      <label htmlFor="mapping-plural-label">Plural label<input id="mapping-plural-label" name="resource-plural-label" autoComplete="off" required value={draft.plural_label} onChange={(event) => set("plural_label", event.target.value)} /></label>
      <label htmlFor="mapping-list-operation">List operation<select id="mapping-list-operation" name="list-operation" required value={draft.list_operation_id} onChange={(event) => set("list_operation_id", event.target.value)}><option value="">Choose an approved read operation</option>{readOperations.map((operation) => <option key={operation.operation_id} value={operation.operation_id}>{operationLabel(operation)}</option>)}</select></label>
      <label htmlFor="mapping-get-operation">Get operation (optional)<select id="mapping-get-operation" name="get-operation" value={draft.get_operation_id} onChange={(event) => set("get_operation_id", event.target.value)}><option value="">No get operation</option>{readOperations.map((operation) => <option key={operation.operation_id} value={operation.operation_id}>{operationLabel(operation)}</option>)}</select></label>
      <label htmlFor="mapping-create-operation">Create operation (optional)<select id="mapping-create-operation" name="create-operation" value={draft.create_operation_id} onChange={(event) => set("create_operation_id", event.target.value)}><option value="">No create operation</option>{approvedOperations.map((operation) => <option key={operation.operation_id} value={operation.operation_id}>{operationLabel(operation)}</option>)}</select></label>
      <label htmlFor="mapping-update-operation">Update operation (optional)<select id="mapping-update-operation" name="update-operation" value={draft.update_operation_id} onChange={(event) => set("update_operation_id", event.target.value)}><option value="">No update operation</option>{approvedOperations.map((operation) => <option key={operation.operation_id} value={operation.operation_id}>{operationLabel(operation)}</option>)}</select></label>
      <label htmlFor="mapping-delete-operation">Delete operation (optional)<select id="mapping-delete-operation" name="delete-operation" value={draft.delete_operation_id} onChange={(event) => set("delete_operation_id", event.target.value)}><option value="">No delete operation</option>{approvedOperations.map((operation) => <option key={operation.operation_id} value={operation.operation_id}>{operationLabel(operation)}</option>)}</select></label>
      <label htmlFor="mapping-items-path">Items path<input id="mapping-items-path" name="items-path" autoComplete="off" required value={draft.items_path} onChange={(event) => set("items_path", event.target.value)} placeholder="data.items" /></label>
      <label htmlFor="mapping-id-path">ID path<input id="mapping-id-path" name="id-path" autoComplete="off" required value={draft.id_path} onChange={(event) => set("id_path", event.target.value)} placeholder="id" /></label>
      <label htmlFor="mapping-name-path">Name path<input id="mapping-name-path" name="name-path" autoComplete="off" required value={draft.name_path} onChange={(event) => set("name_path", event.target.value)} placeholder="name" /></label>
      <details className="settings-advanced form-grid__wide"><summary><strong>Advanced response paths and mutation targets</strong><span>Only needed when the API uses nested or non-standard fields</span></summary><div className="form-grid">
        <label htmlFor="mapping-parent-path">Parent ID path<input id="mapping-parent-path" name="parent-id-path" autoComplete="off" value={draft.parent_id_path} onChange={(event) => set("parent_id_path", event.target.value)} /></label>
        <label htmlFor="mapping-url-path">URL path<input id="mapping-url-path" name="url-path" autoComplete="off" value={draft.url_path} onChange={(event) => set("url_path", event.target.value)} /></label>
        <label htmlFor="mapping-status-path">Status path<input id="mapping-status-path" name="status-path" autoComplete="off" value={draft.status_path} onChange={(event) => set("status_path", event.target.value)} /></label>
        <label htmlFor="mapping-get-target">Get external ID target<input id="mapping-get-target" name="get-external-id-target" autoComplete="off" value={draft.get_external_id_target} onChange={(event) => set("get_external_id_target", event.target.value)} /></label>
        <label htmlFor="mapping-update-target">Update external ID target<input id="mapping-update-target" name="update-external-id-target" autoComplete="off" value={draft.update_external_id_target} onChange={(event) => set("update_external_id_target", event.target.value)} /></label>
        <label htmlFor="mapping-delete-target">Delete external ID target<input id="mapping-delete-target" name="delete-external-id-target" autoComplete="off" value={draft.delete_external_id_target} onChange={(event) => set("delete_external_id_target", event.target.value)} /></label>
      </div></details>
      {preview ? <div className="callout" role="status"><strong>Live mapping preview</strong><p>{preview.projected_resources.length} resource projection{preview.projected_resources.length === 1 ? "" : "s"} produced. No inventory or health state was changed.</p>{preview.path_errors.map((error) => <p className="warning-message" key={error}>{error}</p>)}<pre className="json-preview">{JSON.stringify(preview.projected_resources, null, 2)}</pre><details className="settings-advanced"><summary><strong>Raw sample</strong><span>{preview.raw_sample_truncated ? `Truncated at ${preview.raw_sample_serialized_bytes.toLocaleString()} serialized bytes` : `${preview.raw_sample_serialized_bytes.toLocaleString()} serialized bytes`}</span></summary><pre className="json-preview">{JSON.stringify(preview.raw_sample, null, 2)}</pre></details>{suggestion("items_path", "items_path")}{suggestion("id_path", "id_path")}{suggestion("name_path", "name_path")}{suggestion("url_path", "url_path")}{suggestion("status_path", "status_path")}</div> : null}
      <div className="button-row"><Button variant="secondary" type="button" disabled={busy || !connectionId} onClick={() => void previewMapping()}>Preview live mapping</Button><Button variant="primary" type="submit" disabled={busy || integrationEnabled}>{editingId ? "Save changes" : "Save mapping"}</Button><Button variant="secondary" type="button" onClick={reset}>Cancel</Button></div>
    </form> : null}
    {mappings.length ? <div className="table-wrap integration-table-wrap"><table className="responsive-record-table integration-record-table"><thead><tr><th scope="col">Resource</th><th scope="col">Read operations</th><th scope="col">Write operations</th><th scope="col">Actions</th></tr></thead><tbody>{mappings.map((mapping) => <tr key={mapping.mapping_id}><td data-label="Resource"><strong>{mapping.plural_label}</strong><small className="muted-text mono-text">{mapping.resource_type}</small></td><td data-label="Read operations" className="mono-text">{mapping.list_operation_id}{mapping.get_operation_id ? ` · ${mapping.get_operation_id}` : ""}</td><td data-label="Write operations" className="mono-text">{[mapping.create_operation_id, mapping.update_operation_id, mapping.delete_operation_id].filter(Boolean).join(" · ") || "None declared"}</td><td data-label="Actions"><div className="button-row"><Button variant="secondary" type="button" disabled={busy || integrationEnabled} onClick={() => edit(mapping)}>Edit</Button><Button variant="danger" type="button"  disabled={busy || integrationEnabled} onClick={() => void remove(mapping)}>Delete</Button></div></td></tr>)}</tbody></table></div> : <p className="muted-text">No resource mappings yet. Add one after approving the operations it uses.</p>}
  </section>;
}
