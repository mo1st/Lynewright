import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { ApplicationLog, ApplicationLogList, ApplicationLogLevel } from "../types";
import { confirmAction } from "../context/ConfirmContext";
import { useProject } from "../context/ProjectContext";
import { Button, FormField, Input, PageHeader, Select, Table, type TableColumn } from "../components/ui";

const LEVELS: Array<"" | ApplicationLogLevel> = ["", "critical", "error", "warning", "info", "debug"];
const TIME_RANGES = ["all", "1h", "24h", "7d", "30d"] as const;

export default function ApplicationLogs({ showPageHeader = true }: { showPageHeader?: boolean }) {
  const { projectId, projects, loading: projectLoading } = useProject();
  const projectName = projects.find((project) => project.project_id === projectId)?.name || projectId || "Selected project";
  const projectReady = !projectLoading && Boolean(projectId);
  const [data, setData] = useState<ApplicationLogList>({ items: [], total: 0, counts: {} });
  const [level, setLevel] = useState("");
  const [category, setCategory] = useState("");
  const [search, setSearch] = useState("");
  const [timeRange, setTimeRange] = useState<(typeof TIME_RANGES)[number]>("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!projectReady) {
      setData({ items: [], total: 0, counts: {} }); setError(""); setLoading(projectLoading); return;
    }
    setLoading(true); setError("");
    try { setData(await api.getApplicationLogs({ level, category, search: search.trim() })); }
    catch (nextError) { setError(nextError instanceof Error ? nextError.message : "Could not load application logs."); }
    finally { setLoading(false); }
  }, [category, level, projectLoading, projectReady, search]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!projectReady) return;
    const timer = window.setInterval(() => { void load(); }, 15000);
    return () => window.clearInterval(timer);
  }, [load, projectReady]);

  const categories = useMemo(() => Array.from(new Set(data.items.map((item) => item.category))).sort(), [data.items]);
  const filteredItems = useMemo(() => {
    if (timeRange === "all") return data.items;
    const duration = timeRange === "1h" ? 60 * 60_000 : timeRange === "24h" ? 24 * 60 * 60_000 : timeRange === "7d" ? 7 * 24 * 60 * 60_000 : 30 * 24 * 60 * 60_000;
    const since = Date.now() - duration;
    return data.items.filter((item) => new Date(normalizeTimestamp(item.created_at)).getTime() >= since);
  }, [data.items, timeRange]);

  async function clear() {
    if (!projectReady) return;
    if (!(await confirmAction({ title: "Clear all application logs?", message: "Every recorded entry is discarded. This cannot be undone.", confirmLabel: "Clear logs", tone: "danger" }))) return;
    try { await api.clearApplicationLogs(); await load(); }
    catch (nextError) { setError(nextError instanceof Error ? nextError.message : "Could not clear logs."); }
  }

  const columns: Array<TableColumn<ApplicationLog>> = [
    { key: "time", header: "Time", render: (item) => { const absolute = formatTimestamp(item.created_at); return <time dateTime={normalizeTimestamp(item.created_at)} title={absolute}>{relativeTime(item.created_at)}</time>; } },
    { key: "level", header: "Level", render: (item) => <span className={`log-level level-${item.level}`}>{item.level}</span> },
    { key: "category", header: "Category", render: (item) => <><strong>{item.category}</strong><small className="muted-text">{item.source}</small></> },
    { key: "message", header: "Message", render: (item) => <span title={item.message}>{item.message}</span> },
    { key: "details", header: "Details", render: (item) => <LogDetails item={item} /> },
  ];

  return <div className="logs-page">
    {showPageHeader ? <PageHeader title="Application logs" description={`${projectName} · Backend failures, provider errors, and frontend warnings.`} secondaryAction={<Button variant="secondary" disabled={loading} onClick={() => void load()}>{loading ? "Refreshing…" : "Refresh logs"}</Button>} /> : null}
    <section className="logs-counts" aria-label="Log counts">
      <Count label="Total" value={data.total} /><Count label="Critical" value={data.counts.critical ?? 0} tone="critical" /><Count label="Errors" value={data.counts.error ?? 0} tone="error" /><Count label="Warnings" value={data.counts.warning ?? 0} tone="warning" />
    </section>
    <div className="logs-toolbar" aria-label="Filter application logs">
      <FormField label="Level" htmlFor="log-level-filter" className="logs-filter-field"><Select id="log-level-filter" value={level} onChange={(event) => setLevel(event.target.value)}>{LEVELS.map((item) => <option key={item || "all"} value={item}>{item ? item[0].toUpperCase() + item.slice(1) : "All levels"}</option>)}</Select></FormField>
      <FormField label="Category" htmlFor="log-category-filter" className="logs-filter-field"><Select id="log-category-filter" value={category} onChange={(event) => setCategory(event.target.value)}><option value="">All categories</option>{categories.map((item) => <option value={item} key={item}>{item}</option>)}</Select></FormField>
      <FormField label="Search" htmlFor="log-search" className="logs-filter-field logs-search"><Input id="log-search" value={search} placeholder="Message, source, or details" onChange={(event) => setSearch(event.target.value)} /></FormField>
      <FormField label="Time range" htmlFor="log-time-range" className="logs-filter-field"><Select id="log-time-range" value={timeRange} onChange={(event) => setTimeRange(event.target.value as (typeof TIME_RANGES)[number])}><option value="all">All returned entries</option><option value="1h">Last hour</option><option value="24h">Last 24 hours</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option></Select></FormField>
      <Button variant="danger" onClick={() => void clear()}>Clear logs</Button>
    </div>
    {error ? <p className="form-error" role="alert" aria-live="assertive">{error}</p> : null}
    <Table label="Application logs" columns={columns} rows={filteredItems} getRowKey={(item) => item.id} loading={loading} empty="No matching logs." />
    <p className="logs-result-count" role="status">Showing {filteredItems.length} of {data.items.length} returned entries. The server returns up to 100 matches.</p>
  </div>;
}

function Count({ label, value, tone = "" }: { label: string; value: number; tone?: string }) {
  return <div className={tone ? `log-count count-${tone}` : "log-count"}><span>{label}</span><strong>{value}</strong></div>;
}

/** Summary first; the monospace raw payload is only visible when expanded. */
function LogDetails({ item }: { item: ApplicationLog }) {
  const details = item.details as Record<string, unknown>;
  const full = { correlation_id: item.correlation_id || undefined, request_path: item.request_path || undefined, ...details };
  if (!Object.keys(details).length && !item.correlation_id && !item.request_path) return <span className="muted-text">—</span>;
  const highlights = ["hint", "failure_kind", "error_type", "error_message", "detail", "stage", "endpoint", "http_status", "provider", "fallback_reason"]
    .filter((key) => details[key] !== undefined && details[key] !== null && details[key] !== "")
    .slice(0, 2)
    .map((key) => `${key.replaceAll("_", " ")}: ${String(details[key])}`);
  return <details className="log-details"><summary>{highlights.join(" · ") || "View details"}</summary><pre className="log-payload">{JSON.stringify(full, null, 2)}</pre></details>;
}

function normalizeTimestamp(value: string): string { return value.includes("T") ? value : `${value.replace(" ", "T")}Z`; }
function formatTimestamp(value: string): string { const date = new Date(normalizeTimestamp(value)); return Number.isNaN(date.getTime()) ? value : date.toLocaleString(); }
function relativeTime(value: string): string {
  const date = new Date(normalizeTimestamp(value));
  const delta = date.getTime() - Date.now();
  if (Number.isNaN(delta)) return value;
  const minutes = Math.round(delta / 60_000);
  const hours = Math.round(minutes / 60);
  const days = Math.round(hours / 24);
  return minutes > -1 ? "just now" : minutes > -60 ? `${Math.abs(minutes)}m ago` : hours > -24 ? `${Math.abs(hours)}h ago` : `${Math.abs(days)}d ago`;
}
