import { Badge } from "../ui";
import { statusLabel, statusTone } from "../../lib/status";

export function isTerminalRunState(status: string): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

/** The run is paused on something external (a tool, a job, a human, a schedule). */
export function isWaitingRunState(status: string): boolean {
  return status === "waiting_tool" || status === "waiting_job"
    || status === "waiting_approval" || status === "sleeping";
}

/**
 * Semantic chip for a raw backend status. The canonical value stays available
 * as the title attribute; only its display is humanised.
 */
export function RunStateBadge({ value }: { value: string }) {
  return <Badge tone={statusTone(value)}><span title={value}>{statusLabel(value)}</span></Badge>;
}

export function formatWhen(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}
