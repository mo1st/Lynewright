import { Button } from "../ui";
import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "../../api/client";

type Action = { label?: string; action_token?: string; disabled?: boolean };
type ComponentDefinition = {
  component_id: string;
  primitive: string;
  capability_ids?: string[];
  empty_state?: { title?: string; message?: string };
  actions?: Action[];
  data_binding?: { label?: string; value?: string; detail?: string };
  fallback?: "provider_tools";
  fallback_reason?: string;
};
type RegionResponse = { surface_path: string; components: ComponentDefinition[] };

type Props = {
  surfacePath: string;
  projectId?: string | null;
  className?: string;
};

export default function ControlPlaneRegion({ surfacePath, projectId, className = "" }: Props) {
  const [region, setRegion] = useState<RegionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [retryKey, setRetryKey] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    try {
      setRegion(await apiRequest<RegionResponse>(`/api/control-plane/regions/${encodeURIComponent(surfacePath)}${query}`));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "This region could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [projectId, surfacePath]);

  useEffect(() => { void load(); }, [load, retryKey]);

  if (loading) return <section className={`control-plane-region ${className}`} aria-busy="true"><p className="muted-text" role="status">Loading workspace tools…</p></section>;
  if (error) return <section className={`control-plane-region ${className}`} role="alert"><p>{error}</p><Button variant="secondary" type="button" onClick={() => setRetryKey((value) => value + 1)}>Try again</Button></section>;
  if (!region || region.components.length === 0) return <section className={`control-plane-region ${className}`}><p className="muted-text">Nothing is available here yet.</p></section>;

  return <section className={`control-plane-region ${className}`} aria-label={region.surface_path}>
    {region.components.map((component) => <ControlPlaneComponent key={component.component_id} component={component} />)}
  </section>;
}

function ControlPlaneComponent({ component }: { component: ComponentDefinition }) {
  const [actionState, setActionState] = useState<string | null>(null);
  const binding = component.data_binding;
  const known = new Set(["metric_grid", "data_table", "form", "timeseries", "status_card", "job_panel", "key_value", "action_bar", "approval_card", "alert_list", "markdown_text", "setup_requirement", "resource_binding_status", "dependency_status", "trace_timeline"]);
  const isFallback = component.fallback === "provider_tools" || !known.has(component.primitive);
  const actions = (component.actions ?? []).filter((action) => Boolean(action.action_token));
  const runAction = async (token: string) => {
    setActionState("Working…");
    try {
      await apiRequest(`/api/control-plane/actions/${encodeURIComponent(token)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      setActionState("Done");
    } catch (cause) {
      setActionState(cause instanceof Error ? cause.message : "Action failed.");
    }
  };
  return <article className="panel control-plane-component" data-component-id={component.component_id}>
    <header><strong>{binding?.label ?? component.component_id}</strong></header>
    {isFallback ? <p className="muted-text">This capability is available through an integration.</p> : null}
    {binding?.value ? <p className="control-plane-component__value">{binding.value}</p> : null}
    {binding?.detail ? <p className="muted-text">{binding.detail}</p> : null}
    {component.empty_state && !binding?.value ? <p className="muted-text">{component.empty_state.message ?? "No data is available yet."}</p> : null}
    {actions.length > 0 ? <div className="control-plane-component__actions">{actions.map((action) => <Button variant="secondary" key={`${action.action_token}-${action.label}`} type="button" disabled={action.disabled || actionState === "Working…"} onClick={() => void runAction(action.action_token!)}>{action.label ?? "Run action"}</Button>)}</div> : null}
    {actionState ? <p className="muted-text" role="status" aria-live="polite">{actionState}</p> : null}
  </article>;
}
