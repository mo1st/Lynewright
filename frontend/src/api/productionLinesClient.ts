import { apiRequest } from "./client";

export type ProductionStage = Record<string, unknown> & {
  id: string;
  kind: string;
  contract?: string;
  operation?: string;
};

export type ProductionLine = {
  id: string;
  name: string;
  project_id: string;
  enabled: boolean;
  definition: { stages: ProductionStage[] };
  updated_at?: number;
};

export type ProductionRunStage = {
  stage_id: string;
  status: string;
  attempts: number;
  started_at: number | null;
  ended_at: number | null;
  duration_s: number | null;
  output_ref: string;
  output: Record<string, unknown>;
  progress: Record<string, unknown>;
  error: string;
};

export type ProductionRun = {
  id: string;
  line_id: string;
  project_id: string;
  status: string;
  current_stage: string;
  context: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  archived_at?: string;
  stages: ProductionRunStage[];
  timeline: Array<{
    id: number;
    type: string;
    stage_id: string;
    detail: Record<string, unknown>;
    actor_id: string;
    created_at: number;
  }>;
};

export type ProductionReviewMedia = { media_id: string; name: string; content_type: string; kind: "preview" | "download"; url: string };
export type ProductionReviewItem = { id: string; label: string; title?: string; text?: string; description?: string; caption: string; media_previews: ProductionReviewMedia[] };

export type ProductionOperation = {
  operation_id: string;
  integration_id?: string;
  connection_id?: string;
  revision?: string;
  kind: "native" | "openapi" | "mcp";
  contract: string;
  operation: string;
  description: string;
  async?: Record<string, unknown> | null;
  requires_approval?: boolean;
  read_only?: boolean;
  schema: {
    type?: string;
    properties?: Record<string, Record<string, unknown>>;
    required?: string[];
    additionalProperties?: boolean;
  };
  run_actions?: Array<{
    label: string;
    operation: string;
    args_template?: Record<string, unknown>;
    needs_reason?: boolean;
    inputs?: Array<{ name: string; label?: string; required?: boolean }>;
  }>;
};

function query(path: string, values: Record<string, string | number | boolean | undefined>) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  const suffix = params.toString();
  return suffix ? `${path}?${suffix}` : path;
}

const BASE = "/api/production-lines";
const json = (payload: unknown): RequestInit => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});

export const productionLinesApi = {
  lines(projectId?: string) {
    return apiRequest<{ items: ProductionLine[] }>(query(BASE, { project_id: projectId, include_shared: Boolean(projectId) }));
  },
  operations(projectId: string) {
    return apiRequest<{ items: ProductionOperation[] }>(query(`${BASE}/operations`, { project_id: projectId }));
  },
  runs(projectId?: string, limit = 200) {
    return apiRequest<{ items: ProductionRun[] }>(query(`${BASE}/runs`, { project_id: projectId, limit }));
  },
  run(runId: string) {
    return apiRequest<ProductionRun>(`${BASE}/runs/${encodeURIComponent(runId)}`);
  },
  deleteLine(lineId: string) {
    return apiRequest<void>(`${BASE}/${encodeURIComponent(lineId)}`, { method: "DELETE" });
  },
  archiveRun(runId: string) {
    return apiRequest<void>(`${BASE}/runs/${encodeURIComponent(runId)}`, { method: "DELETE" });
  },
  createLine(payload: Pick<ProductionLine, "id" | "name" | "project_id" | "enabled" | "definition">) {
    return apiRequest<ProductionLine>(BASE, { method: "POST", ...json(payload) });
  },
  updateLine(lineId: string, payload: Partial<Pick<ProductionLine, "name" | "project_id" | "enabled" | "definition">>) {
    return apiRequest<ProductionLine>(`${BASE}/${encodeURIComponent(lineId)}`, { method: "PATCH", ...json(payload) });
  },
  start(lineId: string, projectId: string, vars: Record<string, unknown> = {}) {
    return apiRequest<ProductionRun>(`${BASE}/${encodeURIComponent(lineId)}/runs`, { method: "POST", ...json({ vars, project_id: projectId }) });
  },
  retry(runId: string, stageId: string) {
    return apiRequest<ProductionRun>(`${BASE}/runs/${encodeURIComponent(runId)}/retry`, { method: "POST", ...json({ stage_id: stageId }) });
  },
  continue(runId: string, value: Record<string, unknown>) {
    return apiRequest<ProductionRun>(`${BASE}/runs/${encodeURIComponent(runId)}/continue`, { method: "POST", ...json({ value }) });
  },
  runAction(runId: string, stageId: string, actionIndex: number, inputs: Record<string, unknown>) {
    return apiRequest<{ result: Record<string, unknown> }>(`${BASE}/runs/${encodeURIComponent(runId)}/actions`, {
      method: "POST", ...json({ stage_id: stageId, action_index: actionIndex, inputs }),
    });
  },
  reviewItems(runId: string) {
    return apiRequest<{ items: ProductionReviewItem[] }>(`${BASE}/runs/${encodeURIComponent(runId)}/review/items`);
  },
  submitReview(runId: string, items: Array<{ id: string; decision: "approved" | "refused"; reason: string }>) {
    return apiRequest<{ result: Record<string, unknown>; run: ProductionRun }>(`${BASE}/runs/${encodeURIComponent(runId)}/review/submit`, {
      method: "POST", ...json({ items }),
    });
  },
  cancel(runId: string) {
    return apiRequest<ProductionRun>(`${BASE}/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
  },

};
