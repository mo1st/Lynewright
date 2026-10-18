import { lazy, Suspense, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Activity, Archive, ChevronDown, ClipboardCheck, Home, MoreHorizontal, Plug, Settings as SettingsIcon, Workflow } from "lucide-react";
import { api, getSessionToken, setSessionToken, setActiveTenantId, type CurrentUser } from "./api/client";
const ApplicationLogs = lazy(() => import("./pages/ApplicationLogs"));
const AccountPage = lazy(() => import("./pages/Account"));
const Catalogue = lazy(() => import("./pages/Catalogue"));
const Integrations = lazy(() => import("./pages/Integrations"));
const McpOAuthConsent = lazy(() => import("./pages/McpOAuthConsent"));
const OverviewPage = lazy(() => import("./pages/Overview"));
const Production = lazy(() => import("./pages/Production"));
const Settings = lazy(() => import("./pages/Settings"));
const LinesPage = lazy(() => import("./pages/ProductionAreas").then((module) => ({ default: module.LinesPage })));
const LineDetailPage = lazy(() => import("./pages/ProductionAreas").then((module) => ({ default: module.LineDetailPage })));
const RunsPage = lazy(() => import("./pages/ProductionAreas").then((module) => ({ default: module.RunsPage })));
const RunDetailPage = lazy(() => import("./pages/ProductionAreas").then((module) => ({ default: module.RunDetailPage })));
const ReviewsPage = lazy(() => import("./pages/ProductionAreas").then((module) => ({ default: module.ReviewsPage })));
import { ADMIN_SETTINGS_EXTRA_NAV, ADMIN_SETTINGS_NAV, SETTINGS_NAV } from "./navigation";
import { navigate, useRoute } from "./router";
import { ProjectProvider, useProject } from "./context/ProjectContext";
import { useTheme } from "./context/ThemeContext";
import { AppShell, Button, Dialog, FormField, NavItem, PageHeader, Select, Sidebar, Tabs, ThemeToggle, ToastProvider } from "./components/ui";
import { SystemWarnings } from "./components/ui/SystemWarnings";
import { useDashboardSummary } from "./useDashboardSummary";
import type { AreaPage } from "./navigation";
import styles from "./components/ui/design-system.module.css";

const DesignGallery = import.meta.env.DEV ? lazy(() => import("./pages/DesignGallery")) : null;

const NAV_ITEMS: Array<{ page: AreaPage; path: string; label: string; icon: typeof Home }> = [
  { page: "overview", path: "/overview", label: "Overview", icon: Home },
  { page: "lines", path: "/lines", label: "Lines", icon: Workflow },
  { page: "runs", path: "/runs", label: "Runs", icon: Activity },
  { page: "reviews", path: "/reviews", label: "Reviews", icon: ClipboardCheck },
  { page: "integrations", path: "/integrations", label: "Integrations", icon: Plug },
  { page: "catalogue", path: "/catalogue", label: "Catalogue", icon: Archive },
  { page: "settings", path: "/settings", label: "Settings", icon: SettingsIcon },
];

export default function App() {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [busy, setBusy] = useState(false);
  const { setUserId } = useTheme();
  const route = useRoute();

  useEffect(() => {
    document.title = user ? `${route.label} · Lynewright` : "Sign in · Lynewright";
    if (window.location.pathname === "/") navigate("/overview", { replace: true });
  }, [route.label, user]);
  useEffect(() => { setUserId(user ? String(user.id) : null); }, [setUserId, user?.id]);

  useEffect(() => {
    if (!getSessionToken()) { setAuthReady(true); return; }
    let active = true;
    api.me().then((current) => {
      if (active) { setActiveTenantId(current.active_tenant_id ?? current.tenants[0]?.id ?? ""); setUser(current); }
    }).catch(() => { setSessionToken(""); }).finally(() => { if (active) setAuthReady(true); });
    return () => { active = false; };
  }, []);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setAuthError("");
    try {
      const current = await api.login(email, password);
      setActiveTenantId(current.active_tenant_id ?? current.tenants[0]?.id ?? ""); setUser(current); setPassword("");
      navigate("/overview", { replace: true });
    } catch (error) {
      setSessionToken("");
      setAuthError(error instanceof Error ? error.message : "Sign in failed.");
    } finally { setAuthReady(true); setBusy(false); }
  }

  if (!authReady) return <main className={styles.loginScreen}><p role="status">Restoring your session…</p></main>;
  if (!user) return <main className={styles.loginScreen}>
    <div className={styles.loginStack}>
      <div className={styles.loginWordmark}>Lynewright</div>
      <form className={styles.loginCard} onSubmit={(event) => void signIn(event)} aria-busy={busy}>
        <div><h1>Welcome back</h1><p>Sign in with your dashboard account.</p></div>
        <label className={styles.loginField}><span>Email</span><input type="email" autoComplete="username" required value={email} onChange={(event) => setEmail(event.target.value)} /></label>
        <label className={styles.loginField}><span>Password</span><input type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        <Button type="submit" busy={busy}>Sign in</Button>
        <div className={styles.loginErrorSlot} aria-live="polite">{authError ? <p role="alert">{authError}</p> : null}</div>
      </form>
    </div>
  </main>;

  return <ProjectProvider><ToastProvider><Dashboard user={user} onUserUpdated={(email) => setUser((current) => current ? { ...current, email } : current)} onSignOut={() => { setUser(null); setSessionToken(""); navigate("/overview", { replace: true }); }} /></ToastProvider></ProjectProvider>;
}

function ProjectPicker({ id = "project-picker" }: { id?: string }) {
  const { projectId, projects, loading, selectProject } = useProject();
  return <FormField label="Project" htmlFor={id} className={styles.projectPicker}>
    <Select id={id} value={projectId} disabled={loading || !projects.length} onChange={(event) => selectProject(event.target.value)}>
      {!projects.length ? <option value="">{loading ? "Loading projects…" : "No projects available"}</option> : null}
      {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name || project.project_id}</option>)}
    </Select>
  </FormField>;
}

function AccountMenu({ user, onSignOut }: { user: CurrentUser; onSignOut: () => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => { if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false); };
    const keydown = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", keydown);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", keydown); };
  }, [open]);
  const initial = user.email.trim().charAt(0).toUpperCase() || "L";
  return <div className={styles.accountMenu} ref={root}>
    <Button variant="ghost" type="button" className={`${styles.accountTrigger} ui-account-trigger`} aria-haspopup="true" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <span className={styles.accountAvatar} aria-hidden="true">{initial}</span><span className={styles.accountEmail} title={user.email}>{user.email}</span><ChevronDown size={16} aria-hidden="true" />
    </Button>
    {open ? <div className={styles.accountPopover} aria-label="Account settings">
      <strong className={styles.accountPopoverEmail}>{user.email}</strong>
      <Button variant="ghost" className={styles.accountSignOut} onClick={() => { setOpen(false); navigate("/account"); }}>Account</Button>
      <div className={styles.accountTheme}><span>Appearance</span><ThemeToggle mode="options" /></div>
      <Button variant="ghost" className={styles.accountSignOut} onClick={onSignOut}>Sign out</Button>
    </div> : null}
  </div>;
}

function SettingsWorkspace({ routePath, isAdmin, children }: { routePath: string; isAdmin: boolean; children: ReactNode }) {
  const tabs = isAdmin ? [...SETTINGS_NAV, ADMIN_SETTINGS_NAV, ...ADMIN_SETTINGS_EXTRA_NAV] : SETTINGS_NAV;
  const active = tabs.find((item) => item.path === routePath)?.path ?? "/settings";
  const { projectId, projects } = useProject();
  const projectName = projects.find((project) => project.project_id === projectId)?.name || projectId || "Selected project";
  return <div className={styles.settingsWorkspace}>
    <PageHeader title="Settings" description={`${projectName} · Manage providers, operator workflows, Telegram, and application logs.`} />
    <Tabs<string> label="Settings sections" tabs={tabs.map((item) => ({ key: item.path, label: item.label }))} active={active} onChange={(path) => navigate(path)} className={styles.settingsTabs} />
    <div className={styles.settingsContent}>{children}</div>
  </div>;
}

function Dashboard({ user, onUserUpdated, onSignOut }: { user: CurrentUser; onUserUpdated: (email: string) => void; onSignOut: () => void }) {
  const route = useRoute();
  const { projectId, loading: projectLoading, projects } = useProject();
  const projectName = projects.find((project) => project.project_id === projectId)?.name || projectId || "Selected project";
  const summary = useDashboardSummary(projectId, projectLoading);
  const [moreOpen, setMoreOpen] = useState(false);
  const settingsArea = route.path.startsWith("/settings");
  const waitingCount = summary.runs.filter((run) => run.status === "waiting").length + summary.approvals.length;
  const activePage = (page: AreaPage) => page === "settings" ? settingsArea : route.page === page;
  const goTo = (path: string) => { navigate(path); setMoreOpen(false); };
  async function signOut() { try { await api.logout(); } catch { setSessionToken(""); } onSignOut(); }

  if (route.page === "oauth") return <Suspense fallback={<p role="status">Loading sign-in…</p>}><McpOAuthConsent requestId={route.params.request_id ?? null} /></Suspense>;
  return <>
    <AppShell className={styles.dashboardShell} sidebar={<Sidebar
      className={styles.dashboardSidebar}
      navClassName={styles.dashboardSidebarNav}
      label="Primary navigation"
      header={<div className={styles.sidebarTop}><div className={styles.sidebarWordmark}>Lynewright</div><ProjectPicker /></div>}
      footer={<AccountMenu user={user} onSignOut={() => void signOut()} />}
    >{NAV_ITEMS.map((item) => {
      const Icon = item.icon;
      const badge = item.page === "reviews" && waitingCount > 0 ? <span className={styles.navigationCount}>{waitingCount}</span> : undefined;
      return <NavItem key={item.path} href={item.path} active={activePage(item.page)} icon={<Icon size={18} strokeWidth={1.8} />} badge={badge} onClick={() => goTo(item.path)}>{item.label}</NavItem>;
    })}</Sidebar>} systemBar={<SystemWarnings />}>
      <main className={styles.dashboardContent} id="main-content" tabIndex={-1}>
        {route.page === "overview" ? <Suspense fallback={<p role="status">Loading overview…</p>}><OverviewPage summary={summary} /></Suspense> : null}
        {route.page === "lines" ? <Suspense fallback={<p role="status">Loading lines…</p>}>
          {route.params.edit ? <Production view="lines" projectId={projectId} projectName={projectName} projectLoading={projectLoading} lineId={route.params.line_id} />
            : route.params.id ? <LineDetailPage lineId={route.params.id} projectId={projectId} projectName={projectName} />
              : <LinesPage projectId={projectId} projectName={projectName} projectLoading={projectLoading} />}
        </Suspense> : null}
        {route.page === "runs" ? <Suspense fallback={<p role="status">Loading runs…</p>}>
          {route.params.id ? <RunDetailPage runId={route.params.id} projectId={projectId} projectName={projectName} />
            : <RunsPage projectId={projectId} projectName={projectName} projectLoading={projectLoading} statusFilter={route.params.status} lineFilter={route.params.line} />}
        </Suspense> : null}
        {route.page === "reviews" ? <Suspense fallback={<p role="status">Loading reviews…</p>}><ReviewsPage projectId={projectId} projectName={projectName} projectLoading={projectLoading} /></Suspense> : null}
        {route.page === "design" && DesignGallery ? <Suspense fallback={<p role="status">Loading design system…</p>}><DesignGallery /></Suspense> : null}
        {route.page === "integrations" ? <Suspense fallback={<p role="status">Loading integrations…</p>}><Integrations /></Suspense> : null}
        {route.page === "catalogue" ? <Suspense fallback={<p role="status">Loading catalogue…</p>}><Catalogue /></Suspense> : null}
        {route.page === "account" ? <Suspense fallback={<p role="status">Loading account…</p>}><AccountPage user={user} onSignOut={onSignOut} onUserUpdated={onUserUpdated} /></Suspense> : null}
        {settingsArea ? <Suspense fallback={<p role="status">Loading settings…</p>}><SettingsWorkspace routePath={route.path} isAdmin={user.role === "admin"}>
          {route.page === "settings" ? <Settings panel="providers" /> : null}
          {route.page === "operator" ? <Settings panel="operator" /> : null}
          {route.page === "telegram" ? <Settings panel="telegram" /> : null}
          {route.page === "users" && user.role === "admin" ? <Settings panel="users" currentUserId={user.id} /> : null}
          {route.page === "users" && user.role !== "admin" ? <p className="error-message" role="alert">Only an admin can manage users.</p> : null}
          {route.page === "credentials" && user.role === "admin" ? <Settings panel="credentials" /> : null}
          {route.page === "media" && user.role === "admin" ? <Settings panel="media" /> : null}
          {(route.page === "credentials" || route.page === "media") && user.role !== "admin" ? <p className="error-message" role="alert">Only an admin can manage this data.</p> : null}
          {route.page === "logs" ? <ApplicationLogs showPageHeader={false} /> : null}
        </SettingsWorkspace></Suspense> : null}
      </main>
    </AppShell>
    <nav className={styles.mobileBottomNav} aria-label="Mobile navigation" data-nav-orientation="horizontal">
      {[
        { label: "Overview", path: "/overview", icon: Home, page: "overview" as AreaPage },
        { label: "Lines", path: "/lines", icon: Workflow, page: "lines" as AreaPage },
        { label: "Reviews", path: "/reviews", icon: ClipboardCheck, page: "reviews" as AreaPage },
        { label: "Integrations", path: "/integrations", icon: Plug, page: "integrations" as AreaPage },
      ].map((item) => {
        const Icon = item.icon;
        return <Button variant="ghost" key={item.path} type="button" className={`mobile-nav-item ${styles.mobileNavItem}${activePage(item.page) ? ` ${styles.mobileNavItemActive}` : ""}`} aria-current={activePage(item.page) ? "page" : undefined} onClick={() => goTo(item.path)}>
          <span className={styles.mobileNavIcon}><Icon size={18} aria-hidden="true" />{item.page === "reviews" && waitingCount > 0 ? <span className={styles.mobileNavBadge}>{waitingCount}</span> : null}</span><span>{item.label}</span>
        </Button>;
      })}
      <Button variant="ghost" type="button" className={`mobile-nav-item ${styles.mobileNavItem}${moreOpen || ["runs", "catalogue", "settings", "operator", "telegram", "logs"].includes(route.page) ? ` ${styles.mobileNavItemActive}` : ""}`} aria-expanded={moreOpen} onClick={() => setMoreOpen(true)}><MoreHorizontal size={18} aria-hidden="true" /><span>More</span></Button>
    </nav>
    <Dialog open={moreOpen} title="More" description="Additional areas, project, and account." onClose={() => setMoreOpen(false)} className={styles.mobileMoreDialog} backdropClassName={styles.mobileMoreBackdrop}>
      <div className={styles.moreLinks} role="group" aria-label="More navigation">
        <NavItem href="/runs" active={route.page === "runs"} icon={<Activity size={18} />} onClick={() => goTo("/runs")}>Runs</NavItem>
        <NavItem href="/catalogue" active={route.page === "catalogue"} icon={<Archive size={18} />} onClick={() => goTo("/catalogue")}>Catalogue</NavItem>
        <NavItem href="/settings" active={settingsArea} icon={<SettingsIcon size={18} />} onClick={() => goTo("/settings")}>Settings</NavItem>
      </div>
      <ProjectPicker id="mobile-project-picker" />
      <AccountMenu user={user} onSignOut={() => void signOut()} />
    </Dialog>
  </>;
}
