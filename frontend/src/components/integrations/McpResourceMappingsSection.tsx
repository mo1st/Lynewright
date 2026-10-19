import { Button } from "../ui";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationMappingPreview, IntegrationMcpResourceMapping, IntegrationMcpTool } from "../../types";
import type { Mutate } from "./sectionTypes";
import { confirmAction } from "../../context/ConfirmContext";

type Scalar = string | number | boolean | null;
type Draft = {
  resource_type: string;
  label: string;
  plural_label: string;
  list_tool_name: string;
  list_arguments: string;
  get_tool_name: string;
  create_tool_name: string;
  update_tool_name: string;
  delete_tool_name: string;
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

const EMPTY_DRAFT: Draft = {
  resource_type: "", label: "", plural_label: "", list_tool_name: "", list_arguments: "{}",
  get_tool_name: "", create_tool_name: "", update_tool_name: "", delete_tool_name: "",
  get_external_id_target: "", update_external_id_target: "", delete_external_id_target: "",
  items_path: "", id_path: "", name_path: "", parent_id_path: "", url_path: "", status_path: "",
};

type Props = {
  integrationId: string;
  connectionId: string;
  integrationEnabled: boolean;
  discoveredTools: IntegrationMcpTool[];
  busy: boolean;
  mutate: Mutate;
  reportError: (message: string) => void;
};

function toolLabel(tool: IntegrationMcpTool) { return tool.description ? `${tool.name} · ${tool.description}` : tool.name; }

function draftFromMapping(mapping: IntegrationMcpResourceMapping): Draft {
  return {
    resource_type: mapping.resource_type,
    label: mapping.label,
    plural_label: mapping.plural_label,
    list_tool_name: mapping.list_tool_name,
    list_arguments: JSON.stringify(mapping.list_arguments, null, 2),
    get_tool_name: mapping.get_tool_name ?? "",
    create_tool_name: mapping.create_tool_name ?? "",
    update_tool_name: mapping.update_tool_name ?? "",
    delete_tool_name: mapping.delete_tool_name ?? "",
    get_external_id_target: mapping.get_external_id_target ?? "",
    update_external_id_target: mapping.update_external_id_target ?? "",
    delete_external_id_target: mapping.delete_external_id_target ?? "",
    items_path: mapping.items_path,
    id_path: mapping.id_path,
    name_path: mapping.name_path,
    parent_id_path: mapping.parent_id_path ?? "",
    url_path: mapping.url_path ?? "",
    status_path: mapping.status_path ?? "",
  };
}

function payloadFromDraft(draft: Draft, reportError: (message: string) => void, mappingId?: string | null) {
  let list_arguments: Record<string, Scalar>;
  try {
    const parsed: unknown = JSON.parse(draft.list_arguments);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("List arguments must be a JSON object.");
    const entries = Object.entries(parsed as Record<string, unknown>);
    if (entries.some(([, value]) => value !== null && !["string", "number", "boolean"].includes(typeof value))) {
      throw new Error("List arguments may contain scalar values only.");
    }
    list_arguments = Object.fromEntries(entries) as Record<string, Scalar>;
  } catch (value) {
    reportError(value instanceof Error ? value.message : "List arguments must be valid JSON.");
    return null;
  }
  return {
    ...(mappingId ? { mapping_id: mappingId } : {}),
    resource_type: draft.resource_type,
    label: draft.label,
    plural_label: draft.plural_label,
    list_tool_name: draft.list_tool_name,
    list_arguments,
    get_tool_name: draft.get_tool_name || null,
    create_tool_name: draft.create_tool_name || null,
    update_tool_name: draft.update_tool_name || null,
    delete_tool_name: draft.delete_tool_name || null,
    get_external_id_target: draft.get_external_id_target || null,
    update_external_id_target: draft.update_external_id_target || null,
    delete_external_id_target: draft.delete_external_id_target || null,
    items_path: draft.items_path,
    id_path: draft.id_path,
    name_path: draft.name_path,
    parent_id_path: draft.parent_id_path || null,
    url_path: draft.url_path || null,
    status_path: draft.status_path || null,
    display_fields: [],
    metadata_paths: {},
    enabled: true,
  };
}

function SuggestionButtons({ label, values, onApply }: { label: string; values: IntegrationMappingPreview["suggestions"][keyof IntegrationMappingPreview["suggestions"]]; onApply: (path: string) => void }) {
  if (!values.length) return null;
  return <div className="mapping-suggestions"><strong>{label}</strong>{values.slice(0, 4).map((value) => <Button variant="secondary" type="button" key={value.path} title={value.reason} onClick={() => onApply(value.path)}>{value.path} ({value.score})</Button>)}</div>;
}

export default function McpResourceMappingsSection({ integrationId, connectionId, integrationEnabled, discoveredTools, busy, mutate, reportError }: Props) {
  const [mappings, setMappings] = useState<IntegrationMcpResourceMapping[]>([]);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "failed">("loading");
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [preview, setPreview] = useState<IntegrationMappingPreview | null>(null);
  const approvedTools = discoveredTools.filter((tool) => tool.approved);

  async function loadMappings() {
    if (!connectionId) { setMappings([]); setLoadState("ready"); return; }
    setLoadState("loading");
    try {
      const response = await integrationManagementApi.mcpResourceMappings(integrationId, connectionId);
      setMappings(response.items);
      setLoadState("ready");
    } catch {
      setLoadState("failed");
    }
  }

  useEffect(() => { let active = true; if (!connectionId) { setMappings([]); setLoadState("ready"); return () => { active = false; }; } setLoadState("loading"); void integrationManagementApi.mcpResourceMappings(integrationId, connectionId).then((response) => { if (active) { setMappings(response.items); setLoadState("ready"); } }).catch(() => { if (active) setLoadState("failed"); }); return () => { active = false; }; }, [connectionId, integrationId]);

  const set = (key: keyof Draft, value: string) => setDraft((current) => ({ ...current, [key]: value }));
  const reset = () => { setFormOpen(false); setEditingId(null); setDraft(EMPTY_DRAFT); setPreview(null); };
  const edit = (mapping: IntegrationMcpResourceMapping) => { setEditingId(mapping.mapping_id); setDraft(draftFromMapping(mapping)); setPreview(null); setFormOpen(true); };

  async function previewMapping() {
    const payload = payloadFromDraft(draft, reportError, editingId);
    if (!payload) return;
    const result = await mutate("mcp-resource-mapping-preview", () => integrationManagementApi.previewMcpResourceMapping(integrationId, connectionId, payload, true), "Live mapping preview completed. Review the projection before saving.");
    if (result && "projected_resources" in result) setPreview(result as IntegrationMappingPreview);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const payload = payloadFromDraft(draft, reportError, editingId);
    if (!payload) return;
    const result = await mutate("mcp-resource-mapping", () => editingId ? integrationManagementApi.updateMcpResourceMapping(integrationId, connectionId, editingId, payload) : integrationManagementApi.createMcpResourceMapping(integrationId, connectionId, payload), editingId ? "MCP resource mapping updated." : "MCP resource mapping saved.");
    if (result) { reset(); await loadMappings(); }
  }

  async function remove(mapping: IntegrationMcpResourceMapping) {
    if (!(await confirmAction({
      title: `Remove the ${mapping.plural_label} mapping?`,
      message: "The mapping will be deleted. Detach active bindings first; this does not delete remote resources.",
      confirmLabel: "Remove mapping", tone: "danger",
    }))) return;
    await mutate(`mcp-resource-mapping-delete:${mapping.mapping_id}`, () => integrationManagementApi.deleteMcpResourceMapping(integrationId, connectionId, mapping.mapping_id), "MCP resource mapping deleted.");
    await loadMappings();
  }

  const suggestion = (key: keyof IntegrationMappingPreview["suggestions"], field: keyof Draft) => preview ? <SuggestionButtons label={`Suggested ${field.replaceAll("_", " ")}`} values={preview.suggestions[key]} onApply={(path) => set(field, path)} /> : null;

  return <section className="integration-subsection" aria-label="MCP resource mappings">
    <header className="panel-header"><div><h4>MCP resource mappings</h4><p className="muted-text">Map an approved MCP list tool into a bindable Lynewright resource type. Optional actions remain approval-gated at execution.</p></div><Button variant="secondary" type="button" disabled={busy || !connectionId} onClick={() => { if (formOpen) reset(); else setFormOpen(true); }}>{formOpen ? "Close" : "Add mapping"}</Button></header>
    {integrationEnabled ? <p className="muted-text">Disable this integration before changing mappings.</p> : null}
    {!approvedTools.length ? <p className="callout callout--warning">Approve the MCP tools that list or act on resources before adding a mapping.</p> : null}
    {formOpen ? <form className="form-grid" onSubmit={(event) => void save(event)}>
      <label htmlFor="mcp-resource-type">Resource type<input id="mcp-resource-type" autoComplete="off" required pattern="[a-z][a-z0-9_]*" value={draft.resource_type} onChange={(event) => set("resource_type", event.target.value)} /></label>
      <label htmlFor="mcp-resource-label">Label<input id="mcp-resource-label" autoComplete="off" required value={draft.label} onChange={(event) => set("label", event.target.value)} /></label>
      <label htmlFor="mcp-resource-plural-label">Plural label<input id="mcp-resource-plural-label" autoComplete="off" required value={draft.plural_label} onChange={(event) => set("plural_label", event.target.value)} /></label>
      <label htmlFor="mcp-resource-list-tool">Approved list tool<select id="mcp-resource-list-tool" required value={draft.list_tool_name} onChange={(event) => set("list_tool_name", event.target.value)}><option value="">Choose an approved tool</option>{approvedTools.map((tool) => <option key={tool.name} value={tool.name}>{toolLabel(tool)}</option>)}</select></label>
      <label className="form-grid__wide" htmlFor="mcp-resource-list-arguments">List arguments (bounded scalar JSON)<textarea id="mcp-resource-list-arguments" rows={3} required value={draft.list_arguments} onChange={(event) => set("list_arguments", event.target.value)} /></label>
      <label htmlFor="mcp-resource-items-path">Items path<input id="mcp-resource-items-path" autoComplete="off" required placeholder="data.items" value={draft.items_path} onChange={(event) => set("items_path", event.target.value)} /></label>
      <label htmlFor="mcp-resource-id-path">ID path<input id="mcp-resource-id-path" autoComplete="off" required placeholder="id" value={draft.id_path} onChange={(event) => set("id_path", event.target.value)} /></label>
      <label htmlFor="mcp-resource-name-path">Name path<input id="mcp-resource-name-path" autoComplete="off" required placeholder="name" value={draft.name_path} onChange={(event) => set("name_path", event.target.value)} /></label>
      <details className="settings-advanced form-grid__wide"><summary><strong>Optional actions and response paths</strong><span>Only approved tools can be selected; action identity paths are validated by Lynewright.</span></summary><div className="form-grid">
        <label htmlFor="mcp-resource-get-tool">Get tool<select id="mcp-resource-get-tool" value={draft.get_tool_name} onChange={(event) => set("get_tool_name", event.target.value)}><option value="">No get tool</option>{approvedTools.map((tool) => <option key={tool.name} value={tool.name}>{toolLabel(tool)}</option>)}</select></label>
        <label htmlFor="mcp-resource-get-target">Get ID target<input id="mcp-resource-get-target" autoComplete="off" value={draft.get_external_id_target} onChange={(event) => set("get_external_id_target", event.target.value)} placeholder="channel_id" /></label>
        <label htmlFor="mcp-resource-create-tool">Create tool<select id="mcp-resource-create-tool" value={draft.create_tool_name} onChange={(event) => set("create_tool_name", event.target.value)}><option value="">No create tool</option>{approvedTools.map((tool) => <option key={tool.name} value={tool.name}>{toolLabel(tool)}</option>)}</select></label>
        <label htmlFor="mcp-resource-update-tool">Update tool<select id="mcp-resource-update-tool" value={draft.update_tool_name} onChange={(event) => set("update_tool_name", event.target.value)}><option value="">No update tool</option>{approvedTools.map((tool) => <option key={tool.name} value={tool.name}>{toolLabel(tool)}</option>)}</select></label>
        <label htmlFor="mcp-resource-update-target">Update ID target<input id="mcp-resource-update-target" autoComplete="off" value={draft.update_external_id_target} onChange={(event) => set("update_external_id_target", event.target.value)} placeholder="channel_id" /></label>
        <label htmlFor="mcp-resource-delete-tool">Delete tool<select id="mcp-resource-delete-tool" value={draft.delete_tool_name} onChange={(event) => set("delete_tool_name", event.target.value)}><option value="">No delete tool</option>{approvedTools.map((tool) => <option key={tool.name} value={tool.name}>{toolLabel(tool)}</option>)}</select></label>
        <label htmlFor="mcp-resource-delete-target">Delete ID target<input id="mcp-resource-delete-target" autoComplete="off" value={draft.delete_external_id_target} onChange={(event) => set("delete_external_id_target", event.target.value)} placeholder="channel_id" /></label>
        <label htmlFor="mcp-resource-parent-path">Parent ID path<input id="mcp-resource-parent-path" autoComplete="off" value={draft.parent_id_path} onChange={(event) => set("parent_id_path", event.target.value)} /></label>
        <label htmlFor="mcp-resource-url-path">URL path<input id="mcp-resource-url-path" autoComplete="off" value={draft.url_path} onChange={(event) => set("url_path", event.target.value)} /></label>
        <label htmlFor="mcp-resource-status-path">Status path<input id="mcp-resource-status-path" autoComplete="off" value={draft.status_path} onChange={(event) => set("status_path", event.target.value)} /></label>
      </div></details>
      {preview ? <div className="callout" role="status"><strong>Live mapping preview</strong><p>{preview.projected_resources.length} resource projection{preview.projected_resources.length === 1 ? "" : "s"} produced. No inventory or health state was changed.</p>{preview.path_errors.map((error) => <p className="warning-message" key={error}>{error}</p>)}<pre className="json-preview">{JSON.stringify(preview.projected_resources, null, 2)}</pre><details className="settings-advanced"><summary><strong>Raw sample</strong><span>{preview.raw_sample_truncated ? `Truncated at ${preview.raw_sample_serialized_bytes.toLocaleString()} serialized bytes` : `${preview.raw_sample_serialized_bytes.toLocaleString()} serialized bytes`}</span></summary><pre className="json-preview">{JSON.stringify(preview.raw_sample, null, 2)}</pre></details>{suggestion("items_path", "items_path")}{suggestion("id_path", "id_path")}{suggestion("name_path", "name_path")}{suggestion("url_path", "url_path")}{suggestion("status_path", "status_path")}</div> : null}
      <div className="button-row"><Button variant="secondary" type="button" disabled={busy || !connectionId} onClick={() => void previewMapping()}>Preview live mapping</Button><Button variant="primary" type="submit" disabled={busy || integrationEnabled}>{editingId ? "Save changes" : "Save mapping"}</Button><Button variant="secondary" type="button" onClick={reset}>Cancel</Button></div>
    </form> : null}
    {loadState === "loading" ? <p className="muted-text" role="status">Loading MCP resource mappings…</p> : null}
    {loadState === "failed" ? <div className="callout callout--warning" role="alert"><strong>Could not load MCP resource mappings.</strong><p>Retry to distinguish a temporary dashboard error from an empty mapping list.</p><Button variant="secondary" type="button" disabled={busy} onClick={() => void loadMappings()}>Retry</Button></div> : null}
    {loadState === "ready" && mappings.length ? <div className="table-wrap integration-table-wrap"><table className="responsive-record-table integration-record-table"><thead><tr><th scope="col">Resource</th><th scope="col">List tool</th><th scope="col">Actions</th></tr></thead><tbody>{mappings.map((mapping) => <tr key={mapping.mapping_id}><td data-label="Resource"><strong>{mapping.plural_label}</strong><small className="muted-text mono-text">{mapping.resource_type}</small></td><td data-label="List tool" className="mono-text">{mapping.list_tool_name}</td><td data-label="Actions"><div className="button-row"><Button variant="secondary" type="button" disabled={busy || integrationEnabled} onClick={() => edit(mapping)}>Edit</Button><Button variant="danger" type="button"  disabled={busy || integrationEnabled} onClick={() => void remove(mapping)}>Delete</Button></div></td></tr>)}</tbody></table></div> : null}
    {loadState === "ready" && !mappings.length ? <p className="muted-text">No MCP resource mappings yet.</p> : null}
  </section>;
}
