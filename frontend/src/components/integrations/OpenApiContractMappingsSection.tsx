import { Button } from "../ui";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationMcpContractTarget, IntegrationOpenApiContractMapping, IntegrationOpenApiOperation } from "../../types";
import type { Mutate } from "./sectionTypes";
import { confirmAction } from "../../context/ConfirmContext";

export type OpenApiContractMappingDraft = {
  operation_id: string;
  target_contract: string;
  target_operation: string;
  argument_mapping: string;
  output_mapping: string;
  write_enabled: boolean;
};

const EMPTY_DRAFT: OpenApiContractMappingDraft = {
  operation_id: "", target_contract: "", target_operation: "", argument_mapping: "{}", output_mapping: "{}", write_enabled: false,
};
type Props = { integrationId: string; connectionId: string; integrationEnabled: boolean; connectionSelected: boolean; busy: boolean; operations: IntegrationOpenApiOperation[]; mutate: Mutate };

function operationLabel(operation: IntegrationOpenApiOperation) { return `${operation.operation_id} · ${operation.method} ${operation.path}`; }
function targetKey(target: IntegrationMcpContractTarget) { return `${target.contract}:${target.operation}`; }
function policyText(operation: IntegrationOpenApiOperation | undefined, target: IntegrationMcpContractTarget | undefined) {
  if (!operation && !target) return "Choose an approved operation and a schema-backed Lynewright contract to review policy.";
  return `Contract risk: ${target?.risk_class ?? "unknown"} · remote operation: ${operation ? `${operation.method} ${operation.path}` : "not selected"} · remote destructive: ${operation?.destructive ? "yes" : "no"}.`;
}

export default function OpenApiContractMappingsSection({ integrationId, connectionId, integrationEnabled, connectionSelected, busy, operations, mutate }: Props) {
  const [mappings, setMappings] = useState<IntegrationOpenApiContractMapping[]>([]);
  const [targets, setTargets] = useState<IntegrationMcpContractTarget[]>([]);
  const [formOpen, setFormOpen] = useState(false);
  const [draft, setDraft] = useState<OpenApiContractMappingDraft>(EMPTY_DRAFT);
  const refresh = async () => { const response = await integrationManagementApi.openApiContractMappings(integrationId, connectionId); setMappings(response.items); };
  useEffect(() => { let active = true; void Promise.all([integrationManagementApi.openApiContractMappings(integrationId, connectionId), integrationManagementApi.mcpContractTargets(integrationId)]).then(([mappingResponse, targetResponse]) => { if (active) { setMappings(mappingResponse.items); setTargets(targetResponse.items); } }).catch(() => { if (active) { setMappings([]); setTargets([]); } }); return () => { active = false; }; }, [connectionId, integrationId]);
  const approvedOperations = operations.filter((operation) => operation.approved);
  const selectedOperation = approvedOperations.find((operation) => operation.operation_id === draft.operation_id);
  const selectedTarget = targets.find((target) => target.contract === draft.target_contract && target.operation === draft.target_operation);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let argument_mapping: Record<string, string | number | boolean | null>; let output_mapping: Record<string, string | number | boolean | null>;
    try { argument_mapping = JSON.parse(draft.argument_mapping); output_mapping = JSON.parse(draft.output_mapping); if (!argument_mapping || Array.isArray(argument_mapping) || !output_mapping || Array.isArray(output_mapping)) throw new Error("Mappings must be JSON objects."); } catch { return; }
    const result = await mutate("openapi-contract-mapping", () => integrationManagementApi.createOpenApiContractMapping(integrationId, connectionId, { operation_id: draft.operation_id, target_contract: draft.target_contract, target_operation: draft.target_operation, argument_mapping, output_mapping, enabled: true, write_enabled: draft.write_enabled }), "OpenAPI contract mapping saved. It does not grant a capability.");
    if (result) { setFormOpen(false); setDraft(EMPTY_DRAFT); await refresh(); }
  }
  async function disable(mapping: IntegrationOpenApiContractMapping) { const result = await mutate(`openapi-contract-disable:${mapping.mapping_id}`, () => integrationManagementApi.updateOpenApiContractMapping(integrationId, connectionId, mapping.mapping_id, { ...mapping, enabled: false }), "OpenAPI contract mapping disabled."); if (result) await refresh(); }
  async function test(mapping: IntegrationOpenApiContractMapping) { if (mapping.write_enabled) return; await mutate(`openapi-contract-test:${mapping.mapping_id}`, () => integrationManagementApi.executeOpenApiContractMapping(integrationId, connectionId, mapping.mapping_id, {}), "OpenAPI read mapping test completed."); }
  async function remove(mapping: IntegrationOpenApiContractMapping) {
    if (!(await confirmAction({
      title: `Remove the ${mapping.operation_id} mapping?`,
      message: "The operation mapping and its approvals will be deleted. The remote service is unchanged.",
      confirmLabel: "Remove mapping", tone: "danger",
    }))) return;
    await mutate(`openapi-contract-delete:${mapping.mapping_id}`, () => integrationManagementApi.deleteOpenApiContractMapping(integrationId, connectionId, mapping.mapping_id), "OpenAPI contract mapping deleted.");
    await refresh();
  }

  return <section className="integration-subsection" aria-label="OpenAPI contract mappings">
    <header className="panel-header"><div><h4>OpenAPI contract mappings</h4><p className="muted-text">An approved external operation can adapt to an existing Lynewright contract; this never grants a capability.</p></div><Button variant="secondary" type="button" disabled={busy || !connectionSelected || integrationEnabled} onClick={() => setFormOpen((open) => !open)}>{formOpen ? "Close" : "Add mapping"}</Button></header>
    {integrationEnabled ? <p className="muted-text">Disable this integration before changing mappings.</p> : null}
    {formOpen ? <form className="form-grid" onSubmit={(event) => void save(event)}>
      <label htmlFor="openapi-contract-operation">Approved operation<select id="openapi-contract-operation" required value={draft.operation_id} onChange={(event) => setDraft({ ...draft, operation_id: event.target.value })}><option value="">Choose an approved operation</option>{approvedOperations.map((operation) => <option key={operation.operation_id} value={operation.operation_id}>{operationLabel(operation)}</option>)}</select></label>
      <label htmlFor="openapi-contract-target">Target contract<select id="openapi-contract-target" required value={`${draft.target_contract}:${draft.target_operation}`} onChange={(event) => { const target = targets.find((item) => targetKey(item) === event.target.value); setDraft({ ...draft, target_contract: target?.contract ?? "", target_operation: target?.operation ?? "" }); }}><option value=":">Choose a schema-backed contract operation</option>{targets.map((target) => <option key={targetKey(target)} value={targetKey(target)}>{target.contract} · {target.operation} · {target.risk_class ?? "risk unknown"}</option>)}</select></label>
      <div className="callout form-grid__wide" role="status" aria-live="polite"><strong>Execution policy</strong><p>{policyText(selectedOperation, selectedTarget)}</p><p className="muted-text">Mapped writes require an idempotency key at execution time. Unknown write outcomes are never retried.</p></div>
      <div className="form-grid__wide"><label htmlFor="openapi-contract-arguments"><span>Argument mapping (JSON object)</span><textarea id="openapi-contract-arguments" required rows={3} aria-describedby="openapi-contract-arguments-hint" value={draft.argument_mapping} onChange={(event) => setDraft({ ...draft, argument_mapping: event.target.value })} /></label><small id="openapi-contract-arguments-hint" className="muted-text">Contract input name to bounded API path or scalar.</small></div>
      <div className="form-grid__wide"><label htmlFor="openapi-contract-output"><span>Output mapping (JSON object)</span><textarea id="openapi-contract-output" required rows={3} aria-describedby="openapi-contract-output-hint" value={draft.output_mapping} onChange={(event) => setDraft({ ...draft, output_mapping: event.target.value })} /></label><small id="openapi-contract-output-hint" className="muted-text">Contract output field to bounded API response path or scalar.</small></div>
      <label className="checkbox-row" htmlFor="openapi-contract-write"><input id="openapi-contract-write" autoComplete="off" type="checkbox" checked={draft.write_enabled} onChange={(event) => setDraft({ ...draft, write_enabled: event.target.checked })} /> Enable explicit WRITE opt-in for this mapping</label>
      <div className="button-row"><Button variant="primary" type="submit" disabled={busy || integrationEnabled}>Save mapping</Button><Button variant="secondary" type="button" onClick={() => setFormOpen(false)}>Cancel</Button></div>
    </form> : null}
    {mappings.length ? <div className="table-wrap"><table className="responsive-record-table"><thead><tr><th scope="col">Operation</th><th scope="col">Target</th><th scope="col">Policy</th><th scope="col">Actions</th></tr></thead><tbody>{mappings.map((mapping) => <tr key={mapping.mapping_id}><td data-label="Operation" className="mono-text">{mapping.operation_id}</td><td data-label="Target" className="mono-text">{mapping.target_contract} · {mapping.target_operation}</td><td data-label="Policy"><strong>{mapping.write_enabled ? "WRITE opt-in" : "READ"}</strong><small className="muted-text">Risk: {mapping.risk_class ?? "unknown"} · destructive: {mapping.destructive ? "yes" : "no"}</small></td><td data-label="Actions"><div className="button-row">{!mapping.write_enabled ? <Button variant="secondary" type="button" disabled={busy || integrationEnabled} onClick={() => void test(mapping)}>Test read</Button> : <span className="muted-text">Execution requires an idempotency key</span>}<Button variant="secondary" type="button" disabled={busy || integrationEnabled} onClick={() => void disable(mapping)}>Disable</Button><Button variant="danger" type="button"  disabled={busy || integrationEnabled} onClick={() => void remove(mapping)}>Delete</Button></div></td></tr>)}</tbody></table></div> : <p className="muted-text">No OpenAPI contract mappings for this connection.</p>}
  </section>;
}
