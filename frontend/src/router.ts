import { useEffect, useState } from "react";
import { ADMIN_ASSISTANTS_NAV, ADMIN_SETTINGS_EXTRA_NAV, ADMIN_SETTINGS_NAV, PRIMARY_NAV, SETTINGS_NAV, type AreaPage } from "./navigation";

export type DashboardRoute = { path: string; page: AreaPage; componentFile: string; label: string };
export const ROUTES: DashboardRoute[] = [
  { path: "/overview", page: "overview", componentFile: "pages/Overview.tsx", label: "Overview" },
  { path: "/lines", page: "lines", componentFile: "pages/ProductionAreas.tsx", label: "Lines" },
  { path: "/runs", page: "runs", componentFile: "pages/ProductionAreas.tsx", label: "Runs" },
  { path: "/reviews", page: "reviews", componentFile: "pages/ProductionAreas.tsx", label: "Reviews" },
  { path: "/integrations", page: "integrations", componentFile: "pages/Integrations.tsx", label: "Integrations" },
  { path: "/catalogue", page: "catalogue", componentFile: "pages/Catalogue.tsx", label: "Catalogue" },
  { path: "/settings", page: "settings", componentFile: "pages/Settings.tsx", label: "AI providers" },
  { path: "/settings/operator", page: "operator", componentFile: "pages/Settings.tsx", label: "Operator" },
  { path: "/settings/telegram", page: "telegram", componentFile: "pages/Settings.tsx", label: "Telegram" },
  { path: "/settings/logs", page: "logs", componentFile: "pages/ApplicationLogs.tsx", label: "Application logs" },
  { path: "/settings/users", page: "users", componentFile: "pages/Settings.tsx", label: "Users" },
  { path: "/settings/assistants", page: "assistants", componentFile: "pages/Settings.tsx", label: "Assistants" },
  { path: "/settings/credentials", page: "credentials", componentFile: "pages/Settings.tsx", label: "Credentials" },
  { path: "/settings/media", page: "media", componentFile: "pages/Settings.tsx", label: "Media" },
  { path: "/account", page: "account", componentFile: "pages/Account.tsx", label: "Account" },
  { path: "/mcp/oauth/consent", page: "oauth", componentFile: "pages/McpOAuthConsent.tsx", label: "MCP OAuth Consent" },
];
export type AppRoute = DashboardRoute & { params: Record<string, string>; canonicalUrl: string };
export type NavigationGuardRequest = { kind: "route" | "project"; target: string; commit: () => void; cancel?: () => void };
type NavigationGuard = (request: NavigationGuardRequest) => boolean;
let navigationGuard: NavigationGuard | null = null;
export function registerNavigationGuard(guard: NavigationGuard): () => void {
  navigationGuard = guard;
  return () => { if (navigationGuard === guard) navigationGuard = null; };
}
export function requestNavigation(request: NavigationGuardRequest): boolean { return navigationGuard?.(request) ?? true; }
export function resolveRoute(location: Pick<Location, "pathname" | "search" | "hash"> = window.location): AppRoute {
  const designRoute: DashboardRoute | null = import.meta.env.DEV && location.pathname === "/design"
    ? { path: "/design", page: "design", componentFile: "pages/DesignGallery.tsx", label: "Design system" }
    : null;
  const dynamicLine = location.pathname.match(/^\/lines\/([^/]+)$/);
  const dynamicRun = location.pathname.match(/^\/runs\/([^/]+)$/);
  const dynamicIntegration = location.pathname.match(/^\/integrations\/([^/]+)(?:\/([^/]+))?\/?$/);
  const dynamicRoute: DashboardRoute | null = dynamicLine
    ? { path: "/lines", page: "lines", componentFile: "pages/ProductionAreas.tsx", label: "Line detail" }
    : dynamicRun
      ? { path: "/runs", page: "runs", componentFile: "pages/ProductionAreas.tsx", label: "Run detail" }
      : dynamicIntegration
        ? { path: "/integrations", page: "integrations", componentFile: "pages/Integrations.tsx", label: "Integration detail" }
      : null;
  const route = designRoute ?? dynamicRoute ?? ROUTES.find((item) => item.path === location.pathname) ?? ROUTES[0];
  const query = new URLSearchParams(location.search);
  const params: Record<string, string> = {};
  for (const key of ["run_id", "line_id", "request_id", "edit", "tab", "status", "line"]) { const value = query.get(key); if (value) params[key] = value; }
  if (dynamicLine) params.id = decodeURIComponent(dynamicLine[1]);
  if (dynamicRun) params.id = decodeURIComponent(dynamicRun[1]);
  if (dynamicIntegration) params.id = decodeURIComponent(dynamicIntegration[1]);
  if (dynamicIntegration?.[2]) params.section = decodeURIComponent(dynamicIntegration[2]);
  return { ...route, params, canonicalUrl: `${route.path}${location.search}${location.hash}` };
}
export function pathForPage(page: AreaPage): string {
  return PRIMARY_NAV.find((item) => item.page === page)?.path ?? SETTINGS_NAV.find((item) => item.page === page)?.path ?? ([ADMIN_SETTINGS_NAV, ADMIN_ASSISTANTS_NAV].find((item) => item.page === page)?.path) ?? ADMIN_SETTINGS_EXTRA_NAV.find((item) => item.page === page)?.path ?? (page === "account" ? "/account" : "/lines");
}
export function navigate(to: string, options: { replace?: boolean } = {}): void {
  const commit = () => {
    window.history[options.replace ? "replaceState" : "pushState"]({}, "", to);
    window.dispatchEvent(new CustomEvent("lynewright:navigate"));
  };
  if (!requestNavigation({ kind: "route", target: to, commit })) return;
  commit();
}
export function useRoute(): AppRoute {
  const [route, setRoute] = useState(() => resolveRoute());
  useEffect(() => {
    const update = () => setRoute(resolveRoute());
    window.addEventListener("popstate", update);
    window.addEventListener("lynewright:navigate", update);
    return () => { window.removeEventListener("popstate", update); window.removeEventListener("lynewright:navigate", update); };
  }, []);
  return route;
}
