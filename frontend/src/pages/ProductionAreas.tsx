import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Activity, ArrowLeft, Bell, Check, CheckCircle2, ChevronDown, Circle, Clock3, GitBranch, Image as ImageIcon, ListChecks, MessageSquareText, Play, RefreshCw, RotateCcw, Settings2, Shapes, ShieldCheck, Workflow, XCircle } from "lucide-react";
import { integrationManagementApi } from "../api/integrationManagementClient";
import { productionLinesApi, type ProductionLine, type ProductionOperation, type ProductionReviewItem, type ProductionRun, type ProductionRunStage, type ProductionStage, type ProductionStageItems } from "../api/productionLinesClient";
import { useProject } from "../context/ProjectContext";
import { confirmAction } from "../context/ConfirmContext";
import { navigate } from "../router";
import type { IntegrationManagementSummary } from "../types";
import { Badge, Button, CopyField, Dialog, EmptyState, FormField, Input, KeyValue, PageHeader, Select, StatusBadge, Switch, Table, Tabs, Textarea, useToast } from "../components/ui";
import TypedMediaPreview from "../components/production/TypedMediaPreview";
import { OperatorApprovalsPanel } from "../components/operator/OperatorApprovalsPanel";
import styles from "./ProductionAreas.module.css";

type ProductionPageProps = { projectId: string; projectName: string; projectLoading?: boolean };
type VarsRow = { key: string; value: string; kind: "text" | "number" | "boolean" };
const ACTIVE = new Set(["queued", "running", "waiting"]);

function object(value: unknown): Record<string, unknown> { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function str(value: unknown): string { return value == null ? "" : typeof value === "string" ? value : typeof value === "number" || typeof value === "boolean" ? String(value) : ""; }
function statusTone(value: string): "neutral" | "success" | "warning" | "danger" | "info" {
  if (["done", "completed", "enabled", "approved"].includes(value)) return "success";
  if (["running", "queued", "waiting", "active"].includes(value)) return "info";
  if (["failed", "rejected", "cancelled", "disabled", "denied"].includes(value)) return "danger";
  if (["skipped", "done_with_errors"].includes(value)) return "warning";
  return "neutral";
}
function absoluteTime(seconds?: number | null): string { return typeof seconds === "number" ? new Date(seconds * 1000).toLocaleString() : "Not started"; }
function relativeTime(seconds?: number | null): string {
  if (typeof seconds !== "number") return "Not started";
  const delta = seconds - Date.now() / 1000;
  const abs = Math.abs(delta);
  const unit: Intl.RelativeTimeFormatUnit = abs < 60 ? "second" : abs < 3600 ? "minute" : abs < 86400 ? "hour" : abs < 604800 ? "day" : abs < 2592000 ? "week" : "month";
  const divisor = unit === "second" ? 1 : unit === "minute" ? 60 : unit === "hour" ? 3600 : unit === "day" ? 86400 : unit === "week" ? 604800 : 2592000;
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(Math.round(delta / divisor), unit);
}
function duration(seconds?: number | null): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
}
function elapsed(run: ProductionRun): number { return Math.max(0, (run.updated_at || run.created_at) - run.created_at); }
function routeForLine(lineId: string): string { return `/lines/${encodeURIComponent(lineId)}`; }
function routeForRun(runId: string): string { return `/runs/${encodeURIComponent(runId)}`; }
function operationIdentity(stage: ProductionStage, operations: ProductionOperation[]): ProductionOperation | undefined {
  const id = str(stage.operation_id);
  return operations.find((item) => id && item.operation_id === id) || operations.find((item) => item.contract === stage.contract && item.operation === stage.operation);
}
function sourceLabel(operation: ProductionOperation | undefined, integrations: IntegrationManagementSummary[]): string {
  if (!operation) return "Core stage";
  const id = str(operation.integration_id);
  return integrations.find((item) => item.integration.id === id)?.integration.name || id || operation.kind;
}
function firstMedia(output: Record<string, unknown>): { url: string; name: string; content_type: string } | null {
  const candidates = [...(Array.isArray(output.items) ? output.items : []), ...(Array.isArray(output.media_previews) ? output.media_previews : []), ...(Array.isArray(output.outputs) ? output.outputs : [])];
  for (const candidate of candidates) {
    const item = object(candidate);
    const previews = Array.isArray(item.media_previews) ? item.media_previews : [];
    for (const raw of [...previews, item]) {
      const media = object(raw);
      const url = str(media.url ?? media.value ?? media.ref);
      const contentType = str(media.content_type ?? media.mime);
      if (url && (contentType.startsWith("image/") || str(media.kind ?? media.type).toLowerCase() === "image")) return { url, name: str(media.name ?? item.label) || "Review image", content_type: contentType || "image/*" };
    }
  }
  return null;
}
function MediaList({ value }: { value: unknown }) {
  const [selected, setSelected] = useState<{ url: string; label: string } | null>(null);
  const rawItems = Array.isArray(value) ? value : [];
  return <>
    <div className={styles.mediaList}>{rawItems.map((raw, index) => {
    const media = object(raw); const url = str(media.url ?? media.value ?? media.ref); if (!url || url.startsWith("managed://")) return null;
    const mime = str(media.content_type ?? media.mime); const label = str(media.label ?? media.name ?? media.type) || `Output ${index + 1}`;
    const kind = str(media.kind ?? media.type) || (mime.startsWith("image/") ? "image" : "download");
    const isImage = kind === "image" || mime.startsWith("image/");
    return <div className={styles.mediaItem} key={`${url}-${index}`}>{isImage ? <Button variant="ghost" type="button" className={styles.mediaThumbButton} aria-label={`Preview ${label}`} onClick={() => setSelected({ url, label })}><TypedMediaPreview kind="image" label={label} mime={mime} value={url} /></Button> : <TypedMediaPreview kind={kind} label={label} mime={mime} value={url} />}</div>;
    })}</div>
    <Dialog open={Boolean(selected)} title={selected?.label || "Media preview"} onClose={() => setSelected(null)} className={styles.mediaPreviewDialog}>
      {selected ? <img className={styles.mediaFullPreview} src={selected.url} alt={selected.label} /> : null}
    </Dialog>
  </>;
}
function OutputViewer({ output }: { output: Record<string, unknown> }) {
  const values = [...(Array.isArray(output.outputs) ? output.outputs : []), ...(Array.isArray(output.media_previews) ? output.media_previews : []), ...(Array.isArray(output.media_refs) ? output.media_refs : [])];
  if (Array.isArray(output.items)) for (const raw of output.items) {
    const item = object(raw);
    const previews = Array.isArray(item.media_previews) ? item.media_previews : [];
    const refs = Array.isArray(item.media_refs) ? item.media_refs : [];
    for (const media of previews.length ? previews : refs) values.push({ ...object(media), label: str(item.label ?? item.title ?? item.name) || "Review item" });
  }
  const text = Object.entries(output).filter(([key]) => !["outputs", "media_previews", "media_refs"].includes(key));
  return <div className={styles.outputViewer}>
    {values.length ? <MediaList value={values} /> : null}
    {text.length ? <KeyValue className={styles.outputValues} items={text.map(([key, value]) => ({ label: key.replace(/[_-]/g, " "), value: typeof value === "string" || typeof value === "number" || typeof value === "boolean" ? String(value) : Array.isArray(value) ? `${value.length} items` : value == null ? "—" : "Structured data" }))} /> : null}
    <details className={styles.jsonDisclosure}><summary>View formatted JSON</summary><pre>{JSON.stringify(output, null, 2)}</pre></details>
  </div>;
}
function EmptyAction({ title, description, action, onAction, icon }: { title: string; description: string; action: string; onAction: () => void; icon?: ReactNode }) {
  return <EmptyState title={title} description={description} icon={icon} action={<Button onClick={onAction}>{action}</Button>} className={styles.areaEmpty} />;
}

export function LinesPage({ projectId, projectName, projectLoading = false }: ProductionPageProps) {
  const [lines, setLines] = useState<ProductionLine[]>([]); const [runs, setRuns] = useState<ProductionRun[]>([]);
  const [loading, setLoading] = useState(true); const [error, setError] = useState(""); const [search, setSearch] = useState(""); const [visible, setVisible] = useState(20);
  const load = useCallback(async () => {
    if (projectLoading) return;
    if (!projectId) { setLines([]); setRuns([]); setLoading(false); return; }
    try { const [lineData, runData] = await Promise.all([productionLinesApi.lines(projectId), productionLinesApi.runs(projectId, 200)]); setLines(lineData.items); setRuns(runData.items); setError(""); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Lines could not be loaded."); }
    finally { setLoading(false); }
  }, [projectId, projectLoading]);
  useEffect(() => { setLoading(true); void load(); }, [load]);
  const latestByLine = useMemo(() => { const map = new Map<string, ProductionRun>(); for (const run of [...runs].sort((a, b) => b.created_at - a.created_at)) if (!map.has(run.line_id)) map.set(run.line_id, run); return map; }, [runs]);
  const filtered = useMemo(() => lines.filter((line) => `${line.name} ${line.id}`.toLowerCase().includes(search.trim().toLowerCase())), [lines, search]);
  const columns = [
    { key: "name", header: "Name", render: (line: ProductionLine) => <span className={styles.lineName}><strong>{line.name}</strong><small>{line.id}</small></span> },
    { key: "stages", header: "Stages", render: (line: ProductionLine) => <div className={styles.stageStrip} aria-label={`${line.definition.stages.length} stages`}><strong>{line.definition.stages.length}</strong>{line.definition.stages.slice(0, 4).map((stage, index) => <Badge key={`${stage.id}-${index}`} tone="neutral">{str(stage.label || stage.kind).replaceAll("_", " ")}</Badge>)}{line.definition.stages.length > 4 ? <span>+{line.definition.stages.length - 4}</span> : null}</div> },
    { key: "lastRun", header: "Last run", render: (line: ProductionLine) => { const run = latestByLine.get(line.id); return run ? <span className={styles.lastRun}><StatusBadge status={run.status} /><time title={absoluteTime(run.created_at)}>{relativeTime(run.created_at)}</time></span> : <span className={styles.muted}>No runs yet</span>; } },
    { key: "weekly", header: "Runs this week", render: (line: ProductionLine) => runs.filter((run) => run.line_id === line.id && run.created_at >= Date.now() / 1000 - 7 * 86400).length },
    { key: "enabled", header: "Status", render: (line: ProductionLine) => <Badge tone={line.enabled ? "success" : "neutral"}>{line.enabled ? "Enabled" : "Disabled"}</Badge> },
  ];
  return <div className={styles.page}>
    <PageHeader title="Lines" description={`${projectName} · Reusable workflows and their latest runs.`} primaryAction={<Button onClick={() => navigate("/lines?line_id=new&edit=1")}>Create line</Button>} />
    <div className={styles.filters}><FormField label="Search lines" htmlFor="line-search"><Input id="line-search" type="search" placeholder="Search by name or id" value={search} onChange={(event) => { setSearch(event.target.value); setVisible(20); }} /></FormField><Button variant="secondary" onClick={() => { setLoading(true); void load(); }} icon={<RefreshCw size={16} />}>Refresh</Button></div>
    {error ? <p className={styles.error} role="alert">{error}</p> : null}
    {filtered.length ? <><Table columns={columns} rows={filtered.slice(0, visible)} getRowKey={(line) => line.id} loading={loading} label="Production lines" onRowClick={(line) => navigate(routeForLine(line.id))} />{filtered.length > visible ? <div className={styles.loadMore}><Button variant="secondary" onClick={() => setVisible((count) => count + 20)}>Load more</Button></div> : null}</>
      : !loading ? search ? <EmptyState title="No matching lines" description="Try a different name or line id." icon={<Workflow size={20} />} /> : <EmptyAction title="No lines yet" description="Create a line from declared operations and review gates." action="Create line" onAction={() => navigate("/lines?line_id=new&edit=1")} icon={<Workflow size={20} />} /> : <p role="status">Loading lines…</p>}
  </div>;
}

export function LineDetailPage({ lineId, projectId, projectName }: ProductionPageProps & { lineId: string }) {
  const [line, setLine] = useState<ProductionLine | null>(null); const [runs, setRuns] = useState<ProductionRun[]>([]); const [operations, setOperations] = useState<ProductionOperation[]>([]); const [integrations, setIntegrations] = useState<IntegrationManagementSummary[]>([]);
  const [loading, setLoading] = useState(true); const [error, setError] = useState(""); const [tab, setTab] = useState(new URLSearchParams(window.location.search).get("tab") || "runs"); const [startOpen, setStartOpen] = useState(false); const [vars, setVars] = useState<VarsRow[]>([{ key: "", value: "", kind: "text" }]); const [busy, setBusy] = useState(false); const [lineName, setLineName] = useState(""); const [reviewBy, setReviewBy] = useState<"dashboard" | "dashboard_or_assistant">("dashboard");
  const { addToast } = useToast(); const reportTabAvailable = false;
  const load = useCallback(async () => {
    if (!projectId) { setLoading(false); setLine(null); return; }
    try {
      const [lineResponse, runResponse, operationResponse, integrationResponse] = await Promise.all([productionLinesApi.lines(projectId), productionLinesApi.runs(projectId, 200), productionLinesApi.operations(projectId), integrationManagementApi.list()]);
      setLine(lineResponse.items.find((item) => item.id === lineId) ?? null); setRuns(runResponse.items.filter((run) => run.line_id === lineId)); setOperations(operationResponse.items); setIntegrations(integrationResponse.items); setError("");
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Line details could not be loaded."); }
    finally { setLoading(false); }
  }, [lineId, projectId]);
  useEffect(() => { setLoading(true); void load(); }, [load]);
  useEffect(() => { if (line) { setLineName(line.name); setReviewBy(line.definition.review_by ?? "dashboard"); } }, [line?.id, line?.name, line?.definition.review_by]);
  useEffect(() => { const update = () => setTab(new URLSearchParams(window.location.search).get("tab") || "runs"); window.addEventListener("popstate", update); window.addEventListener("lynewright:navigate", update); return () => { window.removeEventListener("popstate", update); window.removeEventListener("lynewright:navigate", update); }; }, []);
  const stages = line?.definition.stages ?? [];
  const activeRuns = useMemo(() => runs.filter((run) => ACTIVE.has(run.status)), [runs]);
  async function startRun(event: FormEvent) {
    event.preventDefault(); if (!line) return;
    const named = vars.filter((item) => item.key.trim());
    if (new Set(named.map((item) => item.key.trim())).size !== named.length) { setError("Run input names must be unique."); return; }
    const values = Object.fromEntries(named.map((item) => [item.key.trim(), item.kind === "number" ? Number(item.value) : item.kind === "boolean" ? item.value === "true" : item.value]));
    setBusy(true); setError("");
    try { const run = await productionLinesApi.start(line.id, projectId, values); setStartOpen(false); addToast({ tone: "success", message: "Run started." }); navigate(routeForRun(run.id)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "The run could not be started."); }
    finally { setBusy(false); }
  }
  async function toggleEnabled(enabled: boolean) {
    if (!line) return;
    try { const saved = await productionLinesApi.updateLine(line.id, { enabled }); setLine(saved); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Line settings could not be saved."); }
  }
  async function saveName(event: FormEvent) {
    event.preventDefault(); if (!line || !lineName.trim()) return;
    try { const saved = await productionLinesApi.updateLine(line.id, { name: lineName.trim() }); setLine(saved); addToast({ tone: "success", message: "Line settings saved." }); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Line settings could not be saved."); }
  }
  async function saveReviewAccess(value: "dashboard" | "dashboard_or_assistant") {
    if (!line) return;
    setReviewBy(value);
    try {
      const saved = await productionLinesApi.updateLine(line.id, { definition: { ...line.definition, review_by: value } });
      setLine(saved); addToast({ tone: "success", message: "Review access saved." });
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Review access could not be saved."); setReviewBy(line.definition.review_by ?? "dashboard"); }
  }
  async function removeLine() {
    if (!line || !(await confirmAction({ title: `Remove ${line.name}?`, message: "The line definition will be removed. Run history is kept. Lines with active runs cannot be removed.", confirmLabel: "Remove line", tone: "danger" }))) return;
    try { await productionLinesApi.deleteLine(line.id); addToast({ tone: "success", message: "Line removed. Run history is kept." }); navigate("/lines"); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "The line could not be removed."); }
  }
  if (loading) return <div className={styles.page}><p role="status">Loading line…</p></div>;
  if (!line) return <div className={styles.page}><EmptyState title="Line not found" description={error || "This line is not available in the selected project."} icon={<Workflow size={20} />} action={<Button variant="secondary" onClick={() => navigate("/lines")}>Back to lines</Button>} /></div>;
  const runColumns = [
    { key: "status", header: "Status", render: (run: ProductionRun) => <StatusBadge status={run.status} /> },
    { key: "started", header: "Started", render: (run: ProductionRun) => <time title={absoluteTime(run.created_at)}>{relativeTime(run.created_at)}</time> },
    { key: "duration", header: "Duration", render: (run: ProductionRun) => duration(run.stages.reduce((sum, item) => sum + (item.duration_s || 0), 0) || elapsed(run)) },
    { key: "stage", header: "Current stage", render: (run: ProductionRun) => str(stages.find((stage) => stage.id === run.current_stage)?.label) || run.current_stage || "—" },
  ];
  return <div className={styles.page}>
    <PageHeader title={line.name} description={`${projectName} · ${line.id}`} primaryAction={<Button onClick={() => { setVars([{ key: "", value: "", kind: "text" }]); setStartOpen(true); }} disabled={!line.enabled} icon={<Play size={16} />}>Start run</Button>} secondaryAction={<div className={styles.actionGroup}><StatusBadge status={line.enabled ? "enabled" : "disabled"} /><Button variant="secondary" onClick={() => navigate(`/lines?line_id=${encodeURIComponent(line.id)}&edit=1`)}>Edit</Button><details className={styles.moreMenu}><summary>More <ChevronDown size={14} /></summary><div role="menu"><Button variant="danger" role="menuitem" onClick={() => void removeLine()}>Remove line</Button></div></details></div>} />
    {error ? <p className={styles.error} role="alert">{error}</p> : null}
    <section className={styles.stepper} aria-label="Line stages" tabIndex={0}>{stages.map((stage, index) => {
      const operation = operationIdentity(stage, operations); const Icon = stage.kind === "map" ? Shapes : stage.kind === "for_each" || stage.kind.includes("review") ? ListChecks : stage.kind.includes("choice") ? Circle : stage.kind === "notify" ? Bell : stage.kind === "condition" ? GitBranch : Workflow;
      return <div className={styles.step} data-stage-kind={stage.kind} key={`${stage.id}-${index}`}><div className={styles.stepIcon} data-stage-icon={stage.kind === "map" ? "shape" : stage.kind === "for_each" ? "repeat" : undefined}><Icon size={17} aria-hidden="true" /></div><div className={styles.stepCopy}><span className={styles.stepNumber}>Stage {index + 1}</span><strong>{str(stage.label || stage.id || stage.kind)}</strong><small>{stage.kind === "for_each" ? <>{sourceLabel(operation, integrations)} <Badge tone="neutral">×N</Badge></> : sourceLabel(operation, integrations)}</small></div>{index < stages.length - 1 ? <span className={styles.stepConnector} aria-hidden="true" /> : null}</div>;
    })}</section>
    <Tabs label="Line details" tabs={[{ key: "runs", label: "Runs", count: runs.length }, { key: "settings", label: "Settings" }, ...(reportTabAvailable ? [{ key: "trends", label: "Trends" }] : [])]} active={tab} onChange={(next) => { setTab(next); navigate(`${routeForLine(line.id)}?tab=${next}`); }} className={styles.tabs} />
    {tab === "runs" ? <><Table columns={runColumns} rows={runs} getRowKey={(run) => run.id} label={`Runs for ${line.name}`} loading={false} empty="No runs for this line yet." onRowClick={(run) => navigate(routeForRun(run.id))} />{activeRuns.length ? <p className={styles.muted}>{activeRuns.length} run{activeRuns.length === 1 ? " is" : "s are"} active.</p> : null}</> : null}
    {tab === "settings" ? <section className={styles.settingsPanel} aria-label="Line settings"><h2>Line settings</h2><p className={styles.muted}>The line’s saved settings include its name, enabled state, stage definition, and review access.</p><form className={styles.lineSettingsForm} onSubmit={(event) => void saveName(event)}><FormField label="Line name" htmlFor="line-settings-name"><Input id="line-settings-name" value={lineName} onChange={(event) => setLineName(event.target.value)} required maxLength={200} /></FormField><Button type="submit" variant="secondary">Save name</Button></form><FormField label="Who can decide reviews?" htmlFor="line-review-access" helpText="Assistant decisions are available only when this line allows them."><Select id="line-review-access" value={reviewBy} onChange={(event) => void saveReviewAccess(event.target.value as typeof reviewBy)}><option value="dashboard">Dashboard only</option><option value="dashboard_or_assistant">Dashboard and assistants</option></Select></FormField><dl className={styles.outputValues}><div><dt>Line id</dt><dd><code>{line.id}</code></dd></div><div><dt>Project</dt><dd>{projectName}</dd></div><div><dt>Stages</dt><dd>{stages.length}</dd></div></dl><Switch label="Enabled" checked={line.enabled} onChange={(enabled) => void toggleEnabled(enabled)} /></section> : null}
    {tab === "trends" && reportTabAvailable ? <p>Line trends are not available yet.</p> : null}
    <Dialog open={startOpen} title="Start a run" description={`Add values for ${line.name}. Leave a row blank to omit it.`} onClose={() => setStartOpen(false)} footer={<><Button variant="secondary" onClick={() => setStartOpen(false)}>Cancel</Button><Button type="submit" form="line-start-form" busy={busy}>Start run</Button></>}>
      <form id="line-start-form" className={styles.variableForm} onSubmit={(event) => void startRun(event)}>{vars.map((item, index) => <div className={styles.variableRow} key={index}>
        <FormField label="Name" htmlFor={`line-var-name-${index}`}><Input id={`line-var-name-${index}`} value={item.key} onChange={(event) => setVars((current) => current.map((row, i) => i === index ? { ...row, key: event.target.value } : row))} placeholder="input_name" /></FormField>
        <FormField label="Type" htmlFor={`line-var-kind-${index}`}><Select id={`line-var-kind-${index}`} value={item.kind} onChange={(event) => setVars((current) => current.map((row, i) => i === index ? { ...row, kind: event.target.value as VarsRow["kind"], value: event.target.value === "boolean" ? "true" : "" } : row))}><option value="text">Text</option><option value="number">Number</option><option value="boolean">True or false</option></Select></FormField>
        <FormField label="Value" htmlFor={`line-var-value-${index}`}>{item.kind === "boolean" ? <Select id={`line-var-value-${index}`} value={item.value || "true"} onChange={(event) => setVars((current) => current.map((row, i) => i === index ? { ...row, value: event.target.value } : row))}><option value="true">True</option><option value="false">False</option></Select> : <Input id={`line-var-value-${index}`} type={item.kind === "number" ? "number" : "text"} step={item.kind === "number" ? "any" : undefined} value={item.value} required={item.kind === "number"} onChange={(event) => setVars((current) => current.map((row, i) => i === index ? { ...row, value: event.target.value } : row))} />}</FormField>
        <Button variant="ghost" size="sm" onClick={() => setVars((current) => current.filter((_, i) => i !== index))} disabled={vars.length === 1}>Remove</Button>
      </div>)}<Button variant="secondary" size="sm" onClick={() => setVars((current) => [...current, { key: "", value: "", kind: "text" }])}>Add variable</Button>{error ? <p className={styles.error} role="alert">{error}</p> : null}</form>
    </Dialog>
  </div>;
}

export function RunsPage({ projectId, projectName, projectLoading = false, statusFilter = "", lineFilter = "" }: ProductionPageProps & { statusFilter?: string; lineFilter?: string }) {
  const [runs, setRuns] = useState<ProductionRun[]>([]); const [lines, setLines] = useState<ProductionLine[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState(""); const [status, setStatus] = useState(statusFilter); const [lineId, setLineId] = useState(lineFilter); const [visible, setVisible] = useState(30);
  const load = useCallback(async (quiet = false) => {
    if (projectLoading || !projectId) { if (!projectLoading) setLoading(false); return; }
    if (!quiet) setLoading(true);
    try { const [runResponse, lineResponse] = await Promise.all([productionLinesApi.runs(projectId, 200), productionLinesApi.lines(projectId)]); setRuns(runResponse.items); setLines(lineResponse.items); setError(""); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Runs could not be loaded."); }
    finally { setLoading(false); }
  }, [projectId, projectLoading]);
  useEffect(() => { setLoading(true); void load(); }, [load]);
  const visibleRuns = useMemo(() => runs.filter((run) => {
    const statusMatches = !status || (status === "running" ? ["queued", "running"].includes(run.status) : run.status === status);
    return statusMatches && (!lineId || run.line_id === lineId);
  }).sort((a, b) => b.created_at - a.created_at), [runs, status, lineId]);
  useEffect(() => {
    if (!visibleRuns.some((run) => ACTIVE.has(run.status))) return;
    let timer = 0;
    const update = () => { if (timer) window.clearInterval(timer); timer = 0; if (!document.hidden) timer = window.setInterval(() => void load(true), 5000); };
    update(); document.addEventListener("visibilitychange", update);
    return () => { document.removeEventListener("visibilitychange", update); if (timer) window.clearInterval(timer); };
  }, [load, visibleRuns]);
  const lineMap = useMemo(() => new Map(lines.map((line) => [line.id, line])), [lines]);
  const updateFilters = (nextStatus: string, nextLine: string) => { setStatus(nextStatus); setLineId(nextLine); setVisible(30); const params = new URLSearchParams(); if (nextStatus) params.set("status", nextStatus); if (nextLine) params.set("line", nextLine); navigate(`/runs${params.size ? `?${params}` : ""}`); };
  const columns = [
    { key: "status", header: "Status", render: (run: ProductionRun) => <StatusBadge status={run.status} /> },
    { key: "line", header: "Line", render: (run: ProductionRun) => lineMap.get(run.line_id)?.name || run.line_id },
    { key: "started", header: "Started", render: (run: ProductionRun) => <time title={absoluteTime(run.created_at)}>{relativeTime(run.created_at)}</time> },
    { key: "duration", header: "Duration", render: (run: ProductionRun) => duration(run.stages.reduce((sum, stage) => sum + (stage.duration_s || 0), 0) || elapsed(run)) },
    { key: "stage", header: "Current stage", render: (run: ProductionRun) => str(lineMap.get(run.line_id)?.definition.stages.find((stage) => stage.id === run.current_stage)?.label) || run.current_stage || "—" },
    { key: "actor", header: "Started by", render: (run: ProductionRun) => str(run.timeline.find((event) => event.type === "run.started")?.actor_id) || "Dashboard" },
  ];
  return <div className={styles.page}>
    <PageHeader title="Runs" description={`${projectName} · Progress and outputs from your production lines.`} secondaryAction={<Button variant="secondary" onClick={() => void load()} icon={<RefreshCw size={16} />}>Refresh</Button>} />
    <div className={styles.filters}>
      <FormField label="Status" htmlFor="run-status-filter"><Select id="run-status-filter" value={status} onChange={(event) => updateFilters(event.target.value, lineId)}><option value="">All</option><option value="running">Running</option><option value="waiting">Waiting</option><option value="done">Done</option><option value="done_with_errors">Done with errors</option><option value="failed">Failed</option><option value="rejected">Rejected</option></Select></FormField>
      <FormField label="Line" htmlFor="run-line-filter"><Select id="run-line-filter" value={lineId} onChange={(event) => updateFilters(status, event.target.value)}><option value="">All lines</option>{lines.map((line) => <option key={line.id} value={line.id}>{line.name}</option>)}</Select></FormField>
    </div>
    {error ? <p className={styles.error} role="alert">{error}</p> : null}
    {visibleRuns.length ? <><Table columns={columns} rows={visibleRuns.slice(0, visible)} getRowKey={(run) => run.id} label="Production runs" loading={loading} empty="No runs match these filters." onRowClick={(run) => navigate(routeForRun(run.id))} />{visibleRuns.length > visible ? <div className={styles.loadMore}><Button variant="secondary" onClick={() => setVisible((count) => count + 30)}>Load more</Button></div> : null}</> : !loading ? <EmptyState title={status || lineId ? "No matching runs" : "No runs yet"} description={status || lineId ? "Adjust the filters to see more runs." : "Start a line to see its progress here."} icon={<Activity size={20} />} /> : <p role="status">Loading runs…</p>}
  </div>;
}

function StageTimeline({ run, line }: { run: ProductionRun; line?: ProductionLine }) {
  const [expanded, setExpanded] = useState<string>(() => run.stages.find((item) => item.output && Object.keys(item.output).length)?.stage_id || run.current_stage || run.stages[0]?.stage_id || "");
  return <ol className={styles.timeline}>{run.stages.length ? run.stages.map((stageRun, index) => {
    const definition = line?.definition.stages.find((stage) => stage.id === stageRun.stage_id); const open = expanded === stageRun.stage_id;
    const runEvents = run.timeline.filter((event) => event.stage_id === stageRun.stage_id);
    const stageDuration = stageRun.duration_s ?? (stageRun.started_at ? Math.max(0, (stageRun.ended_at ?? run.updated_at) - stageRun.started_at) : null);
    return <li className={styles.timelineItem} key={`${stageRun.stage_id}-${index}`}>
      <span className={`${styles.timelineMarker} ${styles[`timelineTone_${statusTone(stageRun.status)}`]}`} aria-hidden="true">{stageRun.status === "done" ? <Check size={14} /> : <Circle size={10} />}</span>
      <div className={styles.timelineCard} data-stage-card>
        <Button variant="ghost" type="button" data-u2-control className={styles.timelineToggle} aria-expanded={open} onClick={() => setExpanded(open ? "" : stageRun.stage_id)}><span><strong>{str(definition?.label || definition?.id || stageRun.stage_id)}</strong><small>{str(definition?.kind || "stage").replaceAll("_", " ")} · {stageRun.attempts} attempt{stageRun.attempts === 1 ? "" : "s"}</small></span><span className={styles.timelineMeta}><StatusBadge status={stageRun.status} /><span>{duration(stageDuration)}</span></span></Button>
        {open ? <div className={styles.timelineDetail} data-stage-expanded-content>{stageRun.error ? <p className={styles.error}>{stageRun.error}</p> : null}{Object.keys(stageRun.output || {}).length ? <OutputViewer output={stageRun.output} /> : <p className={styles.muted}>No stage output.</p>}{runEvents.length ? <section className={styles.events}><h3>Stage events</h3><ol>{runEvents.map((event) => { const detail = str(event.detail.message ?? event.detail.error); return <li key={event.id}><time title={absoluteTime(event.created_at)}>{relativeTime(event.created_at)}</time><strong>{event.type.replace(/[._]/g, " ")}</strong>{detail ? <span className={styles.eventDetail} title={detail}>{detail}</span> : null}</li>; })}</ol></section> : null}</div> : null}
      </div>
    </li>;
  }) : <li className={styles.muted}>No stages have started.</li>}</ol>;
}

function shortJson(value: unknown): string {
  let summary: string;
  try { summary = JSON.stringify(value) ?? String(value); } catch { summary = String(value); }
  return summary.length > 116 ? `${summary.slice(0, 113)}…` : summary;
}

function itemOutputValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : { value };
}

function RunItemsPanel({
  run, line, onRunChange,
}: {
  run: ProductionRun;
  line: ProductionLine;
  onRunChange: (run: ProductionRun) => void;
}) {
  const stages = line.definition.stages.filter((stage) => stage.kind === "for_each");
  const [stageId, setStageId] = useState(stages[0]?.id || "");
  const [filter, setFilter] = useState<"all" | "failed" | "succeeded">("all");
  const [items, setItems] = useState<ProductionStageItems | null>(null);
  const [loadingItems, setLoadingItems] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [retrying, setRetrying] = useState(false);
  const stageRun = run.stages.find((item) => item.stage_id === stageId);
  const active = ACTIVE.has(run.status) && run.current_stage === stageId;
  const load = useCallback(async () => {
    if (!stageId) return;
    try {
      const result = await productionLinesApi.stageItems(run.id, stageId, filter);
      setItems(result); setLoadError("");
    } catch (caught) {
      setLoadError(caught instanceof Error ? caught.message : "Items could not be loaded.");
    } finally { setLoadingItems(false); }
  }, [filter, run.id, stageId]);
  useEffect(() => { setLoadingItems(true); void load(); }, [load]);
  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 3000);
    return () => window.clearInterval(timer);
  }, [active, load]);
  useEffect(() => {
    if (!stages.some((stage) => stage.id === stageId)) setStageId(stages[0]?.id || "");
  }, [stageId, stages]);

  async function retryFailed() {
    if (!items || !stageId) return;
    setRetrying(true); setLoadError("");
    try {
      const next = await productionLinesApi.retryFailedItems(run.id, stageId);
      onRunChange(next);
      await load();
    } catch (caught) {
      setLoadError(caught instanceof Error ? caught.message : "Failed items could not be retried.");
    } finally { setRetrying(false); }
  }

  const stageLabel = str(stages.find((stage) => stage.id === stageId)?.label || stageId);
  const processed = items ? items.succeeded + items.failed + items.skipped : 0;
  const retryAvailable = Boolean(items?.failed && stageRun && ["failed", "done_with_errors"].includes(stageRun.status)
    && ["failed", "done_with_errors"].includes(run.status));
  const renderOutput = (item: ProductionStageItems["items"][number]) => item.output == null
    ? <p className={styles.muted}>No output was recorded for this item.</p>
    : <OutputViewer output={itemOutputValue(item.output)} />;

  return <section className={styles.itemsPanel} aria-label="Items">
    <header className={styles.itemsHeader}>
      <div><h2>Items</h2><p className={styles.muted}>{stageLabel} · One operation result per item.</p></div>
      {stages.length > 1 ? <FormField label="Stage" htmlFor="items-stage-filter"><Select id="items-stage-filter" value={stageId} onChange={(event) => setStageId(event.target.value)}>{stages.map((stage) => <option key={stage.id} value={stage.id}>{str(stage.label || stage.id)}</option>)}</Select></FormField> : null}
    </header>
    {items ? <div className={styles.itemsProgress}>
      <progress aria-label={`${stageLabel} item progress`} max={Math.max(items.total, 1)} value={Math.min(processed, items.total)} />
      <span aria-live="polite">{processed} / {items.total} processed</span>
      {items.failed ? <Badge tone="warning">{items.failed} failed</Badge> : null}
    </div> : null}
    <div className={styles.itemsToolbar}>
      <FormField label="Filter items" htmlFor="items-status-filter"><Select id="items-status-filter" value={filter} onChange={(event) => setFilter(event.target.value as typeof filter)}><option value="all">All</option><option value="failed">Failed</option><option value="succeeded">Succeeded</option></Select></FormField>
      <p className={styles.muted} aria-live="polite">{items ? `${items.total} total · ${items.succeeded} succeeded · ${items.failed} failed · ${items.skipped} skipped` : "Loading item counts…"}</p>
      {retryAvailable ? <Button type="button" onClick={() => void retryFailed()} busy={retrying}>Retry failed items</Button> : null}
    </div>
    {loadError ? <div className={styles.itemsError} role="alert"><p>{loadError}</p><Button variant="secondary" type="button" onClick={() => { setLoadingItems(true); void load(); }}>Retry loading items</Button></div> : null}
    {loadingItems && !items ? <p role="status">Loading items…</p> : null}
    {!loadingItems && !loadError && !items?.items.length ? <p className={styles.muted}>{items?.total ? "No items match this filter." : "This stage has no items yet."}</p> : null}
    {items?.items.length ? <>
      <div className={styles.itemsTableWrap}>
        <table className={styles.itemsTable} aria-label={`Items for ${stageLabel}`}>
          <thead><tr><th scope="col">Index</th><th scope="col">Status</th><th scope="col">Input</th><th scope="col">Output</th><th scope="col">Error</th></tr></thead>
          <tbody>{items.items.map((item) => {
            const outputId = `item-output-${item.item_index}`;
            return <tr key={item.item_index}>
              <th scope="row">{item.item_index + 1}</th>
              <td><StatusBadge status={item.status} /></td>
              <td><code className={styles.itemSummary} title={shortJson(item.input)}>{shortJson(item.input)}</code></td>
              <td>{item.output == null ? <span className={styles.muted}>—</span> : <a className={styles.outputLink} href={`#${outputId}`}>View output</a>}</td>
              <td className={styles.itemError}>{item.error || <span className={styles.muted}>—</span>}</td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      <ul className={styles.itemCards} aria-label={`Items for ${stageLabel}`}>
        {items.items.map((item) => {
          const outputId = `item-output-${item.item_index}`;
          return <li key={item.item_index} className={styles.itemCard}>
            <div className={styles.itemCardHeading}><strong>Item {item.item_index + 1}</strong><StatusBadge status={item.status} /></div>
            <span className={styles.itemFieldLabel}>Input</span><code className={styles.itemSummary} title={shortJson(item.input)}>{shortJson(item.input)}</code>
            <span className={styles.itemFieldLabel}>Output</span>{item.output == null ? <span className={styles.muted}>—</span> : <a className={styles.outputLink} href={`#${outputId}`}>View output</a>}
            <span className={styles.itemFieldLabel}>Error</span><span className={styles.itemError}>{item.error || <span className={styles.muted}>—</span>}</span>
          </li>;
        })}
      </ul>
      <section className={styles.itemOutputs} aria-label="Item outputs">
        {items.items.filter((item) => item.output != null).map((item) => {
          const outputId = `item-output-${item.item_index}`;
          return <details className={styles.itemOutput} id={outputId} key={item.item_index}><summary>Output for item {item.item_index + 1}</summary>{renderOutput(item)}</details>;
        })}
      </section>
    </> : null}
  </section>;
}

export function RunDetailPage({ runId, projectId, projectName }: ProductionPageProps & { runId: string }) {
  const [run, setRun] = useState<ProductionRun | null>(null); const [line, setLine] = useState<ProductionLine | undefined>(); const [loading, setLoading] = useState(true); const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const [tab, setTab] = useState(new URLSearchParams(window.location.search).get("tab") || "timeline"); const [continueJson, setContinueJson] = useState("{}"); const [gateChoice, setGateChoice] = useState(""); const [reportTabAvailable] = useState(false); const { addToast } = useToast();
  const load = useCallback(async () => {
    try {
      const [runData, lineData] = await Promise.all([productionLinesApi.run(runId), productionLinesApi.lines(projectId)]);
      setRun(runData); setLine(lineData.items.find((item) => item.id === runData.line_id)); setError("");
    }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Run could not be loaded."); }
    finally { setLoading(false); }
  }, [projectId, runId]);
  useEffect(() => { setLoading(true); void load(); }, [load]);
  useEffect(() => {
    if (!run || !ACTIVE.has(run.status)) return undefined;
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 5000);
    return () => window.clearInterval(timer);
  }, [load, run?.status]);
  useEffect(() => { const update = () => setTab(new URLSearchParams(window.location.search).get("tab") || "timeline"); window.addEventListener("popstate", update); window.addEventListener("lynewright:navigate", update); return () => { window.removeEventListener("popstate", update); window.removeEventListener("lynewright:navigate", update); }; }, []);
  async function action(kind: "cancel" | "retry" | "continue", value?: Record<string, unknown>) {
    if (!run) return; setBusy(true); setError("");
    try {
      const next = kind === "cancel" ? await productionLinesApi.cancel(run.id) : kind === "retry" ? await productionLinesApi.retry(run.id, str(value?.stage_id || run.stages.find((item) => item.status === "failed")?.stage_id)) : await productionLinesApi.continue(run.id, value || {});
      setRun(next); addToast({ tone: "success", message: kind === "cancel" ? "Run cancelled." : kind === "retry" ? "Failed stage retried." : "Run continued." });
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Run action failed."); }
    finally { setBusy(false); }
  }
  async function archiveRun() {
    if (!run || !(await confirmAction({ title: "Archive this run?", message: "It will leave the Runs list. Its history and review trail remain stored.", confirmLabel: "Archive run", tone: "danger" }))) return;
    setBusy(true);
    try { await productionLinesApi.archiveRun(run.id); addToast({ tone: "success", message: "Run archived." }); navigate("/runs"); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "The run could not be archived."); }
    finally { setBusy(false); }
  }
  if (loading) return <div className={styles.page}><p role="status">Loading run…</p></div>;
  if (!run) return <div className={styles.page}><EmptyState title="Run not found" description={error || "This run is not available in the selected project."} icon={<Activity size={20} />} action={<Button variant="secondary" onClick={() => navigate("/runs")}>Back to runs</Button>} /></div>;
  const failedStage = run.stages.find((stage) => stage.status === "failed"); const active = ACTIVE.has(run.status); const lastOutput = run.stages.reduce<Record<string, unknown>>((memo, stage) => ({ ...memo, [stage.stage_id]: stage.output }), {});
  const currentStage = line?.definition.stages.find((stage) => stage.id === run.current_stage); const gateOutput = object(run.stages.find((stage) => stage.stage_id === run.current_stage)?.output); const gateItems = Array.isArray(gateOutput.items) ? gateOutput.items : [];
  const itemStages = line?.definition.stages.filter((stage) => stage.kind === "for_each") ?? [];
  return <div className={styles.page}>
    <PageHeader title={line?.name || run.line_id} description={`${projectName} · Run ${run.id}`} primaryAction={<div className={styles.actionGroup}>{active ? <Button variant="danger" onClick={() => void action("cancel")} busy={busy}>Cancel</Button> : null}{failedStage ? <Button variant="secondary" onClick={() => void action("retry", { stage_id: failedStage.stage_id })} busy={busy} icon={<RotateCcw size={16} />}>Retry</Button> : null}{!active ? <details className={styles.moreMenu}><summary>More <ChevronDown size={14} /></summary><div role="menu"><Button variant="danger" role="menuitem" onClick={() => void archiveRun()} busy={busy}>Archive</Button></div></details> : null}</div>} />
    {error ? <p className={styles.error} role="alert">{error}</p> : null}
    <div className={styles.runIdentity}><StatusBadge status={run.status} /><CopyField label="Run id" value={run.id} /></div>
    <Tabs label="Run details" tabs={[{ key: "timeline", label: "Timeline" }, ...(itemStages.length ? [{ key: "items", label: "Items" }] : []), { key: "raw", label: "Raw output" }, ...(reportTabAvailable ? [{ key: "report", label: "Report" }] : [])]} active={tab} onChange={(next) => { setTab(next); navigate(`${routeForRun(run.id)}?tab=${next}`); }} className={styles.tabs} />
    {tab === "timeline" ? <StageTimeline run={run} line={line} /> : null}
    {tab === "items" && line ? <RunItemsPanel run={run} line={line} onRunChange={(next) => { setRun(next); addToast({ tone: "success", message: "Failed items queued for retry." }); }} /> : null}
    {tab === "raw" ? <section className={styles.rawOutput} aria-label="Run output"><h2>Run output</h2><OutputViewer output={lastOutput} /></section> : null}
    {run.status === "waiting" && currentStage?.kind === "gate:choice" ? <section className={styles.continuePanel} aria-label="Choice gate"><h2>Choose an option</h2><div className={styles.choiceCards} role="radiogroup" aria-label="Run choice">{gateItems.map((raw, index) => { const item = object(raw); const value = str(item.id ?? item.value ?? item.key ?? index); return <label className={`${styles.choiceCard}${gateChoice === value ? ` ${styles.choiceCardSelected}` : ""}`} key={value}><input type="radio" name="run-choice" checked={gateChoice === value} onChange={() => setGateChoice(value)} /><span><strong>{str(item.label ?? item.title ?? item.name) || value}</strong>{str(item.description) ? <small>{str(item.description)}</small> : null}</span></label>; })}</div><Button onClick={() => { const chosen = gateItems.find((raw, index) => str(object(raw).id ?? object(raw).value ?? object(raw).key ?? index) === gateChoice); if (chosen !== undefined) void action("continue", { choice: chosen }); }} disabled={!gateChoice} busy={busy}>Continue</Button></section> : null}
    {run.status === "waiting" && currentStage?.kind === "gate:review" ? <section className={styles.continuePanel}><h2>Review this run</h2><p className={styles.muted}>Review items and submit a decision in Reviews.</p><Button variant="secondary" onClick={() => navigate("/reviews")}>Open Reviews</Button></section> : null}
    {run.status === "waiting" && currentStage && !currentStage.kind.startsWith("gate:") ? <section className={styles.continuePanel}><FormField label="Continuation value (JSON object)" htmlFor="run-continue-value"><Textarea id="run-continue-value" rows={4} value={continueJson} onChange={(event) => setContinueJson(event.target.value)} /></FormField><Button variant="secondary" onClick={() => { try { const parsed = JSON.parse(continueJson); if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) void action("continue", parsed); else setError("Continuation value must be a JSON object."); } catch { setError("Enter a valid JSON object to continue."); } }} busy={busy}>Continue</Button></section> : null}
  </div>;
}

type ReviewRecord = { id: string; run: ProductionRun; line?: ProductionLine; kind: "review" | "choice"; item: Record<string, unknown> };

export function ReviewsPage({ projectId, projectName, projectLoading = false }: ProductionPageProps) {
  const [runs, setRuns] = useState<ProductionRun[]>([]); const [lines, setLines] = useState<ProductionLine[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState(""); const [focusedRunId, setFocusedRunId] = useState(""); const [reviewItems, setReviewItems] = useState<ProductionReviewItem[]>([]); const [reviewError, setReviewError] = useState(""); const [choice, setChoice] = useState(""); const [rejectReason, setRejectReason] = useState(""); const [rejectOpen, setRejectOpen] = useState(false); const [confirmApprove, setConfirmApprove] = useState(false); const [busy, setBusy] = useState(false); const [mobileFocused, setMobileFocused] = useState(false); const [area, setArea] = useState("line"); const { addToast } = useToast();
  const load = useCallback(async () => {
    if (projectLoading || !projectId) { setLoading(false); return; }
    try { const [runResponse, lineResponse] = await Promise.all([productionLinesApi.runs(projectId, 200), productionLinesApi.lines(projectId)]); setRuns(runResponse.items); setLines(lineResponse.items); setError(""); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Review queue could not be loaded."); }
    finally { setLoading(false); }
  }, [projectId, projectLoading]);
  useEffect(() => { setLoading(true); void load(); }, [load]);
  const lineMap = useMemo(() => new Map(lines.map((line) => [line.id, line])), [lines]);
  const queue = useMemo(() => runs.flatMap((run) => {
    if (run.status !== "waiting") return [];
    const line = lineMap.get(run.line_id); const stage = line?.definition.stages.find((item) => item.id === run.current_stage); const stageRun = run.stages.find((item) => item.stage_id === run.current_stage);
    const output = object(stageRun?.output); const waiting = object(output.waiting);
    const kind = stage?.kind === "gate:choice" ? "choice" : stage?.kind === "gate:review" || waiting.kind === "review" ? "review" : null;
    if (!kind) return [];
    const records = Array.isArray(output.items) ? output.items : [];
    return [{ id: run.id, run, line, kind, item: object(records[0]) } satisfies ReviewRecord];
  }), [runs, lineMap]);
  const focused = queue.find((item) => item.id === focusedRunId) ?? queue[0];
  useEffect(() => { if (queue.length && !queue.some((item) => item.id === focusedRunId)) setFocusedRunId(queue[0].id); if (!queue.length) setFocusedRunId(""); }, [queue, focusedRunId]);
  const stageRun = focused?.run.stages.find((item) => item.stage_id === focused.run.current_stage); const stageOutput = object(stageRun?.output); const waiting = object(stageOutput.waiting); const operationReview = waiting.kind === "review";
  useEffect(() => {
    let current = true; setReviewItems([]); setReviewError(""); setRejectReason(""); setRejectOpen(false); setChoice("");
    if (!focused || !operationReview) return () => { current = false; };
    productionLinesApi.reviewItems(focused.run.id).then((response) => { if (current) setReviewItems(response.items); }).catch((caught) => { if (current) setReviewError(caught instanceof Error ? caught.message : "Review media could not be loaded."); });
    return () => { current = false; };
  }, [focused?.run.id, operationReview]);
  const rawReviewItems = Array.isArray(stageOutput.items) ? stageOutput.items.map(object) : [];
  const fromRaw = (item: Record<string, unknown>, index: number): ProductionReviewItem => ({
    ...item,
    id: str(item.id) || String(index),
    label: str(item.label ?? item.title ?? item.name) || `Review item ${index + 1}`,
    title: str(item.title ?? item.label ?? item.name),
    text: str(item.text),
    description: str(item.description ?? item.caption),
    caption: str(item.caption ?? item.description),
    media_previews: Array.isArray(item.media_previews) ? item.media_previews as ProductionReviewItem["media_previews"] : [],
  });
  const reviewQueueItems: ProductionReviewItem[] = operationReview ? reviewItems.map((item, index) => {
    const raw = rawReviewItems.find((candidate) => str(candidate.id) === item.id) ?? {};
    const source = fromRaw({ ...item, ...raw, media_previews: item.media_previews }, index);
    return { ...source, ...item, title: str(raw.title ?? raw.label ?? raw.name ?? item.title ?? item.label), text: str(raw.text ?? item.text), description: str(raw.description ?? raw.caption ?? item.description ?? item.caption), media_previews: item.media_previews };
  }) : rawReviewItems.map(fromRaw);
  const focusAt = (index: number) => { const next = queue[(index + queue.length) % queue.length]; if (next) { setFocusedRunId(next.id); setMobileFocused(true); } };
  const focusedIndex = queue.findIndex((item) => item.id === focused?.id);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.target instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;
      if (!queue.length) return;
      if (event.key.toLowerCase() === "j") { event.preventDefault(); focusAt(focusedIndex + 1); }
      if (event.key.toLowerCase() === "k") { event.preventDefault(); focusAt(focusedIndex - 1); }
      if (event.key.toLowerCase() === "a" && focused?.kind === "review") { event.preventDefault(); setConfirmApprove(true); }
      if (event.key.toLowerCase() === "r" && focused?.kind === "review") { event.preventDefault(); setRejectOpen(true); window.setTimeout(() => document.getElementById("review-reject-reason")?.focus(), 0); }
    };
    window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey);
  }, [queue, focusedIndex, focused?.kind]);
  async function submitDecision(decision: "approved" | "refused") {
    if (!focused) return;
    if (decision === "refused" && !rejectReason.trim()) return;
    setBusy(true); setError("");
    try {
      if (focused.kind === "choice") {
        const choices = Array.isArray(stageOutput.items) ? stageOutput.items : [];
        const chosen = choices.find((raw, index) => { const item = object(raw); return str(item.id ?? item.value ?? item.key ?? index) === choice; });
        if (chosen === undefined) throw new Error("Choose an option before continuing.");
        const next = await productionLinesApi.continue(focused.run.id, { choice: chosen }); setRuns((items) => items.map((run) => run.id === next.id ? next : run));
      } else if (operationReview) {
        const decisions = reviewQueueItems.map((item) => ({ id: item.id, decision, reason: decision === "refused" ? rejectReason.trim() : "" }));
        const result = await productionLinesApi.submitReview(focused.run.id, decisions); setRuns((items) => items.map((run) => run.id === result.run.id ? result.run : run));
      } else {
        const items = reviewQueueItems.map((item) => ({ id: item.id, decision, reason: decision === "refused" ? rejectReason.trim() : "" }));
        const next = await productionLinesApi.continue(focused.run.id, { approved: decision === "approved", items }); setRuns((items) => items.map((run) => run.id === next.id ? next : run));
      }
      addToast({ tone: "success", message: decision === "approved" ? "Review approved." : focused.kind === "choice" ? "Choice submitted." : "Review rejected." }); setConfirmApprove(false); setRejectOpen(false); setMobileFocused(false); setRejectReason(""); void load();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "The review could not be submitted."); }
    finally { setBusy(false); }
  }
  const tabs = [{ key: "line", label: "Line reviews", count: queue.length }, { key: "operator", label: "Operator approvals" }];
  return <div className={styles.page}>
    <PageHeader title="Reviews" description={`${projectName} · Review line items and operator requests waiting for you.`} />
    <Tabs label="Review queues" tabs={tabs} active={area} onChange={setArea} className={styles.tabs} />
    {error ? <p className={styles.error} role="alert">{error}</p> : null}
    {area === "operator" ? <section className={styles.operatorApprovals}><OperatorApprovalsPanel /></section> : loading ? <p role="status">Loading review queue…</p> : queue.length ? <div className={`${styles.reviewLayout}${mobileFocused && focused ? ` ${styles.reviewMobileFocused}` : ""}`}>
      <aside className={styles.reviewQueue} aria-label="Waiting review queue"><h2>Waiting</h2>{queue.map((item, index) => {
        const stageRecord = item.run.stages.find((entry) => entry.stage_id === item.run.current_stage); const media = firstMedia(object(stageRecord?.output));
        return <Button variant="ghost" type="button" data-u2-control className={`${styles.queueItem}${focused?.id === item.id ? ` ${styles.queueItemActive}` : ""}`} key={item.id} aria-current={focused?.id === item.id ? "true" : undefined} onClick={() => { setFocusedRunId(item.id); setMobileFocused(true); }}>
          {media ? <img className={styles.queueThumb} src={media.url} alt="" /> : <span className={styles.queueThumbPlaceholder}><ImageIcon size={18} /></span>}
          <span className={styles.queueCopy}><strong title={item.line?.name || item.run.line_id}>{item.line?.name || item.run.line_id}</strong><span className={styles.queueBadge}><Badge tone="warning">Waiting</Badge></span><small>{str(item.line?.definition.stages.find((stage) => stage.id === item.run.current_stage)?.label || item.run.current_stage || item.kind)}</small><small title={absoluteTime(item.run.updated_at)}>Waiting {relativeTime(item.run.updated_at)}</small></span>
        </Button>;
      })}</aside>
      {focused ? <section className={styles.reviewFocus} aria-label="Focused review">
        <div className={styles.mobileReviewTop}><Button variant="ghost" size="sm" icon={<ArrowLeft size={16} />} onClick={() => setMobileFocused(false)}>Queue</Button><span>{focusedIndex + 1} of {queue.length}</span></div>
        <div className={styles.reviewFocusHeader}><div><p className={styles.eyebrow}>{focused.kind === "choice" ? "Choice gate" : operationReview ? "Integration review" : "Line review"}</p><h2>{focused.line?.name || focused.run.line_id}</h2><KeyValue className={styles.reviewMeta} items={[{ label: "Stage", value: str(focused.line?.definition.stages.find((stage) => stage.id === focused.run.current_stage)?.label || focused.run.current_stage) }, { label: "Run", value: focused.run.id }, { label: "Waiting since", value: <time title={absoluteTime(focused.run.updated_at)}>{relativeTime(focused.run.updated_at)}</time> }]} /></div><Button variant="secondary" size="sm" onClick={() => navigate(routeForRun(focused.run.id))}>Run details</Button></div>
        {focused.kind === "choice" ? <div className={styles.choiceCards} role="radiogroup" aria-label="Choose an option">{(Array.isArray(stageOutput.items) ? stageOutput.items : []).map((raw, index) => { const item = object(raw); const id = str(item.id ?? item.value ?? item.key ?? index); return <label className={`${styles.choiceCard}${choice === id ? ` ${styles.choiceCardSelected}` : ""}`} key={id}><input type="radio" name="review-choice" value={id} checked={choice === id} onChange={() => setChoice(id)} /><span><strong>{str(item.label ?? item.title ?? item.name) || id}</strong>{str(item.description) ? <small>{str(item.description)}</small> : null}</span></label>; })}</div> : <>
          {reviewError ? <p role="alert" className={styles.error}>{reviewError}</p> : null}
          <div className={styles.reviewItems}>{reviewQueueItems.map((item) => <article className={styles.reviewCard} key={item.id}><div className={styles.reviewMedia}>{item.media_previews.map((media, index) => <TypedMediaPreview key={`${media.media_id}-${index}`} kind={media.content_type.startsWith("image/") ? "image" : "download"} label={media.name || item.label} mime={media.content_type} value={media.url} />)}</div><div><h3>{item.title || item.label}</h3><KeyValue className={styles.reviewItemFields} items={[...(item.text ? [{ label: "Text", value: <span className={styles.preservedText}>{item.text}</span> }] : []), ...((item.description || item.caption) && (item.description || item.caption) !== item.text ? [{ label: "Description", value: <span className={styles.preservedText}>{item.description || item.caption}</span> }] : [])]} /></div></article>)}</div>
          {!reviewQueueItems.length && (str(focused.item.text) || str(focused.item.description) || str(focused.item.caption)) ? <KeyValue className={styles.reviewItemFields} items={[...(str(focused.item.text) ? [{ label: "Text", value: <span className={styles.preservedText}>{str(focused.item.text)}</span> }] : []), ...((str(focused.item.description) || str(focused.item.caption)) ? [{ label: "Description", value: <span className={styles.preservedText}>{str(focused.item.description ?? focused.item.caption)}</span> }] : [])]} /> : null}
          {rejectOpen ? <div className={styles.rejectEditor}><FormField label="Reason for rejection" htmlFor="review-reject-reason"><Textarea id="review-reject-reason" autoFocus rows={3} value={rejectReason} onChange={(event) => setRejectReason(event.target.value)} placeholder="Required if you reject this review" /></FormField><div className={styles.actionGroup}><Button variant="secondary" onClick={() => { setRejectOpen(false); setRejectReason(""); }}>Cancel</Button><Button variant="danger" onClick={() => void submitDecision("refused")} disabled={!rejectReason.trim()} busy={busy} icon={<XCircle size={16} />}>Confirm rejection</Button></div></div> : null}
        </>}
        <div className={styles.reviewActions} data-review-actions>
          {focused.kind === "choice" ? <Button onClick={() => void submitDecision("approved")} disabled={!choice} busy={busy}>Continue</Button> : <><Button onClick={() => setConfirmApprove(true)} busy={busy} icon={<CheckCircle2 size={16} />}>Approve</Button>{!rejectOpen ? <Button variant="danger" onClick={() => setRejectOpen(true)} busy={busy} icon={<XCircle size={16} />}>Reject</Button> : null}</>}
        </div>
        <footer className={styles.shortcutHints}><span>j / k move</span><span>a approve</span><span>r reject</span></footer>
      </section> : null}
      <Dialog open={confirmApprove} title="Approve this review?" description="This decision will continue the waiting line run." onClose={() => setConfirmApprove(false)} footer={<><Button variant="secondary" onClick={() => setConfirmApprove(false)}>Cancel</Button><Button onClick={() => void submitDecision("approved")} busy={busy}>Approve</Button></>}><p>{focused?.line?.name || "This line"} · {str(focused?.line?.definition.stages.find((stage) => stage.id === focused.run.current_stage)?.label || focused?.run.current_stage)}</p></Dialog>
    </div> : <EmptyState title="Nothing to review" description="Waiting line items will appear here. You can inspect recent activity in Runs." icon={<ShieldCheck size={20} />} action={<Button variant="secondary" onClick={() => navigate("/runs")}>View runs</Button>} />}
    {!loading && area === "line" ? <div className={styles.shortcutFooter}>Keyboard shortcuts: <kbd>j</kbd>/<kbd>k</kbd> move · <kbd>a</kbd> approve · <kbd>r</kbd> reject</div> : null}
  </div>;
}
