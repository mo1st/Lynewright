import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, getActiveTenantId, setActiveProjectId } from "../api/client";
import { requestNavigation } from "../router";

export type TenantProject = { project_id: string; tenant_id: string; name?: string };
type ProjectContextValue = { projectId: string; projects: TenantProject[]; loading: boolean; error: string; selectProject(projectId: string): void; refreshProjects(): Promise<void> };
const EMPTY: ProjectContextValue = { projectId: "", projects: [], loading: false, error: "", selectProject: () => undefined, refreshProjects: async () => undefined };
const ProjectContext = createContext(EMPTY);
const storageKey = (tenantId: string) => `lynewright.active-project:${tenantId || "default"}`;

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [projectId, setProjectId] = useState("");
  const [projects, setProjects] = useState<TenantProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const refreshProjects = useCallback(async () => {
    setLoading(true); setError(""); setProjectId(""); setActiveProjectId("");
    try {
      const user = await api.me();
      const tenantId = getActiveTenantId() || user.active_tenant_id || user.tenants[0]?.id || "";
      const available = tenantId ? await api.listTenantProjects(tenantId) : [];
      const preferred = window.localStorage.getItem(storageKey(tenantId)) ?? "";
      const next = available.find((item) => item.project_id === preferred)?.project_id ?? user.tenants.find((item) => item.id === tenantId)?.default_project_id ?? available[0]?.project_id ?? "";
      setProjects(available); setProjectId(next); setActiveProjectId(next);
      window.localStorage.setItem(storageKey(tenantId), next);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Project context could not load."); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void refreshProjects(); window.addEventListener("lynewright:tenant-change", refreshProjects); return () => window.removeEventListener("lynewright:tenant-change", refreshProjects); }, [refreshProjects]);
  const selectProject = useCallback((next: string) => {
    if (!projects.some((item) => item.project_id === next) || next === projectId) return;
    const commit = () => { setProjectId(next); setActiveProjectId(next); window.localStorage.setItem(storageKey(getActiveTenantId()), next); };
    if (requestNavigation({ kind: "project", target: next, commit })) commit();
  }, [projectId, projects]);
  const value = useMemo(() => ({ projectId, projects, loading, error, selectProject, refreshProjects }), [projectId, projects, loading, error, selectProject, refreshProjects]);
  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}
export function useProject(): ProjectContextValue { return useContext(ProjectContext); }
