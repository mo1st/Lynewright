import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ReactNode } from "react";
import { Activity, Copy } from "lucide-react";
import { AppShell, Banner, Badge, Button, Card, Checkbox, CopyField, Dialog, EmptyState, FormField, IconButton, Input, KeyValue, NavItem, PageHeader, Section, Select, Sidebar, StatusBadge, Switch, Table, Tabs, Textarea, ThemeToggle, Toast, ToastProvider, SystemBar } from "./index";
import { PageState } from "./PageState";

const columns = [{ key: "name", header: "Name", render: (row: { name: string }) => row.name }];
const cell = (theme: "light" | "dark", node: ReactNode) => renderToStaticMarkup(<div data-theme={theme}>{node}</div>);

describe("design system component rendering", () => {
  it.each(["light", "dark"] as const)("renders layout components with the %s token scope", (theme) => {
    const markup = cell(theme, <AppShell sidebar={<Sidebar header="Brand" footer="Account"><NavItem active icon={<Activity size={16} />} onClick={() => undefined}>Lines</NavItem></Sidebar>}><main><PageHeader title="Lines" description="Production lines" primaryAction={<Button>Create line</Button>} /><Section title="Recent" description="Latest runs"><Card>Latest run</Card></Section></main></AppShell>);
    expect(markup).toContain(`data-theme="${theme}"`);
    expect(markup).toContain('aria-label="Main navigation"');
    expect(markup).toContain('aria-current="page"');
    expect(markup).toContain("Create line");
    expect(markup).toContain("Latest run");
  });

  it("renders action variants, sizes, busy and icon-only actions accessibly", () => {
    const markup = cell("light", <><Button>Primary</Button><Button variant="secondary" size="sm">Save</Button><Button variant="ghost">Quiet</Button><Button variant="danger" busy>Removing</Button><IconButton label="Copy value"><Copy size={16} /></IconButton></>);
    expect(markup).toContain('aria-busy="true"');
    expect(markup).toContain('aria-label="Copy value"');
    expect(markup).toContain("Removing");
  });

  it("renders horizontally accessible tabs with selected state and counts", () => {
    const markup = renderToStaticMarkup(<Tabs label="Integration sections" tabs={[{ key: "overview", label: "Overview" }, { key: "tools", label: "Tools", count: 2 }]} active="tools" onChange={() => undefined} />);
    expect(markup).toContain('role="tablist"');
    expect(markup).toContain('aria-selected="true"');
    expect(markup).toContain("2");
  });

  it("renders data tables, empty/loading states, status mapping and key-value rows", () => {
    const markup = renderToStaticMarkup(<><Table label="Integrations" columns={columns} rows={[{ name: "OpenAPI" }]} getRowKey={(row) => row.name} /><Table label="Empty" columns={columns} rows={[]} getRowKey={(row) => row.name} empty="Nothing to show" /><Table label="Loading" columns={columns} rows={[]} getRowKey={(row) => row.name} loading /><Badge tone="success">Ready</Badge><StatusBadge status="running" /><KeyValue items={[{ label: "Kind", value: "native" }]} /></>);
    expect(markup).toContain('aria-label="Integrations"');
    expect(markup).toContain("Nothing to show");
    expect(markup).toContain("Loading…");
    expect(markup).toContain("running");
    expect(markup).toContain("native");
  });

  it("renders advanced copy fields and real-icon empty states", () => {
    const markup = renderToStaticMarkup(<><CopyField label="Artifact hash" value="a1b2c3" /><EmptyState title="No files" description="Add an integration to get started." icon={<Activity size={16} />} action={<Button>Import</Button>} /></>);
    expect(markup).toContain('aria-label="Copy Artifact hash"');
    expect(markup).toContain("a1b2c3");
    expect(markup).toContain("No files");
    expect(markup).toContain("Import");
  });

  it("renders informational, warning, danger and per-session system banners", () => {
    const markup = renderToStaticMarkup(<><Banner>Information</Banner><Banner tone="warning" title="Review">Needs review</Banner><Banner tone="danger" title="Blocked">Request blocked</Banner><SystemBar storageKey="test-bar">Per-session notice</SystemBar></>);
    expect(markup).toContain("Information");
    expect(markup).toContain("Needs review");
    expect(markup).toContain('aria-label="Dismiss message"');
    expect(markup).toContain("Per-session notice");
  });

  it("renders a labelled modal and toast states", () => {
    const markup = renderToStaticMarkup(<><Dialog open title="Confirm change" description="Check the details." onClose={() => undefined}>Dialog body</Dialog><Toast tone="warning" message="Runner warning" onDismiss={() => undefined} /><ToastProvider><span>Provider content</span></ToastProvider></>);
    expect(markup).toContain('role="dialog"');
    expect(markup).toContain('aria-labelledby="ui-dialog-title"');
    expect(markup).toContain("Runner warning");
    expect(markup).toContain("Provider content");
  });

  it("renders labelled fields and control variants", () => {
    const markup = renderToStaticMarkup(<><FormField label="Name" htmlFor="name" required helpText="Used in the list."><Input id="name" placeholder="Example" /></FormField><FormField label="Runtime" htmlFor="runtime"><Select id="runtime"><option>Native</option></Select></FormField><Textarea aria-label="Notes" /><Checkbox aria-label="I agree" label="I agree" /><Switch label="Enabled" checked onChange={() => undefined} /></>);
    expect(markup).toContain('for="name"');
    expect(markup).toContain("Used in the list.");
    expect(markup).toContain('role="switch"');
    expect(markup).toContain('type="checkbox"');
  });

  it("renders the theme control and shared page state", () => {
    const markup = renderToStaticMarkup(<><ThemeToggle /><PageState kind="empty" message="Nothing has run yet." /><PageState kind="error" message="Reload the page." /></>);
    expect(markup).toContain("Theme:");
    expect(markup).toContain("Nothing has run yet.");
    expect(markup).toContain("Reload the page.");
    expect(markup).toContain("<svg");
  });
});
