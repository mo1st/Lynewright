import { Activity, AlertTriangle, CheckCircle2, CircleHelp, Plug, ShieldAlert } from "lucide-react";
import { Badge, Button, Card, EmptyState, PageHeader, Section, StatusBadge } from "../components/ui";
import { useProject } from "../context/ProjectContext";
import { navigate } from "../router";
import type { DashboardSummary } from "../useDashboardSummary";
import styles from "../components/ui/design-system.module.css";

function countLabel(count: number, singular: string, plural = `${singular}s`) { return `${count} ${count === 1 ? singular : plural}`; }
function relativeTime(seconds: number) {
  const delta = seconds - Date.now() / 1000; const abs = Math.abs(delta);
  const unit: Intl.RelativeTimeFormatUnit = abs < 60 ? "second" : abs < 3600 ? "minute" : abs < 86400 ? "hour" : abs < 604800 ? "day" : "week";
  const divisor = unit === "second" ? 1 : unit === "minute" ? 60 : unit === "hour" ? 3600 : unit === "day" ? 86400 : 604800;
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(Math.round(delta / divisor), unit);
}

export default function Overview({ summary }: { summary: DashboardSummary }) {
  const { projectId, projects } = useProject();
  const projectName = projects.find((project) => project.project_id === projectId)?.name || projectId || "Selected project";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const runsToday = summary.runs.filter((run) => run.created_at * 1000 >= today.getTime()).length;
  const waitingRuns = summary.runs.filter((run) => run.status === "waiting").length;
  const failedRuns = summary.runs.filter((run) => run.status === "failed").length;
  const waitingReviews = waitingRuns + summary.approvals.length;
  const disconnectedRunner = summary.integrations.find((item) => {
    const isContainer = item.availability.execution_plane === "container" || item.integration.execution_plane === "container";
    return isContainer && item.availability.runner_status !== "connected";
  });
  const disconnectedRunners = summary.integrations.filter((item) => {
    const isContainer = item.availability.execution_plane === "container" || item.integration.execution_plane === "container";
    return isContainer && item.availability.runner_status !== "connected";
  }).length;
  const driftedIntegration = summary.integrations.find((item) => item.provenance?.status === "catalogue_drift");
  const catalogueDrift = summary.integrations.filter((item) => item.provenance?.status === "catalogue_drift").length;
  const integrationPath = (item?: DashboardSummary["integrations"][number]) => item ? `/integrations?integration_id=${encodeURIComponent(item.integration.id)}` : "/integrations";

  const metrics = [
    { label: `Lines in ${projectName}`, value: summary.loading ? "…" : summary.linesError ? "—" : summary.lines.length, error: summary.linesError },
    { label: `Runs today in ${projectName}`, value: summary.loading ? "…" : summary.runsError ? "—" : runsToday, error: summary.runsError },
    { label: `Reviews waiting in ${projectName}`, value: summary.loading ? "…" : summary.runsError || summary.approvalsError ? "—" : waitingReviews, error: summary.runsError || summary.approvalsError },
    { label: `Failed runs in ${projectName}`, value: summary.loading ? "…" : summary.runsError ? "—" : failedRuns, error: summary.runsError },
  ];
  const attention = [
    { label: "Reviews waiting", value: summary.runsError || summary.approvalsError ? null : waitingReviews, destination: "/reviews", icon: Activity },
    { label: "Failed runs", value: summary.runsError ? null : failedRuns, destination: "/runs?status=failed", icon: AlertTriangle },
    { label: "Runners not connected", value: summary.integrationsError ? null : disconnectedRunners, destination: integrationPath(disconnectedRunner), icon: Plug },
    { label: "Catalogue drift", value: summary.integrationsError ? null : catalogueDrift, destination: integrationPath(driftedIntegration), icon: ShieldAlert },
  ];
  const attentionItems = attention.filter((item) => item.value === null || item.value > 0);
  const allClear = !summary.loading && !summary.runsError && !summary.approvalsError && !summary.integrationsError && attentionItems.length === 0;

  return <div className={styles.dashboardPage}>
    <PageHeader title="Overview" description={`${projectName} · Production activity and items that need attention.`} />
    <div className={styles.overviewKpis} aria-label={`Production summary for ${projectName}`}>
      {metrics.map((metric) => <Card key={metric.label} padded className={styles.overviewKpi}>
        <span className={styles.overviewKpiLabel}>{metric.label}</span>
        <strong className={styles.overviewKpiValue} aria-label={metric.error ? `${metric.label}: unavailable` : undefined}>{metric.value}</strong>
        {metric.error ? <small>Unavailable</small> : null}
      </Card>)}
    </div>
    <Section title="Needs attention" description={`Current work for ${projectName}.`} className={styles.overviewAttentionSection}>
      {summary.loading ? <p role="status">Checking current work…</p> : null}
      {allClear ? <EmptyState title="Nothing needs attention" description="Your reviews, runs, runners, and catalogue are in good shape." icon={<CheckCircle2 size={20} />} /> : null}
      {attentionItems.length ? <ul className={styles.overviewAttention} aria-label="Items needing attention">
        {attentionItems.map((item) => {
          const Icon = item.value === null ? CircleHelp : item.value > 0 ? AlertTriangle : CheckCircle2;
          return <li key={item.label}><Button variant="ghost" type="button" className={`${styles.overviewAttentionRow} ui-overview-link`} onClick={() => navigate(item.destination)}>
            <Icon className={styles.overviewAttentionIcon} size={18} aria-hidden="true" />
            <span>{item.label}</span>
            <Badge tone={item.value === null ? "neutral" : item.value > 0 ? "warning" : "success"}>{item.value === null ? "Unavailable" : countLabel(item.value, "item")}</Badge>
          </Button></li>;
        })}
      </ul> : null}
    </Section>
    <Section title="Recent runs" description={`The latest activity in ${projectName}.`} className={styles.overviewAttentionSection}>
      {summary.loading ? <p role="status">Loading recent runs…</p> : summary.runsError ? <p role="status">Recent runs are unavailable.</p> : summary.runs.length ? <ol className={styles.overviewRecentRuns} aria-label="Recent runs">
        {[...summary.runs].sort((a, b) => b.created_at - a.created_at).slice(0, 5).map((run) => {
          const line = summary.lines.find((item) => item.id === run.line_id);
          return <li key={run.id}><Button variant="ghost" type="button" className={`${styles.overviewAttentionRow} ui-overview-link`} onClick={() => navigate(`/runs/${encodeURIComponent(run.id)}`)}><StatusBadge status={run.status} /><strong>{line?.name || run.line_id}</strong><time title={new Date(run.created_at * 1000).toLocaleString()}>{relativeTime(run.created_at)}</time></Button></li>;
        })}
      </ol> : <EmptyState title="No runs yet" description="Start a line to see its recent activity here." icon={<Activity size={20} />} action={<Button variant="secondary" onClick={() => navigate("/lines")}>View lines</Button>} />}
    </Section>
  </div>;
}
