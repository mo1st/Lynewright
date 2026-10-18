import { Button } from "../ui";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationMcpContractMapping, IntegrationMcpContractTarget, IntegrationMcpMappingValue, IntegrationMcpTool } from "../../types";
import type { Mutate } from "./sectionTypes";
import { confirmAction } from "../../context/ConfirmContext";
import McpToolProbe from "./McpToolProbe";
import McpToolSchema from "./McpToolSchema";
import { ClampedText } from "../ui";

type Props = {
  integrationId: string;
  connectionId: string;
  discoveredTools: IntegrationMcpTool[];
  enabled: boolean;
  connectionEnabled: boolean;
  busy: boolean;
  mutate: Mutate;
  reportError: (message: string) => void;
};

type Draft = {
  mapping_id?: string;
  tool_name: string;
  target_contract: string;
  target_operation: string;
  argument_mapping: string;
  output_mapping: string;
  enabled: boolean;
  write_enabled: boolean;
};

const EMPTY_DRAFT: Draft = {
  tool_name: "",
  target_contract: "",
  target_operation: "",
  argument_mapping: "{}",
  output_mapping: "{}",
  enabled: true,
  write_enabled: false,
};

function toolWrites(tool: IntegrationMcpTool | undefined) {
  return tool?.read_only === false || tool?.destructive === true;
}

function safetyLabel(tool: IntegrationMcpTool | undefined) {
  if (!tool || tool.safety_known !== true) return "remote safety unknown";
  if (tool.destructive === true) return "WRITE · destructive";
  if (tool.read_only === false) return "WRITE · non-destructive";
  return "READ · non-destructive";
}

export default function McpContractMappingsSection({ integrationId, connectionId, discoveredTools, enabled, connectionEnabled, busy, mutate, reportError }: Props) {
  const [mappings, setMappings] = useState<IntegrationMcpContractMapping[]>([]);
  const [targets, setTargets] = useState<IntegrationMcpContractTarget[]>([]);
  const [formOpen, setFormOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [loadFailed, setLoadFailed] = useState(false);

  const refresh = async () => {
    const [mappingResponse, targetResponse] = await Promise.all([
      integrationManagementApi.mcpContractMappings(integrationId, connectionId),
      integrationManagementApi.mcpContractTargets(integrationId),
    ]);
    setMappings(mappingResponse.items);
    setTargets(targetResponse.items);
    setLoadFailed(false);
  };

  useEffect(() => {
    if (!connectionId) {
      setMappings([]);
      setTargets([]);
      return;
    }
    let active = true;
    void Promise.all([
      integrationManagementApi.mcpContractMappings(integrationId, connectionId),
      integrationManagementApi.mcpContractTargets(integrationId),
    ]).then(([mappingResponse, targetResponse]) => {
      if (active) {
        setMappings(mappingResponse.items);
        setTargets(targetResponse.items);
        setLoadFailed(false);
      }
    }).catch(() => {
      if (active) {
        setLoadFailed(true);
      }
    });
    return () => { active = false; };
  }, [connectionId, integrationId]);

  const approvedTools = discoveredTools.filter((tool) => tool.approved);
  const selectedTool = approvedTools.find((tool) => tool.name === draft.tool_name);
  const selectedTarget = targets.find((target) => target.contract === draft.target_contract && target.operation === draft.target_operation);

  async function approve(tool: IntegrationMcpTool) {
    if (toolWrites(tool)) {
      const consequence = tool.destructive === true ? " It may permanently delete remote data." : " It may change remote data.";
      if (!(await confirmAction({
        title: `Approve WRITE tool ${tool.name}?`,
        message: `${consequence} Approval alone does not grant a Lynewright capability.`,
        confirmLabel: "Approve tool", tone: "danger",
      }))) return;
    }
    const names = discoveredTools.filter((item) => item.approved || item.name === tool.name).map((item) => item.name);
    await mutate(`tool:${tool.name}`, () => integrationManagementApi.approveMcpTools(integrationId, connectionId, names), `${tool.name} approved for this connection.`);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let argument_mapping: Record<string, IntegrationMcpMappingValue>;
    let output_mapping: Record<string, IntegrationMcpMappingValue>;
    try {
      argument_mapping = JSON.parse(draft.argument_mapping);
      output_mapping = JSON.parse(draft.output_mapping);
      if (!argument_mapping || Array.isArray(argument_mapping) || !output_mapping || Array.isArray(output_mapping)) throw new Error("Mappings must be JSON objects.");
    } catch (value) {
      reportError(value instanceof Error ? value.message : "Argument and output mappings must be JSON objects.");
      return;
    }
    const payload = {
      tool_name: draft.tool_name,
      target_contract: draft.target_contract,
      target_operation: draft.target_operation,
      argument_mapping,
      output_mapping,
      enabled: draft.enabled,
      write_enabled: draft.write_enabled,
    };
    const editing = draft.mapping_id;
    const result = await mutate(
      "mcp-contract-mapping",
      () => editing
        ? integrationManagementApi.updateMcpContractMapping(integrationId, connectionId, editing, payload)
        : integrationManagementApi.createMcpContractMapping(integrationId, connectionId, payload),
      editing ? "MCP contract mapping updated. It does not grant a capability." : "MCP contract mapping saved. It does not grant a capability.",
    );
    if (result) {
      setFormOpen(false);
      setDraft(EMPTY_DRAFT);
      await refresh();
    }
  }

  function edit(mapping: IntegrationMcpContractMapping) {
    setDraft({
      mapping_id: mapping.mapping_id,
      tool_name: mapping.tool_name,
      target_contract: mapping.target_contract,
      target_operation: mapping.target_operation,
      argument_mapping: JSON.stringify(mapping.argument_mapping, null, 2),
      output_mapping: JSON.stringify(mapping.output_mapping, null, 2),
      enabled: mapping.enabled,
      write_enabled: mapping.write_enabled === true,
    });
    setFormOpen(true);
  }

  async function remove(mapping: IntegrationMcpContractMapping) {
    if (!(await confirmAction({
      title: `Remove the ${mapping.tool_name} mapping?`,
      message: "Only Lynewright's mapping will be deleted. This does not change remote data.",
      confirmLabel: "Remove mapping", tone: "danger",
    }))) return;
    await mutate(
      `mcp-contract-mapping-delete:${mapping.mapping_id}`,
      () => integrationManagementApi.deleteMcpContractMapping(integrationId, connectionId, mapping.mapping_id),
      "MCP contract mapping removed.",
    );
    await refresh();
  }

  async function test(mapping: IntegrationMcpContractMapping) {
    if (mapping.write_enabled) return;
    await mutate(
      `mcp-contract-test:${mapping.mapping_id}`,
      () => integrationManagementApi.executeMcpContractMapping(integrationId, connectionId, mapping.mapping_id, {}),
      "MCP read mapping test completed.",
    );
  }

  return <section className="integration-subsection" aria-label="MCP contract mappings">
    <header className="panel-header">
      <div><h4>MCP contract mappings</h4><p className="muted-text">Adapt an approved external tool to an existing Lynewright contract. A mapping never grants a capability.</p></div>
      <Button variant="secondary" type="button" disabled={busy || !connectionId || enabled || !connectionEnabled || loadFailed} onClick={() => { setFormOpen((open) => !open); if (formOpen) setDraft(EMPTY_DRAFT); }}>{formOpen ? "Close" : "Add mapping"}</Button>
    </header>
    {enabled ? <p className="muted-text">Disable the integration before changing mappings. Re-enable it to test or execute them.</p> : null}
    {loadFailed ? <div className="callout callout--warning" role="alert"><strong>Contract mappings could not be loaded.</strong><p>The last loaded values are preserved; no empty-state conclusion has been drawn.</p><Button variant="secondary" type="button" disabled={busy} onClick={() => void refresh().catch(() => setLoadFailed(true))}>Retry</Button></div> : null}
    {formOpen ? <form className="form-grid" onSubmit={(event) => void save(event)}>
      <p className="form-grid__wide muted-text">{draft.mapping_id ? `Editing mapping ${draft.mapping_id}.` : "New mapping. Changes do not grant capabilities."}</p>
      <label htmlFor="mcp-mapping-tool">Approved tool<select id="mcp-mapping-tool" required value={draft.tool_name} onChange={(event) => setDraft({ ...draft, tool_name: event.target.value })}><option value="">Choose an approved tool</option>{approvedTools.map((tool) => <option key={tool.name} value={tool.name}>{tool.name} · {safetyLabel(tool)}</option>)}</select></label>
      <label htmlFor="mcp-mapping-target">Target contract<select id="mcp-mapping-target" required value={`${draft.target_contract}:${draft.target_operation}`} onChange={(event) => { const target = targets.find((item) => `${item.contract}:${item.operation}` === event.target.value); setDraft({ ...draft, target_contract: target?.contract ?? "", target_operation: target?.operation ?? "" }); }}><option value=":">Choose a schema-backed contract operation</option>{targets.map((target) => <option key={`${target.contract}:${target.operation}`} value={`${target.contract}:${target.operation}`}>{target.contract} · {target.operation} · {target.risk_class ?? "risk unknown"}</option>)}</select></label>
      <div className={`callout form-grid__wide${toolWrites(selectedTool) && !draft.write_enabled ? " callout--warning" : ""}`} role="status" aria-live="polite"><strong>Execution policy</strong><p>Contract risk: {selectedTarget?.risk_class ?? "unknown"} · tool policy: {safetyLabel(selectedTool)} · destructive: {selectedTool?.destructive === true ? "yes" : "no"}.</p><p className="muted-text">Mapped writes require an idempotency key. Destructive writes also require confirmation. Unknown outcomes are never retried.</p></div>
      <div className="form-grid__wide"><label htmlFor="mcp-mapping-arguments"><span>Argument mapping (JSON object)</span><textarea id="mcp-mapping-arguments" required rows={3} aria-describedby="mcp-mapping-arguments-hint" value={draft.argument_mapping} onChange={(event) => setDraft({ ...draft, argument_mapping: event.target.value })} /></label><small id="mcp-mapping-arguments-hint" className="muted-text">Remote argument to contract input path. Use {`{"$literal":"value"}`} for an explicit string constant.</small></div>
      <div className="form-grid__wide"><label htmlFor="mcp-mapping-output"><span>Output mapping (JSON object)</span><textarea id="mcp-mapping-output" required rows={3} aria-describedby="mcp-mapping-output-hint" value={draft.output_mapping} onChange={(event) => setDraft({ ...draft, output_mapping: event.target.value })} /></label><small id="mcp-mapping-output-hint" className="muted-text">Contract output field to bounded MCP response path or scalar.</small></div>
      <label className="checkbox-row" htmlFor="mcp-mapping-write"><input id="mcp-mapping-write" autoComplete="off" type="checkbox" checked={draft.write_enabled} onChange={(event) => setDraft({ ...draft, write_enabled: event.target.checked })} /> Enable explicit WRITE opt-in for this mapping</label>
      <div className="button-row"><Button variant="primary" type="submit" disabled={busy || enabled}>{draft.mapping_id ? "Save changes" : "Save mapping"}</Button><Button variant="secondary" type="button" onClick={() => { setFormOpen(false); setDraft(EMPTY_DRAFT); }}>Cancel</Button></div>
    </form> : null}
    {discoveredTools.length ? <div className="table-wrap"><table className="responsive-record-table"><thead><tr><th scope="col">Tool</th><th scope="col">Description</th><th scope="col">Policy</th><th scope="col">Approval</th><th scope="col">Input</th><th scope="col">Read-only proof</th></tr></thead><tbody>{discoveredTools.map((tool) => <tr key={tool.name}><td data-label="Tool" className="mono-text">{tool.name}</td><td data-label="Description"><ClampedText text={tool.description || "—"} label="tool description" /></td><td data-label="Policy"><strong>{safetyLabel(tool)}</strong></td><td data-label="Approval">{tool.approved ? "Approved" : <Button variant="secondary" type="button" disabled={busy} onClick={() => void approve(tool)}>{toolWrites(tool) ? "Approve WRITE" : "Approve"}</Button>}</td><td data-label="Input"><McpToolSchema integrationId={integrationId} connectionId={connectionId} toolName={tool.name} busy={busy} mutate={mutate} /></td><td data-label="Read-only proof">{tool.approved ? <McpToolProbe integrationId={integrationId} connectionId={connectionId} tool={tool} busy={busy} mutate={mutate} reportError={reportError} /> : <span className="muted-text">Approve first</span>}</td></tr>)}</tbody></table></div> : null}
    {mappings.length ? <div className="table-wrap"><table className="responsive-record-table"><thead><tr><th scope="col">Tool</th><th scope="col">Target</th><th scope="col">Policy</th><th scope="col">Actions</th></tr></thead><tbody>{mappings.map((mapping) => <tr key={mapping.mapping_id}><td data-label="Tool" className="mono-text">{mapping.tool_name}</td><td data-label="Target" className="mono-text">{mapping.target_contract} · {mapping.target_operation}</td><td data-label="Policy"><strong>{mapping.write_enabled ? "WRITE opt-in" : "READ"}</strong><small className="muted-text">Risk: {mapping.risk_class ?? "unknown"} · destructive: {mapping.destructive ? "yes" : "no"}</small></td><td data-label="Actions"><div className="button-row">{mapping.write_enabled ? <span className="muted-text">Execution requires an idempotency key{mapping.destructive ? " and confirmation" : ""}</span> : <Button variant="secondary" type="button" disabled={busy || !connectionEnabled || !enabled} onClick={() => void test(mapping)}>Test read</Button>}<Button variant="secondary" type="button" disabled={busy || enabled || !connectionEnabled} onClick={() => edit(mapping)}>Edit</Button><Button variant="danger" type="button" disabled={busy || enabled || !connectionEnabled} onClick={() => void remove(mapping)}>Remove</Button></div></td></tr>)}</tbody></table></div> : loadFailed ? null : <p className="muted-text">No contract mappings for this connection.</p>}
  </section>;
}
