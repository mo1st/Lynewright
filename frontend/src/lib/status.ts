/**
 * One source of truth for how a backend status string reads as colour.
 *
 * Statuses arrive from many tables (agent runs, resources, extensions, incidents,
 * Telegram sessions, resource sync) and previously each surface invented its own
 * `is-<status>` CSS rule, so most values fell through to an unstyled chip. Tones
 * are deliberately semantic and separate from the indigo brand accent.
 */
export type StatusTone = "positive" | "attention" | "critical" | "active" | "neutral";

const TONES: Record<StatusTone, readonly string[]> = {
  positive: [
    "connected", "confirmed", "completed", "approved", "resolved", "published",
    "validated", "installed", "built", "scanned", "passed", "succeeded", "synced",
    "enabled", "active", "healthy", "full_autopilot", "low", "confirmed_direct",
  ],
  attention: [
    "awaiting_approval", "awaiting_confirmation", "pending_qr", "pending",
    "button_clicked", "message_received", "manual_review", "investigating",
    "recovering", "paused", "expired", "warning", "medium", "high",
    "proposal_only", "draft", "planned", "open", "copilot",
    // Operator run states: the run is paused on something external.
    "waiting_tool", "waiting_job", "waiting_approval",
  ],
  critical: [
    "failed", "error", "rejected", "cancelled", "critical", "scan_failed",
    "validation_failed", "logged_out", "rolled_back", "blocked", "denied",
  ],
  active: [
    "running", "executing", "connecting", "scanning", "building", "syncing",
    "in_progress", "queued", "controlled_autopilot",
  ],
  /* `disabled` is deliberate operator intent, not a fault — it stays quiet.
   * `sleeping` is the Operator's idle-by-design state, same reasoning. */
  neutral: [
    "advisor", "disconnected", "disabled", "unknown", "none", "not_started", "skipped",
    "sleeping",
  ],
};

const LOOKUP = new Map<string, StatusTone>();
for (const [tone, values] of Object.entries(TONES) as Array<[StatusTone, readonly string[]]>) {
  for (const value of values) LOOKUP.set(value, tone);
}

/** Resolve a raw status/severity/mode string to its tone. Unknown values stay neutral. */
export function statusTone(value: string | null | undefined): StatusTone {
  if (!value) return "neutral";
  return LOOKUP.get(value.trim().toLowerCase()) ?? "neutral";
}

/** Class list for the shared semantic badge, e.g. `ui-badge ui-badge--critical`. */
export function statusPillClass(value: string | null | undefined): string {
  return `ui-badge ui-badge--${statusTone(value)}`;
}

/** Backend statuses are snake_case; operators read prose. */
export function statusLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return value.replaceAll("_", " ");
}
