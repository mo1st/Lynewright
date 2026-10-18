import { Button } from "../ui";
import { useState } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { ProductionOperation } from "../../api/productionLinesClient";
import type { Mutate } from "./sectionTypes";

type Props = { projectId: string; busy: boolean; mutate: Mutate; operations: ProductionOperation[] };

/** Operations the project's production-line endpoint currently authorizes. */
export default function GenericOperationsSection({ projectId, busy, mutate, operations }: Props) {
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  async function describe(item: Record<string, unknown>) {
    const operationId = String(item.operation_id ?? "");
    if (!operationId) return;
    try { setSelected(await integrationManagementApi.describeOperation(operationId, projectId)); setError(""); } catch (value) { setError(value instanceof Error ? value.message : "Could not describe operation."); }
  }
  async function testRead(item: Record<string, unknown>) {
    if (item.read_only !== true) return;
    const operationId = String(item.operation_id ?? "");
    if (!operationId) return;
    const prepared = await mutate(`operation:prepare:${operationId}`, () => integrationManagementApi.prepareOperation({ project_id: projectId, operation_id: operationId, arguments: { project_id: projectId } }), "Read operation prepared.");
    if (!prepared || typeof prepared !== "object") return;
    const preparedId = String((prepared as Record<string, unknown>).prepared_invocation_id ?? "");
    await mutate(`operation:invoke:${operationId}`, () => integrationManagementApi.invokeOperation({ project_id: projectId, operation_id: operationId, prepared_invocation_id: preparedId }), "Read operation invoked.");
  }
  const executionId = String(selected?.execution_id ?? "");
  async function inspectExecution() { if (executionId) setSelected(await integrationManagementApi.getExecution(executionId, projectId)); }
  async function cancelExecution() { if (executionId) await mutate(`operation:cancel:${executionId}`, () => integrationManagementApi.cancelExecution(executionId, projectId), "Execution cancellation requested."); }
  return <section className="integration-subsection" aria-label="Operations"><header className="integration-section-header"><div><h4>Available operations</h4><p className="muted-text">These operations are approved, enabled, and available to lines in this project.</p></div></header>{error ? <p className="form-error" role="alert">{error}</p> : null}{operations.length ? <div className="table-wrap"><table className="responsive-record-table"><thead><tr><th scope="col">Operation</th><th scope="col">Description</th><th scope="col">Kind</th><th scope="col">Approval and access</th><th scope="col">Actions</th></tr></thead><tbody>{operations.map((item) => { const record = item as unknown as Record<string, unknown>; const asynchronous = Boolean(item.async); return <tr key={item.operation_id}><td data-label="Operation"><strong>{item.operation}</strong></td><td data-label="Description">{item.description || "No description provided."}</td><td data-label="Kind">{asynchronous ? "Async" : "Sync"}</td><td data-label="Approval and access">{item.requires_approval ? "Available; run approval required" : "Approved and enabled"}</td><td data-label="Actions"><div className="button-row"><Button variant="secondary" type="button" disabled={busy} onClick={() => void describe(record)}>Details</Button>{item.read_only === true && item.schema?.properties && Object.keys(item.schema.properties).length === 0 ? <Button variant="secondary" type="button" disabled={busy} onClick={() => void testRead(record)}>Test read</Button> : null}</div></td></tr>; })}</tbody></table></div> : <p className="muted-text">No operations are currently available to lines in this project.</p>}{selected ? <><div className="button-row">{executionId ? <><Button variant="secondary" type="button" onClick={() => void inspectExecution()}>Inspect execution</Button><Button variant="secondary" type="button" onClick={() => void cancelExecution()}>Cancel execution</Button></> : null}</div><pre className="json-preview">{JSON.stringify(selected, null, 2)}</pre></> : null}</section>;
}
