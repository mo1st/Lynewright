import { Button } from "../ui";
import { useEffect, useState } from "react";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationManagementSummary } from "../../types";
import type { Mutate } from "./sectionTypes";

type Props = { integrationId: string; refreshToken: IntegrationManagementSummary; busy: boolean; mutate: Mutate };

export default function BindingsSection({ integrationId, refreshToken, busy, mutate }: Props) {
  const [bindings, setBindings] = useState<Record<string, unknown>[]>([]);
  useEffect(() => { let active = true; void integrationManagementApi.bindings(integrationId).then((response) => { if (active) setBindings(response.items); }).catch(() => { if (active) setBindings([]); }); return () => { active = false; }; }, [integrationId, refreshToken]);
  async function repair(binding: Record<string, unknown>) {
    const id = String(binding.id ?? "");
    const reason = window.prompt("Why does this binding need repair?", "Operator requested a consistency check.");
    if (!id || !reason?.trim()) return;
    await mutate(`binding-repair:${id}`, () => integrationManagementApi.repairBinding(integrationId, id, reason.trim()), "Binding repair requested.");
  }
  return <section className="integration-subsection" aria-label="Bindings"><header className="panel-header"><div><h4>Bindings</h4><p className="muted-text">Canonical project relationships remain separate from remote resources.</p></div></header>{bindings.length ? <div className="table-wrap integration-table-wrap"><table className="responsive-record-table integration-record-table"><thead><tr><th scope="col">Resource</th><th scope="col">Project</th><th scope="col">State</th><th scope="col">Actions</th></tr></thead><tbody>{bindings.map((binding) => { const id = String(binding.id ?? ""); const state = String(binding.state ?? "UNKNOWN"); const stateLabel = state === "PENDING" ? "Pending — awaiting lifecycle verification" : state; return <tr key={id}><td data-label="Resource" className="mono-text">{String(binding.resource_type ?? "unknown")}</td><td data-label="Project" className="mono-text">{String(binding.project_id ?? "Unscoped")}</td><td data-label="State"><span aria-label={state === "PENDING" ? "Pending, awaiting lifecycle verification" : state}>{stateLabel}</span></td><td data-label="Actions">{state === "DEGRADED" ? <Button variant="secondary" type="button" disabled={busy} onClick={() => void repair(binding)}>Repair</Button> : <span className="muted-text">{state === "PENDING" ? "Awaiting verification" : "No action needed"}</span>}</td></tr>; })}</tbody></table></div> : <p className="muted-text">No bindings exist yet. Bind a discovered resource to a project when it is ready.</p>}</section>;
}
