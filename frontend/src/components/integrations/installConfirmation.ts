import { ApiError } from "../../api/client";
import type { IntegrationInstallSummary } from "../../types";

export type PendingInstallConfirmation = { summary: IntegrationInstallSummary; summaryHash: string };

export function installConfirmationFromError(error: unknown): PendingInstallConfirmation | null {
  if (!(error instanceof ApiError) || error.status !== 409 || !error.detail || typeof error.detail !== "object") return null;
  const detail = error.detail as Record<string, unknown>;
  const summary = detail.install_summary;
  const summaryHash = detail.summary_hash;
  if (detail.requires_confirmation !== true || !summary || typeof summary !== "object" || typeof summaryHash !== "string") return null;
  return { summary: summary as IntegrationInstallSummary, summaryHash };
}
