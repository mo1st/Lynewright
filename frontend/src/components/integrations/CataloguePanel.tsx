import { useCallback, useEffect, useMemo, useState } from "react";
import { Archive, ExternalLink, Info, Search, ShieldAlert } from "lucide-react";
import { catalogueApi, type CatalogueEntry, type CatalogueResponse } from "../../api/catalogueClient";
import { integrationManagementApi } from "../../api/integrationManagementClient";
import type { IntegrationManagementSummary } from "../../types";
import { Badge, Button, CopyField, Dialog, EmptyState, FormField, Input, PageHeader, Select, Switch, useToast } from "../ui";
import styles from "../ui/design-system.module.css";

const DOCS = "https://github.com/lynewright/lynewright/blob/main/docs/integrations/catalogue.md";
type KindFilter = "all" | CatalogueEntry["kind"];

function titleFor(entry: CatalogueEntry) {
  if (entry.name?.trim()) return entry.name.trim();
  const parts = entry.id.split(".");
  const last = parts[parts.length - 1] ?? entry.id;
  return last.replace(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function cleanSetupText(value: string) {
  return value.replace(/LYNEWRIGHT_[A-Z0-9_<>]+/g, "the configured endpoint reference");
}

function relativeTime(value: string | null) {
  if (!value) return "not checked yet";
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const abs = Math.abs(seconds);
  const unit: Intl.RelativeTimeFormatUnit = abs < 60 ? "second" : abs < 3600 ? "minute" : abs < 86400 ? "hour" : "day";
  const amount = unit === "second" ? seconds : unit === "minute" ? Math.round(seconds / 60) : unit === "hour" ? Math.round(seconds / 3600) : Math.round(seconds / 86400);
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(amount, unit);
}

function provenanceLabel(item: IntegrationManagementSummary | undefined, entry: CatalogueEntry) {
  const provenance = item?.provenance;
  if (!provenance) return entry.provenance_kind === "catalogue" ? "Catalogue ✓" : entry.provenance_kind === "bundled" ? "Bundled" : "Outside ⚠";
  if (provenance.kind === "bundled") return "Bundled";
  if (provenance.kind === "catalogue") {
    return provenance.signing_key_id?.startsWith("dev:") ? "Catalogue (dev key)" : "Catalogue ✓";
  }
  return "Outside ⚠";
}

function statusBadge(response: CatalogueResponse) {
  const status = response.status;
  if (status.status === "untrusted" || (!status.trusted_key_ids.length && !status.dev_key_trusted)) return { text: "Untrusted", tone: "danger" as const, key: "no trusted catalogue key" };
  if (status.dev_key_trusted || status.verified_key_id?.startsWith("dev:")) return { text: "Development key", tone: "warning" as const, key: "development key" };
  if (status.status === "ready" && status.verified_key_id) {
    const key = status.trusted_key_ids.find((item) => item.id === status.verified_key_id);
    return { text: "Trusted", tone: "success" as const, key: key?.source ?? "trusted key" };
  }
  return { text: status.status === "offline" ? "Offline" : status.status === "empty" ? "Not checked" : "Unavailable", tone: "neutral" as const, key: "catalogue status" };
}

function advanced(entry: CatalogueEntry) {
  return <details className={styles.directoryAdvanced}>
    <summary>Advanced</summary>
    <div className={styles.directoryAdvancedFields}>
      <CopyField label="Entry ID" value={entry.id} />
      <CopyField label={entry.kind === "container" ? "Image digest" : "SHA-256"} value={entry.sha256 ?? (entry.source.includes("@") ? entry.source.slice(entry.source.lastIndexOf("@") + 1) : "Not provided")} />
      <CopyField label="Source URL" value={entry.source} />
    </div>
  </details>;
}

function EntryDetails({ entry, integration, onClose }: { entry: CatalogueEntry; integration?: IntegrationManagementSummary; onClose: () => void }) {
  const reviewDate = entry.review.date;
  const requirements = entry.requires_operator_setup ?? [];
  return <Dialog open title={titleFor(entry)} description={`${entry.kind} integration · version ${entry.version}`} onClose={onClose}>
    <div className={styles.directoryDetail}>
      <p className={styles.directoryDescription}>{entry.description || "No description provided by the catalogue."}</p>
      <div className={styles.directoryBadges}><Badge>{entry.kind}</Badge><Badge>{entry.licence}</Badge><Badge>{entry.permissions.egress === "none" ? "No network" : "Internet"}</Badge></div>
      <section><h3>Permissions requested</h3><p>Capabilities: {entry.permissions.capabilities.length ? entry.permissions.capabilities.join(", ") : "None"}</p><p>Credential references: {entry.permissions.credential_refs.length ? entry.permissions.credential_refs.join(", ") : "None"}</p><p>Media: {entry.permissions.media}</p></section>
      {entry.limits ? <section><h3>Limits</h3><p>{entry.limits.cpus} CPU · {entry.limits.memory_mb} MB memory · {entry.limits.pids} processes · {entry.limits.timeout_seconds}s timeout</p></section> : null}
      <section><h3>Review record</h3><p>Reviewed by {entry.review.reviewer} on {reviewDate}</p>{entry.review.notes_url ? <a href={entry.review.notes_url} target="_blank" rel="noreferrer">Review notes <ExternalLink size={13} aria-hidden="true" /></a> : null}</section>
      {requirements.length ? <section><h3>Setup steps</h3><ul>{requirements.map((note) => <li key={note}>{cleanSetupText(note)}</li>)}</ul></section> : null}
      {entry.installed && (integration?.provenance?.status === "catalogue_drift" || entry.provenance_status === "catalogue_drift") ? <p className={styles.directoryDriftReason}><ShieldAlert size={16} />{integration?.provenance?.drift_reason || "The installed artifact no longer matches the signed catalogue entry."}</p> : null}
      {advanced(entry)}
    </div>
  </Dialog>;
}

export default function CataloguePanel() {
  const [response, setResponse] = useState<CatalogueResponse | null>(null);
  const [integrations, setIntegrations] = useState<IntegrationManagementSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [selectedEntry, setSelectedEntry] = useState<CatalogueEntry | null>(null);
  const [installEntry, setInstallEntry] = useState<CatalogueEntry | null>(null);
  const [repairEntry, setRepairEntry] = useState<CatalogueEntry | null>(null);
  const [endpointRef, setEndpointRef] = useState("");
  const [installing, setInstalling] = useState(false);
  const [repairing, setRepairing] = useState(false);
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState<KindFilter>("all");
  const [installedOnly, setInstalledOnly] = useState(false);
  const [error, setError] = useState("");
  const { addToast } = useToast();

  const load = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    setError("");
    const [catalogueResult, integrationResult] = await Promise.allSettled([catalogueApi.get(), integrationManagementApi.list()]);
    if (catalogueResult.status === "fulfilled") setResponse(catalogueResult.value);
    else setError(catalogueResult.reason instanceof Error ? catalogueResult.reason.message : "Catalogue could not be loaded.");
    if (integrationResult.status === "fulfilled") setIntegrations(integrationResult.value.items);
    if (showLoading) setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);

  const entries = useMemo(() => (response?.entries ?? []).filter((entry) => !entry.yanked), [response]);
  const filtered = useMemo(() => entries.filter((entry) => {
    const matchesKind = kind === "all" || entry.kind === kind;
    const matchesInstalled = !installedOnly || Boolean(entry.installed);
    const query = search.trim().toLocaleLowerCase();
    const matchesSearch = !query || [titleFor(entry), entry.summary ?? "", entry.id, entry.kind].join(" ").toLocaleLowerCase().includes(query);
    return matchesKind && matchesInstalled && matchesSearch;
  }), [entries, installedOnly, kind, search]);
  const trust = response ? statusBadge(response) : null;
  const noKeys = Boolean(response && (response.status.status === "untrusted" || (!response.status.trusted_key_ids.length && !response.status.dev_key_trusted)));

  async function refresh() {
    setRefreshing(true);
    setError("");
    try {
      setResponse(await catalogueApi.refresh());
      const managed = await integrationManagementApi.list().catch(() => null);
      if (managed) setIntegrations(managed.items);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Catalogue refresh failed.");
    } finally { setRefreshing(false); }
  }

  async function install() {
    if (!installEntry) return;
    setInstalling(true);
    try {
      await catalogueApi.install(installEntry.id, installEntry.version, endpointRef.trim());
      const display = titleFor(installEntry);
      setInstallEntry(null);
      setEndpointRef("");
      await load(false);
      addToast({ tone: "success", message: <span><strong>{display} installed.</strong> <a href={`/integrations?integration_id=${encodeURIComponent(installEntry.id)}`}>Open integration</a>{installEntry.kind === "package" ? <> · <a href="/integrations?panel=Setup">Set up runner</a></> : null}</span> });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Catalogue installation failed.");
    } finally { setInstalling(false); }
  }

  async function repair() {
    if (!repairEntry) return;
    setRepairing(true);
    try {
      await catalogueApi.repair(repairEntry.id, repairEntry.version);
      const display = titleFor(repairEntry);
      setRepairEntry(null);
      await load(false);
      addToast({ tone: "success", message: <span><strong>{display} installation repaired.</strong> <a href={`/integrations?integration_id=${encodeURIComponent(repairEntry.id)}`}>Open integration</a></span> });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Catalogue package repair failed.");
    } finally { setRepairing(false); }
  }

  const visibleEmptyText = !entries.length ? "There are no entries in this signed catalogue yet." : "No entries match these filters.";
  return <div className={styles.dashboardPage}>
    <PageHeader title="Catalogue" description="Reviewed integrations you can install. Every file is checked against a signed list." secondaryAction={<Button variant="secondary" disabled={loading || refreshing || response?.status.status === "offline"} onClick={() => void refresh()}>{refreshing ? "Refreshing…" : "Refresh"}</Button>} />
    {error ? <p role="alert" className={styles.directoryError}>{error}</p> : null}
    {response && trust ? <div className={styles.directoryTrustLine}>
      <span>Signed by {trust.key} · serial {response.status.serial ?? "—"} · checked {relativeTime(response.status.fetched_at)}</span>
      <Badge tone={trust.tone}>{trust.text}</Badge>
      <span className={styles.directoryInfo} title="Configure catalogue trust keys in the security documentation."><Info size={14} aria-hidden="true" /><span className="sr-only">Catalogue trust configuration is documented in Security.</span></span>
    </div> : null}
    {noKeys ? <div className={styles.directoryUntrusted} role="status"><ShieldAlert size={18} /><p>No trusted signing key is configured, so entries cannot be verified or installed from a catalogue. Add an operator key to enable signed catalogue installs.</p><a href={DOCS}>How to add a key</a></div> : null}
    {!noKeys ? <>
      <div className={styles.directoryFilters}>
        <FormField label="Search catalogue" htmlFor="catalogue-search" className={styles.directorySearch}>
          <div className={styles.directorySearchControl}><Search size={16} aria-hidden="true" /><Input id="catalogue-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search integrations" /></div>
        </FormField>
        <FormField label="Kind" htmlFor="catalogue-kind" className={styles.directoryKindFilter}><Select id="catalogue-kind" value={kind} onChange={(event) => setKind(event.target.value as KindFilter)}><option value="all">All</option><option value="file">File</option><option value="package">Package</option><option value="container">Container</option></Select></FormField>
        <Switch label="Installed" className={styles.directoryInstalledToggle} checked={installedOnly} onChange={setInstalledOnly} />
      </div>
      {loading ? <p role="status">Loading catalogue…</p> : filtered.length ? <div className={styles.directoryGrid} aria-label="Catalogue entries">
        {filtered.map((entry) => {
          const managed = integrations.find((item) => item.provenance?.catalogue_entry_id === entry.id || item.integration.id === entry.id);
          const drifted = entry.provenance_status === "catalogue_drift" || managed?.provenance?.status === "catalogue_drift";
          const installable = !entry.installed;
          return <article className={styles.directoryCard} key={`${entry.id}@${entry.version}`}>
            <Button variant="ghost" type="button" className={`${styles.directoryCardOpen} signed-list-card-trigger ui-button`} onClick={() => setSelectedEntry(entry)}>
              <span className={styles.directoryCardHeading}><strong>{titleFor(entry)}</strong><span>v{entry.version}</span></span>
              {entry.summary ? <span className={styles.directorySummary}>{entry.summary}</span> : null}
              <span className={styles.directoryBadges}><Badge>{entry.kind}</Badge><Badge>{entry.licence}</Badge><Badge>{entry.permissions.egress === "none" ? "No network" : "Internet"}</Badge></span>
              <span className={styles.directoryReviewed}>Reviewed by {entry.review.reviewer} on {entry.review.date}</span>
              {entry.limits ? <span className={styles.directoryLimits}>{entry.limits.cpus} CPU · {entry.limits.memory_mb} MB · {entry.limits.pids} processes</span> : null}
            </Button>
            {entry.requires_operator_setup?.length ? <details className={styles.directorySetupNote}><summary>Setup required · Show steps</summary><ul>{entry.requires_operator_setup.map((note) => <li key={note}>{cleanSetupText(note)}</li>)}</ul></details> : null}
            {entry.installed ? <div className={styles.directoryInstalledState}><Badge tone={entry.installation_incomplete ? "danger" : "success"}>{entry.installation_incomplete ? "Installation incomplete" : "Installed ✓"}</Badge><Badge tone={managed?.provenance?.kind === "outside" || entry.provenance_kind === "outside" ? "warning" : "neutral"}>{provenanceLabel(managed, entry)}</Badge>{drifted ? <Badge tone="warning">Drift · {managed?.provenance?.drift_reason ?? "Catalogue entry changed"}</Badge> : null}{entry.installation_incomplete ? <Button variant="secondary" onClick={() => setRepairEntry(entry)}>Repair</Button> : null}</div> : <Button onClick={() => { setInstallEntry(entry); setEndpointRef(""); }} disabled={!installable}>Install</Button>}
            {advanced(entry)}
          </article>;
        })}
      </div> : <EmptyState title={entries.length ? "No results" : "No catalogue entries"} description={visibleEmptyText} icon={entries.length ? <Search size={20} /> : <Archive size={20} />} />}
    </> : null}
    {selectedEntry ? <EntryDetails entry={selectedEntry} integration={integrations.find((item) => item.provenance?.catalogue_entry_id === selectedEntry.id || item.integration.id === selectedEntry.id)} onClose={() => setSelectedEntry(null)} /> : null}
    {installEntry ? <Dialog open title={`Install ${titleFor(installEntry)}?`} description={`${installEntry.kind === "file" ? "No code, data only" : "Runs in its own container"} · ${installEntry.permissions.egress === "none" ? "No network access" : "Internet access"}`} onClose={() => setInstallEntry(null)} footer={<><Button variant="secondary" onClick={() => setInstallEntry(null)} disabled={installing}>Cancel</Button><Button onClick={() => void install()} busy={installing}>Install</Button></>}>
      <div className={styles.directoryInstallDetails}>
        <p>{installEntry.kind === "file" ? "This declarative file is validated before it is added." : "This package will run in an isolated container."}</p>
        <p><strong>Egress:</strong> {installEntry.permissions.egress === "none" ? "No network" : "Internet"}</p>
        <p><strong>Capabilities:</strong> {installEntry.permissions.capabilities.length ? installEntry.permissions.capabilities.join(", ") : "None"}</p>
        <p><strong>Credential references:</strong> {installEntry.permissions.credential_refs.length ? installEntry.permissions.credential_refs.join(", ") : "None"}</p>
        <p><strong>Media access:</strong> {installEntry.permissions.media}</p>
        {installEntry.requires_operator_setup?.length ? <>
          <div className={styles.directorySetupCallout}><strong>Setup required</strong><ul>{installEntry.requires_operator_setup.map((note) => <li key={note}>{cleanSetupText(note)}</li>)}</ul></div>
          <FormField label="Internal endpoint reference" htmlFor="catalogue-endpoint-ref" helpText="Use the endpoint reference configured by the operator."><Input id="catalogue-endpoint-ref" value={endpointRef} onChange={(event) => setEndpointRef(event.target.value)} maxLength={64} autoComplete="off" /></FormField>
        </> : null}
      </div>
    </Dialog> : null}
    {repairEntry ? <Dialog open title={`Repair ${titleFor(repairEntry)}?`} description="The verified wheel and runner build file will be downloaded again and restored to this package's managed directory." onClose={() => { if (!repairing) setRepairEntry(null); }} footer={<><Button variant="secondary" onClick={() => setRepairEntry(null)} disabled={repairing}>Cancel</Button><Button onClick={() => void repair()} busy={repairing}>Repair installation</Button></>}><p>The signed catalogue hash is checked before any file is restored. The package code will not run in the API.</p></Dialog> : null}
  </div>;
}
