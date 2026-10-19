import { useState } from "react";
import { Activity, Check, Copy, Moon, Plus, Search, Sun, X } from "lucide-react";
import { Badge, Button, Card, Checkbox, CopyField, Dialog, EmptyState, FormField, IconButton, Input, KeyValue, NavItem, PageHeader, Section, Select, Sidebar, StatusBadge, Switch, Table, Tabs, Textarea, Toast, ToastProvider, useToast, AppShell, Banner, SystemBar } from "../components/ui";
import { SidebarBrand } from "../components/ui/BrandLockup";
import styles from "../components/ui/design-system.module.css";
import { useProject } from "../context/ProjectContext";

const sampleRows = [{ id: "01", name: "integration.example.generate_final_review_image_with_context_and_metadata", state: "approved" }, { id: "02", name: "Image render", state: "running" }];
const columns = [
  { key: "name", header: "Operation", render: (row: typeof sampleRows[number]) => row.name },
  { key: "state", header: "Status", render: (row: typeof sampleRows[number]) => <StatusBadge status={row.state} /> },
];

function Showcase({ theme }: { theme: "light" | "dark" }) {
  const [tab, setTab] = useState("overview");
  const [enabled, setEnabled] = useState(true);
  const [dialogOpen, setDialogOpen] = useState(true);
  const { addToast } = useToast();
  const noop = () => undefined;
  return <section data-theme={theme} className={styles.galleryTheme} aria-label={`${theme} component theme`}>
    <h2 className={styles.sectionTitle}>{theme === "light" ? "Light theme" : "Dark theme"}</h2>
    <AppShell preview sidebar={<Sidebar header={<SidebarBrand />} footer={<small>Signed in as admin</small>}><NavItem className="design-active-nav" onClick={noop} icon={<Activity size={16} />} active>Overview</NavItem><NavItem onClick={noop} icon={<Search size={16} />}>Integrations</NavItem></Sidebar>}>
      <div className={styles.shellContent}><PageHeader title="Page heading" description="A short description explains what is on this page." primaryAction={<Button variant="secondary" icon={<Plus size={16} />}>Create</Button>} secondaryAction={<Button variant="secondary">Secondary</Button>} /></div>
    </AppShell>
    <Section title="Actions" description="Primary is the only accent-filled action; supporting actions stay neutral.">
      <div className={styles.galleryRow}><Button data-testid={`${theme}-button-primary`}>Primary</Button><Button data-testid={`${theme}-button-secondary`} variant="secondary">Secondary</Button><Button data-testid={`${theme}-button-ghost`} variant="ghost">Ghost</Button><Button data-testid={`${theme}-button-danger`} variant="danger">Danger</Button><Button data-testid={`${theme}-button-danger-confirm`} variant="danger" dangerConfirm>Danger confirm</Button><Button data-testid={`${theme}-button-small`} variant="secondary" size="sm">Small</Button><Button variant="secondary" disabled>Disabled</Button><Button variant="secondary" busy>Saving</Button><IconButton label="Close"><X size={16} /></IconButton></div>
    </Section>
    <Section title="Tabs and data">
      <Tabs label="Example sections" className="design-tabs" tabs={[{ key: "overview", label: "Overview" }, { key: "connections", label: "Connections", count: 2 }, { key: "disabled", label: "Unavailable", disabled: true }]} active={tab} onChange={setTab} />
      <div className={styles.galleryGrid}><Table label="Example integrations" columns={columns} rows={sampleRows} getRowKey={(row) => row.id} rowActions={() => <Button size="sm" variant="ghost">Open</Button>} /><Table label="Empty table" columns={columns} rows={[]} getRowKey={(row) => row.id} empty="No integrations yet." /><Table label="Loading table" columns={columns} rows={[]} getRowKey={(row) => row.id} loading /></div>
      <div className={styles.galleryRow}><Badge tone="success">Ready</Badge><Badge tone="warning">Review</Badge><Badge tone="danger">Blocked</Badge><Badge tone="info">Running</Badge><Badge>Unknown</Badge><StatusBadge status="approved" /><StatusBadge status="queued" /></div>
      <KeyValue items={[{ label: "Source", value: "Verified source" }, { label: "Version", value: "1.2.0" }]} />
      <CopyField label="Integration ID" value="lynewright.integration.example" />
    </Section>
    <Section title="Feedback">
      <EmptyState title="No runs yet" description="Start a line to see the run history here." icon={<Activity size={20} />} action={<Button variant="secondary" size="sm">Start a line</Button>} />
      <Banner title="Informational message">A short note with context for the operator.</Banner>
      <Banner tone="warning" title="Needs attention">Review this setup before continuing.</Banner>
      <Banner tone="danger" title="Action blocked">The request could not be completed.</Banner>
      <SystemBar storageKey={`design-gallery-${theme}`} title="System message">This message can be dismissed for this browser session.</SystemBar>
      <Toast tone="success" message="Changes saved." /><Toast tone="warning" message="A runner needs attention." /><Toast tone="danger" message="The action failed." />
      <Button variant="secondary" size="sm" onClick={() => addToast({ tone: "success", message: "Toast added from the shared provider." })}>Show toast</Button>
      <Dialog open={dialogOpen} preview labelledBy={`dialog-title-${theme}`} title="Confirm change" description="Dialog actions are labelled and keyboard accessible." onClose={() => setDialogOpen(false)} footer={<><Button variant="secondary" onClick={() => setDialogOpen(false)}>Cancel</Button><Button variant="secondary" onClick={() => setDialogOpen(false)}>Confirm</Button></>}><p>This is an inline gallery preview of a modal dialog.</p></Dialog>
      {!dialogOpen ? <Button variant="secondary" onClick={() => setDialogOpen(true)}>Show dialog</Button> : null}
    </Section>
    <Section title="Forms">
      <div className={styles.galleryGrid}><FormField label="Integration name" htmlFor={`name-${theme}`} required helpText="Shown in the integrations list."><Input id={`name-${theme}`} placeholder="Example integration" /><Input aria-label="Disabled text input" disabled value="Unavailable" readOnly /></FormField><FormField label="Runtime" htmlFor={`runtime-${theme}`}><Select id={`runtime-${theme}`} defaultValue="native"><option value="native">Native</option><option value="openapi">OpenAPI</option></Select></FormField><FormField label="Summary" htmlFor={`summary-${theme}`} error="A summary is required."><Textarea id={`summary-${theme}`} placeholder="Describe the integration." invalid /></FormField></div>
      <div className={styles.galleryRow}><Checkbox label="I reviewed these changes" defaultChecked /><Switch label="Integration enabled" checked={enabled} onChange={setEnabled} /></div>
    </Section>
  </section>;
}

export default function DesignGallery() {
  const { projectId, projects } = useProject();
  const projectName = projects.find((project) => project.project_id === projectId)?.name || projectId || "Selected project";
  return <ToastProvider><div className={styles.gallery} data-build-only="LYNEWRIGHT_DESIGN_GALLERY_ONLY"><PageHeader title="Design system" description={`${projectName} · Shared components, states, and theme tokens. Available in development builds only.`} /><div className={styles.galleryRow}><IconButton label="Copy sample value"><Copy size={16} /></IconButton><IconButton label="Show ready state"><Check size={16} /></IconButton><IconButton label="Dark mode preview"><Moon size={16} /></IconButton><IconButton label="Light mode preview"><Sun size={16} /></IconButton><EmptyState title="Nothing to select" description="The empty state includes a real icon and a single next action." /></div><div className={styles.galleryThemes}><Showcase theme="light" /><Showcase theme="dark" /></div></div></ToastProvider>;
}
