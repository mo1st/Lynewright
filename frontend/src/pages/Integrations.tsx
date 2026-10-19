import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, Boxes, ChevronDown, CircleHelp, FileUp, Link2, Package, Plug, Settings2 } from "lucide-react";
import { api } from "../api/client";
import { integrationManagementApi, type IntegrationRemovalPreview } from "../api/integrationManagementClient";
import { productionLinesApi, type ProductionOperation } from "../api/productionLinesClient";
import type { CredentialMetadata } from "../api/client";
import type { IntegrationInstallConfirmation, IntegrationManagementConnection, IntegrationManagementSummary, IntegrationSettingsModel } from "../types";
import { useProject } from "../context/ProjectContext";
import { navigate, useRoute } from "../router";
import { Badge, Button, Card, Checkbox, CopyField, Dialog, EmptyState, FormField, Input, KeyValue, PageHeader, SearchInput, Select, StatusBadge, Table, Tabs, useToast } from "../components/ui";
import AuthorizationSection from "../components/integrations/AuthorizationSection";
import BindingsSection from "../components/integrations/BindingsSection";
import ConnectionManagementSection from "../components/integrations/ConnectionManagementSection";
import DefinitionUpgradeSection from "../components/integrations/DefinitionUpgradeSection";
import ProductionLineSettings from "../components/production/ProductionLineSettings";
import GenericOperationsSection from "../components/integrations/GenericOperationsSection";
import HealthDiagnosticsSection from "../components/integrations/HealthDiagnosticsSection";
import IntegrationFileSection from "../components/integrations/IntegrationFileSection";
import IntegrationProvenanceDetails from "../components/integrations/IntegrationProvenanceDetails";
import IntegrationDraftSection from "../components/integrations/IntegrationDraftSection";
import ManualIntegrationSection from "../components/integrations/ManualIntegrationSection";
import McpContractMappingsSection from "../components/integrations/McpContractMappingsSection";
import McpResourceMappingsSection from "../components/integrations/McpResourceMappingsSection";
import OpenApiManagementSection from "../components/integrations/OpenApiManagementSection";
import ResourcesSection from "../components/integrations/ResourcesSection";
import SetupRequirementsSection from "../components/integrations/SetupRequirementsSection";
import WebhooksSection from "../components/integrations/WebhooksSection";
import InstallConfirmationDialog from "../components/integrations/InstallConfirmationDialog";
import IntegrationRunnerSection from "../components/integrations/IntegrationRunnerSection";
import { installConfirmationFromError, type PendingInstallConfirmation } from "../components/integrations/installConfirmation";
import type { Mutate } from "../components/integrations/sectionTypes";
import styles from "../components/integrations/IntegrationsWorkspace.module.css";

type IntegrationKind = "Native" | "OpenAPI" | "MCP" | "Webhook" | "Container";
type SourceKind = "Catalogue" | "Outside" | "Bundled";
type SectionKey = "Overview" | "Connections" | "Operations" | "Settings" | "Runner" | "Health & diagnostics" | "MCP tools" | "OpenAPI mappings" | "Resources" | "Bindings" | "Setup" | "Webhooks";
type AddFlow = "chooser" | "file" | "draft" | "manual" | null;

const sectionSlugs: Record<SectionKey, string> = {
  Overview: "overview", Connections: "connections", Operations: "operations", Settings: "settings", Runner: "runner",
  "Health & diagnostics": "health", "MCP tools": "mcp-tools", "OpenAPI mappings": "openapi-mappings", Resources: "resources", Bindings: "bindings", Setup: "setup", Webhooks: "webhooks",
};
const sectionsBySlug = Object.fromEntries(Object.entries(sectionSlugs).map(([section, slug]) => [slug, section])) as Record<string, SectionKey>;

function kindFor(item: IntegrationManagementSummary): IntegrationKind {
  if (item.availability.execution_plane === "container") return "Container";
  const runtime = String(item.integration.configuration?.manual?.runtime ?? "").toLowerCase();
  if (runtime === "openapi") return "OpenAPI";
  if (runtime === "mcp") return "MCP";
  if (runtime === "webhook") return "Webhook";
  const declared = String(item.availability.kind ?? item.integration.kind ?? "").toLowerCase();
  if (["openapi", "mcp", "webhook", "container"].includes(declared)) return declared as IntegrationKind;
  return "Native";
}

function sourceFor(item: IntegrationManagementSummary): SourceKind {
  if (item.provenance?.kind === "catalogue") return "Catalogue";
  if (item.provenance?.kind === "bundled" || item.integration.source === "bundled") return "Bundled";
  return "Outside";
}

function sourceLabel(item: IntegrationManagementSummary) {
  if (item.provenance?.kind === "catalogue") return item.provenance.signing_key_id?.startsWith("dev:") ? "Catalogue (dev key)" : "Catalogue ✓";
  if (item.provenance?.kind === "bundled" || item.integration.source === "bundled") return "Bundled";
  return "Outside ⚠";
}

function needsAttention(item: IntegrationManagementSummary) {
  return Boolean(item.attention?.length || item.provenance?.status === "catalogue_drift" || item.provenance?.status === "awaiting_confirmation" || item.availability.available === false || item.integration.status === "error");
}

function friendlyStatus(item: IntegrationManagementSummary) {
  if (needsAttention(item)) return "Needs attention";
  return item.integration.enabled ? "Enabled" : "Disabled";
}

function relativeTime(value?: string | null) {
  if (!value) return "Not seen yet";
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return "Not available";
  const seconds = Math.round((timestamp - Date.now()) / 1000);
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [["day", 86400], ["hour", 3600], ["minute", 60]];
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, divisor] of units) if (Math.abs(seconds) >= divisor) return formatter.format(Math.round(seconds / divisor), unit);
  return formatter.format(seconds, "second");
}

function downloadText(filename: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: "application/yaml;charset=utf-8" }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = filename;
  document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
}

export default function Integrations() {
  const route = useRoute();
  const { projectId } = useProject();
  const { addToast } = useToast();
  const integrationId = route.params.id ?? "";
  const section = sectionsBySlug[route.params.section ?? "overview"] ?? "Overview";
  const setSection = (next: SectionKey, replace = false) => {
    if (integrationId) navigate(`/integrations/${encodeURIComponent(integrationId)}/${sectionSlugs[next]}`, { replace });
  };
  const [items, setItems] = useState<IntegrationManagementSummary[]>([]);
  const [installPolicy, setInstallPolicy] = useState<"catalogue-only" | "confirm" | "open">("confirm");
  const [connections, setConnections] = useState<IntegrationManagementConnection[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [credentials, setCredentials] = useState<CredentialMetadata[]>([]);
  const [settings, setSettings] = useState<IntegrationSettingsModel | null>(null);
  const [settingsUnavailable, setSettingsUnavailable] = useState(false);
  const [selected, setSelected] = useState<IntegrationManagementSummary | null>(null);
  const [operationsByIntegration, setOperationsByIntegration] = useState<Record<string, ProductionOperation[]>>({});
  const [operationsLoading, setOperationsLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [kindFilter, setKindFilter] = useState("All kinds");
  const [statusFilter, setStatusFilter] = useState("All statuses");
  const [sourceFilter, setSourceFilter] = useState("All sources");
  const [busy, setBusy] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [addFlow, setAddFlow] = useState<AddFlow>(null);
  const [confirmation, setConfirmation] = useState<PendingInstallConfirmation | null>(null);
  const [confirmingPackageId, setConfirmingPackageId] = useState("");
  const [packageConfirmationId, setPackageConfirmationId] = useState("");
  const [removal, setRemoval] = useState<IntegrationRemovalPreview | null>(null);
  const [removeDialogOpen, setRemoveDialogOpen] = useState(false);
  const [removeTypedId, setRemoveTypedId] = useState("");
  const [disableUsingLines, setDisableUsingLines] = useState(false);
  const [deleteExclusiveCredentials, setDeleteExclusiveCredentials] = useState(false);

  const reloadList = useCallback(async () => {
    const response = await integrationManagementApi.list();
    setItems(response.items ?? []); setInstallPolicy(response.install_policy ?? "confirm");
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true); setError("");
    Promise.all([integrationManagementApi.list(), api.getCredentials()]).then(([response, refs]) => {
      if (!active) return;
      setItems(response.items ?? []); setCredentials(refs); setInstallPolicy(response.install_policy ?? "confirm");
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "Integrations could not be loaded."); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    setOperationsLoading(true);
    if (!projectId) { setOperationsByIntegration({}); setOperationsLoading(false); return () => { active = false; }; }
    productionLinesApi.operations(projectId).then(({ items: operations }) => {
      if (!active) return;
      const grouped: Record<string, ProductionOperation[]> = {};
      for (const operation of operations) {
        if (!operation.integration_id) continue;
        (grouped[operation.integration_id] ??= []).push(operation);
      }
      setOperationsByIntegration(grouped);
    }).catch(() => { if (active) setOperationsByIntegration({}); }).finally(() => { if (active) setOperationsLoading(false); });
    return () => { active = false; };
  }, [projectId, items]);

  useEffect(() => {
    let active = true;
    setSelected(null); setConnections([]); setSettings(null); setSettingsUnavailable(false);
    if (!integrationId) return () => { active = false; };
    setError("");
    integrationManagementApi.get(integrationId).then((result) => {
      if (!active) return;
      setSelected(result); setConnections(result.connections ?? []);
      setConnectionId(result.connections?.[0]?.id ?? "");
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "Integration details could not be loaded."); });
    return () => { active = false; };
  }, [integrationId]);

  useEffect(() => {
    let active = true;
    if (section !== "Settings" || !selected || !integrationId) { setSettings(null); setSettingsUnavailable(false); return () => { active = false; }; }
    if (!selected.management.settings_schema) { setSettings(null); setSettingsUnavailable(true); return () => { active = false; }; }
    setSettingsUnavailable(false);
    integrationManagementApi.settings(integrationId).then((model) => { if (active) setSettings(model); }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "Declared settings could not be loaded."); });
    return () => { active = false; };
  }, [section, integrationId, selected]);

  useEffect(() => {
    if (integrationId) return;
    const legacyId = new URLSearchParams(window.location.search).get("integration_id");
    if (legacyId) navigate(`/integrations/${encodeURIComponent(legacyId)}`);
  }, [integrationId]);

  const filteredItems = useMemo(() => items.filter((item) => {
    const kind = kindFor(item); const source = sourceFor(item); const status = friendlyStatus(item);
    const matchesSearch = `${item.integration.name} ${item.integration.id}`.toLowerCase().includes(search.toLowerCase().trim());
    return matchesSearch && (kindFilter === "All kinds" || kind === kindFilter)
      && (statusFilter === "All statuses" || status === statusFilter)
      && (sourceFilter === "All sources" || source === sourceFilter);
  }), [items, search, kindFilter, statusFilter, sourceFilter]);

  const runtime = selected ? String(selected.integration.configuration?.manual?.runtime ?? "") : "";
  const manual = Boolean(selected?.availability.manual);
  const runtimeUnavailable = Boolean(selected && selected.availability.available === false);
  const integrationEnabled = Boolean(selected?.integration.enabled);
  const selectedConnection = connections.find((item) => item.id === connectionId) ?? null;
  const availableSections = useMemo<SectionKey[]>(() => {
    if (!selected) return [];
    const sections: SectionKey[] = ["Overview"];
    if (manual || connections.length) sections.push("Connections");
    if ((operationsByIntegration[selected.integration.id]?.length ?? 0) > 0) sections.push("Operations");
    if (selected.management.settings_schema) sections.push("Settings");
    if (selected.availability.execution_plane === "container") sections.push("Runner");
    sections.push("Health & diagnostics");
    if (runtime === "mcp") sections.push("MCP tools");
    if (runtime === "openapi") sections.push("OpenAPI mappings");
    if (selected.management.resource_types.length || selected.activity.resource_count) sections.push("Resources");
    if (selected.activity.binding_count || selected.activity.resource_count) sections.push("Bindings");
    if (selected.setup_requirements.length) sections.push("Setup");
    if (runtime === "webhook" || selected.inbound.length) sections.push("Webhooks");
    return sections;
  }, [selected, manual, connections.length, operationsByIntegration, runtime]);

  useEffect(() => {
    if (!integrationId || !selected || route.params.section === sectionSlugs[section]) return;
    setSection(section, true);
  }, [integrationId, selected, route.params.section, section]);

  useEffect(() => {
    if (!selected || operationsLoading || !availableSections.length || availableSections.includes(section)) return;
    setSection("Overview", true);
  }, [selected, operationsLoading, availableSections, section]);

  const mutate: Mutate = async <T,>(key: string, action: () => Promise<T>, message: string): Promise<T | undefined> => {
    setBusy(key); setError(""); setNotice("");
    try {
      const result = await action(); setNotice(message);
      if (integrationId) { const updated = await integrationManagementApi.get(integrationId); setSelected(updated); setConnections(updated.connections ?? []); }
      await reloadList(); return result;
    } catch (caught) { setError(caught instanceof Error ? caught.message : "The integration action failed."); return undefined; }
    finally { setBusy(""); }
  };

  async function onCreated(integration: IntegrationManagementSummary) {
    setAddFlow(null); await reloadList(); navigate(`/integrations/${encodeURIComponent(integration.integration.id)}/overview`);
  }
  async function beginPackageConfirmation(id: string) {
    setConfirmingPackageId(id); setPackageConfirmationId(id); setError(""); setConfirmation(null);
    try { await integrationManagementApi.confirmPackageInstall(id); setPackageConfirmationId(""); await reloadList(); const result = await integrationManagementApi.get(id); setSelected(result); setConnections(result.connections ?? []); }
    catch (caught) { const pending = installConfirmationFromError(caught); if (pending) setConfirmation(pending); else { setPackageConfirmationId(""); setError(caught instanceof Error ? caught.message : "Package confirmation could not be started."); } }
    finally { setConfirmingPackageId(""); }
  }
  async function confirmPackage(value: IntegrationInstallConfirmation) {
    if (!packageConfirmationId) return;
    setBusy("confirm-package"); setError("");
    try { await integrationManagementApi.confirmPackageInstall(packageConfirmationId, value); setPackageConfirmationId(""); setConfirmation(null); await reloadList(); const result = await integrationManagementApi.get(packageConfirmationId); setSelected(result); setConnections(result.connections ?? []); setNotice("Native package confirmed. Review its capabilities before enabling it."); }
    catch (caught) { const pending = installConfirmationFromError(caught); if (pending) setConfirmation(pending); else setError(caught instanceof Error ? caught.message : "Native package could not be confirmed."); }
    finally { setBusy(""); }
  }

  async function exportIntegration() {
    if (!selected) return;
    try { const file = await integrationManagementApi.exportFile(selected.integration.id); downloadText(`${selected.integration.id}.yaml`, file); addToast({ tone: "success", message: "Integration file exported." }); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Integration file could not be exported."); }
  }
  async function removeIntegration() {
    if (!selected || !removal || removeTypedId !== selected.integration.id) return;
    setBusy("remove"); setError("");
    try {
      const result = await integrationManagementApi.remove(selected.integration.id, {
        confirm_name: removeTypedId,
        disable_using_lines: disableUsingLines,
        delete_exclusive_credentials: deleteExclusiveCredentials,
      });
      setRemoveDialogOpen(false); setRemoval(null); setSelected(null);
      navigate("/integrations"); await reloadList();
      addToast({ tone: "success", message: result.listed_package_note || "Integration removed. You can install it again from the Catalogue." });
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Integration could not be removed."); }
    finally { setBusy(""); }
  }

  async function openRemovalPreview() {
    if (!selected) return;
    setError(""); setRemoveTypedId(""); setDisableUsingLines(false); setDeleteExclusiveCredentials(false);
    try { setRemoval(await integrationManagementApi.removalPreview(selected.integration.id)); setRemoveDialogOpen(true); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Removal preview could not be loaded."); }
  }

  function badgeForSource(item: IntegrationManagementSummary) {
    const drift = item.provenance?.status === "catalogue_drift";
    const tone = item.provenance?.kind === "catalogue" ? item.provenance.signing_key_id?.startsWith("dev:") ? "warning" : "success" : item.provenance?.kind === "bundled" ? "info" : "warning";
    return <span className={styles.badges}><Badge tone={tone}>{sourceLabel(item)}</Badge>{drift ? <Badge tone="warning">Drift · {item.provenance?.drift_reason || "Review required"}</Badge> : null}</span>;
  }

  const columns = [
    { key: "name", header: "Name", render: (item: IntegrationManagementSummary) => <span className={styles.nameCell}><strong>{item.integration.name}</strong><small>{item.integration.id}</small></span> },
    { key: "kind", header: "Kind", render: (item: IntegrationManagementSummary) => <Badge tone="info">{kindFor(item)}</Badge> },
    { key: "source", header: "Source", render: (item: IntegrationManagementSummary) => badgeForSource(item) },
    { key: "status", header: "Status", render: (item: IntegrationManagementSummary) => <StatusBadge status={friendlyStatus(item) === "Needs attention" ? "degraded" : item.integration.enabled ? "enabled" : "disabled"}>{friendlyStatus(item)}</StatusBadge> },
    { key: "runner", header: "Runner health", render: (item: IntegrationManagementSummary) => item.availability.execution_plane === "container" ? <span className={styles.runnerCell}><StatusBadge status={item.availability.runner_status === "connected" ? "healthy" : item.availability.runner_status === "stale" ? "degraded" : "not_connected"}>{item.availability.runner_status === "connected" ? "Connected" : item.availability.runner_status === "stale" ? "Stale" : "Not connected"}</StatusBadge>{item.availability.runner_last_seen_at ? <small title={new Date(item.availability.runner_last_seen_at).toLocaleString()}>Runner last seen {relativeTime(item.availability.runner_last_seen_at)}</small> : null}</span> : "—" },
    { key: "operations", header: "Operations", render: (item: IntegrationManagementSummary) => operationsLoading ? "Loading…" : operationsByIntegration[item.integration.id]?.length ?? 0 },
  ];

  return <div className={`${styles.page} page-stack integrations-page`}>
    {!integrationId ? <>
      <PageHeader title="Integrations" description="Connect tools and services to your production lines." primaryAction={<Button icon={<Plug size={16} />} onClick={() => setAddFlow("chooser")}>Add integration</Button>} />
      <div className={styles.filters} aria-label="Integration filters">
        <SearchInput value={search} onChange={setSearch} placeholder="Search integrations" />
        <FormField label="Kind" htmlFor="integration-kind-filter"><Select id="integration-kind-filter" value={kindFilter} onChange={(event) => setKindFilter(event.target.value)}><option>All kinds</option><option>Native</option><option>OpenAPI</option><option>MCP</option><option>Webhook</option><option>Container</option></Select></FormField>
        <FormField label="Status" htmlFor="integration-status-filter"><Select id="integration-status-filter" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option>All statuses</option><option>Enabled</option><option>Disabled</option><option>Needs attention</option></Select></FormField>
        <FormField label="Source" htmlFor="integration-source-filter"><Select id="integration-source-filter" value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}><option>All sources</option><option>Catalogue</option><option>Outside</option><option>Bundled</option></Select></FormField>
      </div>
      {error ? <p className="form-error" role="alert">{error}</p> : null}{notice ? <p className="form-success" role="status">{notice}</p> : null}
      {!loading && !items.length ? <EmptyState title="No integrations yet" description="Add an integration to connect a service to your lines." icon={<Plug size={22} />} action={<Button onClick={() => setAddFlow("chooser")}>Add integration</Button>} /> : null}
      {!loading && items.length > 0 && !filteredItems.length ? <EmptyState title="No integrations match" description="Try changing the search or filters." icon={<CircleHelp size={22} />} /> : null}
      {loading || filteredItems.length > 0 ? <Table<IntegrationManagementSummary> label="Integrations" columns={columns} rows={filteredItems} loading={loading} getRowKey={(item) => item.integration.id} onRowClick={(item) => navigate(`/integrations/${encodeURIComponent(item.integration.id)}`)} empty="No integrations match these filters." /> : null}
    </> : <>
      <div className={styles.backLink}><Button variant="ghost" size="sm" icon={<ArrowLeft size={16} />} onClick={() => navigate("/integrations")}>All integrations</Button></div>
      {error ? <p className="form-error" role="alert">{error}</p> : null}{notice ? <p className="form-success" role="status">{notice}</p> : null}
      {!selected && !error ? <p role="status">Loading integration…</p> : null}
      {selected ? <>
        <PageHeader title={selected.integration.name} description={`${kindFor(selected)} · ${sourceLabel(selected)} · ${friendlyStatus(selected)}`} secondaryAction={<div className={styles.detailActions}>
          <Button variant="secondary" disabled={Boolean(busy) || (selected.integration.enabled ? false : selected.provenance?.status === "awaiting_confirmation" || (installPolicy === "catalogue-only" && selected.provenance?.kind === "outside"))} onClick={() => void mutate(selected.integration.enabled ? "disable" : "enable", () => selected.integration.enabled ? integrationManagementApi.disable(integrationId) : integrationManagementApi.enable(integrationId), selected.integration.enabled ? "Integration disabled." : "Integration enabled.")}>{selected.integration.enabled ? "Disable" : selected.provenance?.status === "awaiting_confirmation" ? "Awaiting confirmation" : "Enable"}</Button>
          <details className={styles.moreMenu}><summary>More <ChevronDown size={14} /></summary><div role="menu">
            {selected.availability.declarative ? <Button variant="ghost" type="button" role="menuitem" onClick={() => void exportIntegration()}>Export file</Button> : null}
            <Button variant="ghost" type="button" role="menuitem" disabled={Boolean(busy) || runtimeUnavailable} onClick={() => void mutate("reconcile", () => integrationManagementApi.reconcile(integrationId), "Integration reconciliation completed.")}>Reconcile</Button>
            {sourceFor(selected) !== "Bundled" ? <Button variant="ghost" type="button" role="menuitem" className={styles.dangerAction} onClick={() => void openRemovalPreview()}>Remove</Button> : null}
          </div></details>
        </div>} />
        {selected.provenance?.status === "awaiting_confirmation" ? <Card className={styles.confirmationCard}><strong>Package confirmation required</strong><p>This native package stays disabled until an administrator reviews its install summary.</p><Button disabled={Boolean(busy) || Boolean(confirmingPackageId)} onClick={() => void beginPackageConfirmation(integrationId)}>{confirmingPackageId ? "Preparing summary…" : "Review and confirm package"}</Button></Card> : null}
        <div className={styles.detailLayout}>
          <Tabs<SectionKey> label="Integration sections" tabs={availableSections.map((key) => ({ key, label: key }))} active={section} onChange={(next) => setSection(next)} className={styles.detailTabs} />
          <div className={styles.sectionContent}>
            {section === "Overview" ? <>
              <PageHeader title="Overview" description="Source, access, health, and attention for this integration." />
              <KeyValue items={[
                { label: "Kind", value: kindFor(selected) }, { label: "Status", value: <StatusBadge status={friendlyStatus(selected) === "Needs attention" ? "degraded" : integrationEnabled ? "enabled" : "disabled"}>{friendlyStatus(selected)}</StatusBadge> },
                { label: "Source", value: <Badge tone={selected.provenance?.kind === "catalogue" ? selected.provenance.signing_key_id?.startsWith("dev:") ? "warning" : "success" : selected.provenance?.kind === "bundled" ? "info" : "warning"}>{sourceLabel(selected)}</Badge> },
                { label: "Health", value: <StatusBadge status={String(selected.health.status ?? selected.integration.status ?? "unknown")} /> },
                { label: "Connections", value: selected.activity.connection_count }, { label: "Resources", value: selected.activity.resource_count }, { label: "Bindings", value: selected.activity.binding_count },
              ]} />
              {sourceFor(selected) === "Bundled" ? <p className={styles.muted}>Bundled integrations are part of Lynewright and cannot be removed. You can disable them here.</p> : null}
              {selected.attention.length ? <section className={styles.attention} aria-label="Attention items"><h2>Needs attention</h2><ul>{selected.attention.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul></section> : <p className={styles.muted}>No attention items.</p>}
              <AuthorizationSection integrationId={integrationId} requested={selected.integration.requested_capabilities ?? selected.capabilities.requested} permissionMode={selected.integration.permission_mode ?? selected.capabilities.permission_mode} granted={selected.integration.granted_capabilities ?? selected.capabilities.granted} manual={manual} integrationEnabled={integrationEnabled} busy={Boolean(busy)} mutate={mutate} />
              <details className={styles.advanced}><summary>Advanced</summary><KeyValue items={[{ label: "Integration id", value: <CopyField label="Integration id" value={selected.integration.id} /> }, { label: "Version", value: selected.integration.version }, { label: "Execution plane", value: selected.availability.execution_plane ?? "direct" }]} /><h3>Provenance details</h3><IntegrationProvenanceDetails provenance={selected.provenance} installPolicy={installPolicy} /></details>
            </> : null}
            {section === "Connections" ? <>
              <PageHeader title="Connections" description="Choose project-scoped endpoints and stored credentials." />
              <KeyValue items={connections.map((item) => ({ label: item.name, value: <StatusBadge status={item.enabled ? "enabled" : "disabled"}>{item.enabled ? "Enabled" : "Disabled"}</StatusBadge> }))} />
              <details className={styles.advanced}><summary>Connection setup and advanced details</summary>
                <ConnectionManagementSection integrationId={integrationId} connections={connections} selectedConnectionId={connectionId} integrationEnabled={integrationEnabled} oauthSupported={selected.oauth.supported} busy={Boolean(busy)} credentialOptions={credentials} mutate={mutate} onSelect={setConnectionId} onDiscover={(id) => { void mutate(`discover:${id}`, () => integrationManagementApi.discover(integrationId, id), "Connection discovery completed."); }} />
                <AuthorizationSection integrationId={integrationId} requested={selected.integration.requested_capabilities ?? selected.capabilities.requested} permissionMode={selected.integration.permission_mode ?? selected.capabilities.permission_mode} granted={selected.integration.granted_capabilities ?? selected.capabilities.granted} manual={manual} integrationEnabled={integrationEnabled} busy={Boolean(busy)} mutate={mutate} />
                {!manual ? <DefinitionUpgradeSection integrationId={integrationId} runtimeUnavailable={runtimeUnavailable} busy={Boolean(busy)} mutate={mutate} /> : null}
              </details>
            </> : null}
            {section === "Operations" ? <><PageHeader title="Operations" description="Operations that this integration makes available to project lines." /><GenericOperationsSection projectId={projectId} operations={operationsByIntegration[integrationId] ?? []} busy={Boolean(busy)} mutate={mutate} /></> : null}
            {section === "Settings" ? <><PageHeader title="Settings" description="Settings declared by this integration." />{settings ? <ProductionLineSettings integrationId={integrationId} settings={settings} credentialOptions={credentials} enabled={integrationEnabled} busy={Boolean(busy)} onSave={(values) => void mutate("settings", () => integrationManagementApi.updateSettings(integrationId, values), "Integration settings saved.")} /> : settingsUnavailable ? <EmptyState title="No declared settings" description="This integration does not declare operator settings." icon={<Settings2 size={22} />} /> : <p role="status">Loading declared settings…</p>}</> : null}
            {section === "Runner" ? <><PageHeader title="Runner" description="Where this integration’s code runs." /><IntegrationRunnerSection integration={selected} /></> : null}
            {section === "Health & diagnostics" ? <><PageHeader title="Health & diagnostics" description="Check availability and review integration diagnostics." /><HealthDiagnosticsSection integrationId={integrationId} fallback={selected.health} enabled={integrationEnabled} manual={manual} /></> : null}
            {section === "MCP tools" ? <><PageHeader title="MCP tools" description="Review discovered tools and their operation mappings." />{connectionId ? <details open className={styles.advanced}><summary>Advanced · tool approvals and mappings</summary><McpContractMappingsSection integrationId={integrationId} connectionId={connectionId} discoveredTools={selectedConnection?.discovered_tools ?? []} enabled={integrationEnabled} connectionEnabled={Boolean(selectedConnection?.enabled)} busy={Boolean(busy)} mutate={mutate} reportError={setError} /><McpResourceMappingsSection integrationId={integrationId} connectionId={connectionId} integrationEnabled={integrationEnabled} discoveredTools={selectedConnection?.discovered_tools ?? []} busy={Boolean(busy)} mutate={mutate} reportError={setError} /></details> : <EmptyState title="Add a connection first" description="Add an MCP connection to discover tools." icon={<Plug size={22} />} action={<Button variant="secondary" onClick={() => setSection("Connections")}>Open connections</Button>} />}</> : null}
            {section === "OpenAPI mappings" ? <><PageHeader title="OpenAPI mappings" description="Approve operations and map them to line contracts." />{connectionId ? <details open className={styles.advanced}><summary>Advanced · operation approvals and mappings</summary><OpenApiManagementSection integration={selected} connectionId={connectionId} busy={Boolean(busy)} mutate={mutate} /></details> : <EmptyState title="Add a connection first" description="Add an OpenAPI connection to load its operations." icon={<Plug size={22} />} action={<Button variant="secondary" onClick={() => setSection("Connections")}>Open connections</Button>} />}</> : null}
            {section === "Resources" ? <><PageHeader title="Resources" description="Resources discovered through this integration." /><details open className={styles.advanced}><summary>Resource inventory</summary><ResourcesSection integration={selected} connection={selectedConnection} busy={Boolean(busy)} mutate={mutate} /></details></> : null}
            {section === "Bindings" ? <><PageHeader title="Bindings" description="Resource links used by production lines." /><details open className={styles.advanced}><summary>Bindings and repair</summary><BindingsSection integrationId={integrationId} refreshToken={selected} busy={Boolean(busy)} mutate={mutate} /></details></> : null}
            {section === "Setup" ? <><PageHeader title="Setup" description="Requirements attached to configured resources." /><details open className={styles.advanced}><summary>Advanced · setup requirements</summary><SetupRequirementsSection integrationId={integrationId} summary={selected.setup_requirements} runtime={String(selected.integration.type)} busy={Boolean(busy)} mutate={mutate} /></details></> : null}
            {section === "Webhooks" ? <><PageHeader title="Webhooks" description="Inbound endpoints declared by this integration." /><details open className={styles.advanced}><summary>Advanced · webhook endpoints</summary><WebhooksSection integrationId={integrationId} /></details></> : null}
          </div>
        </div>
      </> : null}
    </>}

    <Dialog open={addFlow !== null} title={addFlow === "chooser" ? "Add integration" : addFlow === "file" ? "Import integration file" : addFlow === "draft" ? "From link or docs" : "Manual integration"} description={addFlow === "chooser" ? "Choose how you want to connect a service." : undefined} onClose={() => setAddFlow(null)} className={styles.addDialog}>
      {error && addFlow ? <p className="form-error" role="alert">{error}</p> : null}
      {addFlow === "chooser" ? <div className={styles.chooser}>
        <Button variant="secondary" type="button" onClick={() => { setAddFlow(null); navigate("/catalogue"); }}><Package size={20} /><span><strong>From catalogue</strong><small>Choose a reviewed integration from the signed catalogue.</small></span></Button>
        <Button variant="secondary" type="button" onClick={() => setAddFlow("file")}><FileUp size={20} /><span><strong>Import file</strong><small>Validate a declarative integration file. Outside installs require typed confirmation.</small></span></Button>
        <Button variant="secondary" type="button" onClick={() => setAddFlow("draft")}><Link2 size={20} /><span><strong>From link or docs</strong><small>Create a draft, review its operations, probe one, then enable.</small></span></Button>
        <Button variant="secondary" type="button" onClick={() => setAddFlow("manual")}><Boxes size={20} /><span><strong>Manual</strong><small>Set up a data-only MCP, OpenAPI, or webhook integration.</small></span></Button>
      </div> : null}
      {addFlow === "file" ? <IntegrationFileSection selected={null} embedded onClose={() => setAddFlow(null)} onImported={(item) => void onCreated(item)} /> : null}
      {addFlow === "draft" ? <div className={styles.flowBody}><p className={styles.flowSafety}>A live probe is required before enable. Integration secrets are rejected; use stored credential references.</p><IntegrationDraftSection embedded onClose={() => setAddFlow(null)} onEnabled={(id) => { void reloadList().then(() => { setAddFlow(null); navigate(`/integrations/${encodeURIComponent(id)}`); }); }} /></div> : null}
      {addFlow === "manual" ? <ManualIntegrationSection onCreated={(item) => void onCreated(item)} onClose={() => setAddFlow(null)} reportError={setError} /> : null}
      {addFlow !== "file" && addFlow !== "manual" && addFlow !== "draft" ? <div className={styles.modalFooter}><Button variant="secondary" onClick={() => setAddFlow(null)}>Cancel</Button></div> : null}
    </Dialog>
    {confirmation ? <InstallConfirmationDialog summary={confirmation.summary} summaryHash={confirmation.summaryHash} busy={busy === "confirm-package"} onConfirm={(value) => void confirmPackage(value)} onCancel={() => { setConfirmation(null); setPackageConfirmationId(""); }} /> : null}
    <Dialog open={removeDialogOpen && Boolean(selected && removal)} title={`Remove ${selected?.integration.name ?? "integration"}?`} description="This removes the integration from Lynewright. Bundled integrations can only be disabled." onClose={() => { setRemoveDialogOpen(false); setRemoval(null); }} className={styles.addDialog}>
      {removal ? <div className={styles.flowBody}>
        {removal.blocked_reasons.length ? <div className="form-error" role="alert"><strong>Removal is blocked</strong><ul>{removal.blocked_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></div> : null}
        {removal.lines.length ? <section><h3>Lines that use this integration</h3><ul>{removal.lines.map((line) => <li key={line.id}><strong>{line.name}</strong> · {line.operations.join(", ")}</li>)}</ul><Checkbox label="Disable these lines, then remove the integration" checked={disableUsingLines} onChange={(event) => setDisableUsingLines(event.target.checked)} /></section> : <p>No production lines use these operations.</p>}
        <section><h3>Will be removed</h3><ul>
          <li>{removal.connections.length} connection{removal.connections.length === 1 ? "" : "s"}{removal.connections.length ? `: ${removal.connections.map((item) => item.name).join(", ")}` : ""}</li>
          <li>Mappings: {Object.entries(removal.mappings).map(([table, count]) => `${table.replaceAll("_", " ")} (${count})`).join(", ") || "none"}</li>
          <li>{removal.runner_tokens} active runner token{removal.runner_tokens === 1 ? "" : "s"}</li>
          <li>{removal.stored_wheel ? `Stored package wheel ${removal.stored_wheel.filename}` : "No stored package wheel"}</li>
          <li>{removal.staging_files.length} package staging item{removal.staging_files.length === 1 ? "" : "s"}</li>
          <li>{removal.media.length} owned media file{removal.media.length === 1 ? "" : "s"}{removal.media.length ? `: ${removal.media.map((item) => item.name).join(", ")}` : ""}</li>
          <li>{removal.settings_fields?.length ? `Settings: ${removal.settings_fields.join(", ")}` : removal.settings ? "Integration settings" : "No integration settings"}</li>
        </ul></section>
        {removal.credentials.used.length ? <section><h3>Credentials</h3><p>Credentials are kept by default because they may be shared.</p><p>Used: {removal.credentials.used.join(", ")}</p>{removal.credentials.exclusive.length ? <Checkbox label={`Also delete credentials used only by this integration: ${removal.credentials.exclusive.join(", ")}`} checked={deleteExclusiveCredentials} onChange={(event) => setDeleteExclusiveCredentials(event.target.checked)} /> : <p>No exclusive credentials can be deleted.</p>}</section> : <p>No stored credentials are used by this integration.</p>}
        {removal.runner_instruction ? <p className="muted-text">{removal.runner_instruction}</p> : null}
        {removal.listed_package ? <p className="form-error">This package is listed in LYNEWRIGHT_INTEGRATION_PACKAGES. Remove {removal.distribution} from that setting too, or it will return after the next restart.</p> : null}
        <CopyField label="Integration ID" value={removal.integration_id} />
        <FormField label="Type the integration ID to confirm" htmlFor="integration-remove-confirm" required><Input id="integration-remove-confirm" autoComplete="off" value={removeTypedId} onChange={(event) => setRemoveTypedId(event.target.value)} /></FormField>
        <footer className={styles.modalFooter}><Button variant="secondary" onClick={() => { setRemoveDialogOpen(false); setRemoval(null); }}>Cancel</Button><Button variant="danger" busy={busy === "remove"} disabled={removal.bundled || Boolean(removal.disable_lines_blocked_reasons.length) || removeTypedId !== removal.integration_id || Boolean(removal.lines.length && !disableUsingLines)} onClick={() => void removeIntegration()}>Remove integration</Button></footer>
      </div> : <p role="status">Loading removal preview…</p>}
    </Dialog>
  </div>;
}
