import { apiRequest } from "./client";
import type {
  InteractionConnection,
  InteractionConnectionCreated,
  InteractionLink,
  InteractionLinkToken,
  LinkTokenStatus,
  TelegramSettings,
  NotificationRoute,
  NotificationRouteChannel,
  NotificationSendResult,
  OperatorApproval,
  OperatorAutomation,
  OperatorAutomationInput,
  OperatorProfile,
  OperatorProfileInput,
  OperatorDecisionModel,
  OperatorRun,
  OperatorRunStep,
  OperatorWorkflow,
} from "../types";

/**
 * Every Operator and Interaction/Telegram endpoint the product UI consumes, in
 * one place. The Operator management API is being built in parallel (plan
 * §10.1–10.7 is the frozen contract); if a path or field deviates at
 * integration, this file is the only place that needs to change.
 *
 * List endpoints are normalised through `asList` because the plan freezes the
 * routes but not the envelope shape: the repo mixes bare arrays
 * (`GET /api/interactions/...`) and keyed objects (`{ providers }`). Both are
 * accepted so a one-line integration fix stays a one-line fix.
 */

const OPERATOR_BASE = "/api/operator";
const INTERACTIONS_BASE = "/api/interactions";

function asList<T>(payload: unknown, keys: string[]): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of [...keys, "items"]) {
      if (Array.isArray(record[key])) return record[key] as T[];
    }
  }
  return [];
}

function unwrap<T>(payload: unknown, key: string): T {
  if (payload && typeof payload === "object" && key in (payload as Record<string, unknown>)) {
    return (payload as Record<string, unknown>)[key] as T;
  }
  return payload as T;
}

function withQuery(path: string, params: Record<string, string | number | undefined>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  const suffix = query.toString();
  return suffix ? `${path}?${suffix}` : path;
}

// ------------------------------------------------------------------ profiles

export const operatorApi = {
  getDecisionModel() {
    return apiRequest<OperatorDecisionModel>(`${OPERATOR_BASE}/decision-model`);
  },
  listProfiles(projectId?: string) {
    return apiRequest<unknown>(withQuery(`${OPERATOR_BASE}/profiles`, { project_id: projectId }))
      .then((payload) => asList<OperatorProfile>(payload, ["profiles"]));
  },
  getProfile(profileId: string) {
    return apiRequest<OperatorProfile>(`${OPERATOR_BASE}/profiles/${encodeURIComponent(profileId)}`);
  },
  createProfile(payload: OperatorProfileInput) {
    return apiRequest<OperatorProfile>(`${OPERATOR_BASE}/profiles`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  updateProfile(profileId: string, patch: Partial<OperatorProfileInput>) {
    return apiRequest<OperatorProfile>(`${OPERATOR_BASE}/profiles/${encodeURIComponent(profileId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
  },
  deleteProfile(profileId: string) {
    return apiRequest<OperatorProfile>(`${OPERATOR_BASE}/profiles/${encodeURIComponent(profileId)}`, { method: "DELETE" });
  },
  /** Idempotent first-time setup of a conservative default profile (§10.2).
   *  `project_id` is required by OperatorBootstrapRequest — no default exists,
   *  so an empty body would fail validation with 422. */
  bootstrap(projectId: string) {
    return apiRequest<unknown>(`${OPERATOR_BASE}/bootstrap`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId }),
    }).then((payload) => unwrap<OperatorProfile>(payload, "profile"));
  },

  // ------------------------------------------------------------------- runs

  listRuns(limit = 20, projectId?: string) {
    return apiRequest<unknown>(
      withQuery(`${OPERATOR_BASE}/runs`, { limit, project_id: projectId }),
    ).then((payload) => asList<OperatorRun>(payload, ["runs"]));
  },
  clearRuns(projectId: string) {
    return apiRequest<{ cleared: number }>(
      withQuery(`${OPERATOR_BASE}/runs`, { project_id: projectId }),
      { method: "DELETE" },
    );
  },
  getRun(runId: string) {
    return apiRequest<OperatorRun>(`${OPERATOR_BASE}/runs/${encodeURIComponent(runId)}`);
  },
  /** Manual objective launch (§10.3): profile + project + objective + safe context. */
  startRun(payload: {
    profile_id: string;
    project_id: string;
    objective: string;
    context?: Record<string, unknown>;
    idempotency_key?: string;
    execution_mode?: "sync" | "queued";
    workflow_id?: string;
  }) {
    return apiRequest<OperatorRun>(`${OPERATOR_BASE}/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  resumeRun(runId: string) {
    return apiRequest<OperatorRun>(`${OPERATOR_BASE}/runs/${encodeURIComponent(runId)}/resume`, {
      method: "POST",
    });
  },
  cancelRun(runId: string) {
    return apiRequest<OperatorRun>(`${OPERATOR_BASE}/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
    });
  },
  listRunSteps(runId: string) {
    return apiRequest<unknown>(`${OPERATOR_BASE}/runs/${encodeURIComponent(runId)}/steps`)
      .then((payload) => asList<OperatorRunStep>(payload, ["steps"]));
  },
  listWorkflows(projectId?: string, status?: string) {
    return apiRequest<unknown>(withQuery(`${OPERATOR_BASE}/workflows`, { project_id: projectId, status }))
      .then((payload) => asList<OperatorWorkflow>(payload, ["workflows"]));
  },
  createWorkflow(payload: { project_id: string; name: string; objective_template: string; plan: Record<string, unknown>; contract_revisions?: Record<string, unknown> }) {
    return apiRequest<OperatorWorkflow>(`${OPERATOR_BASE}/workflows`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  },
  reviewWorkflow(workflowId: string, approve = true, reviewer = "") {
    return apiRequest<OperatorWorkflow>(`${OPERATOR_BASE}/workflows/${encodeURIComponent(workflowId)}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approve, reviewer }) });
  },
  enableWorkflow(workflowId: string) {
    return apiRequest<OperatorWorkflow>(`${OPERATOR_BASE}/workflows/${encodeURIComponent(workflowId)}/enable`, { method: "POST" });
  },

  // -------------------------------------------------------------- approvals

  listApprovals(status?: string, projectId?: string) {
    return apiRequest<unknown>(
      withQuery(`${OPERATOR_BASE}/approvals`, { status, project_id: projectId }),
    ).then((payload) => asList<OperatorApproval>(payload, ["approvals"]));
  },
  /** Decision vocabulary matches the runtime store: approved | denied. */
  resolveApproval(approvalId: string, decision: "approved" | "denied", note = "") {
    return apiRequest<OperatorApproval>(
      `${OPERATOR_BASE}/approvals/${encodeURIComponent(approvalId)}/resolve`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, note }),
      },
    );
  },

  // ------------------------------------------------------------- automations

  listAutomations(projectId?: string) {
    return apiRequest<unknown>(
      withQuery(`${OPERATOR_BASE}/automations`, { project_id: projectId }),
    ).then((payload) => asList<OperatorAutomation>(payload, ["automations"]));
  },
  createAutomation(payload: OperatorAutomationInput) {
    return apiRequest<OperatorAutomation>(`${OPERATOR_BASE}/automations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  updateAutomation(automationId: string, patch: Partial<OperatorAutomationInput>) {
    return apiRequest<OperatorAutomation>(
      `${OPERATOR_BASE}/automations/${encodeURIComponent(automationId)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      },
    );
  },
  deleteAutomation(automationId: string) {
    return apiRequest<void>(`${OPERATOR_BASE}/automations/${encodeURIComponent(automationId)}`, {
      method: "DELETE",
    });
  },
};

// ------------------------------------------- interaction gateway / Telegram
// These routes already exist (backend/app/routers/interactions.py).

export type InteractionApi = {
  listConnections(projectId?: string): Promise<InteractionConnection[]>;
  createConnection(payload: {
    channel_type?: "telegram";
    project_id: string;
    name: string;
    bot_token: string;
    operator_profile_id?: string;
  }): Promise<InteractionConnectionCreated>;
  updateConnection(
    connectionId: string,
    patch: { name?: string; enabled?: boolean; bot_token?: string; operator_profile_id?: string },
  ): Promise<InteractionConnection>;
  verifyConnection(connectionId: string): Promise<InteractionConnection>;
  rotateWebhookSecret(connectionId: string): Promise<{ id: string; webhook_secret: string }>;
  deleteConnection(connectionId: string): Promise<void>;
  setDeliveryMode(connectionId: string, mode: "polling" | "webhook"): Promise<InteractionConnection>;
  telegramSettings(): Promise<TelegramSettings>;
  linkTokenStatus(tokenId: string): Promise<LinkTokenStatus>;
  createLinkToken(payload: {
    connection_id: string;
    project_id: string;
    ttl_seconds?: number;
  }): Promise<InteractionLinkToken>;
  listLinks(connectionId?: string): Promise<InteractionLink[]>;
  revokeLink(linkId: string): Promise<InteractionLink>;
  listNotificationRoutes(projectId?: string): Promise<NotificationRoute[]>;
  upsertNotificationRoute(payload: {
    project_id: string;
    event_class: string;
    intent: string;
    channels: NotificationRouteChannel[];
    enabled: boolean;
  }): Promise<NotificationRoute>;
  deleteNotificationRoute(eventClass: string, projectId: string): Promise<void>;
  sendTestNotification(payload: {
    project_id: string;
    event_class: string;
    title: string;
    body?: string;
    intent?: string;
  }): Promise<NotificationSendResult>;
};

export const interactionApi: InteractionApi = {
  async listConnections(projectId) {
    const payload = await apiRequest<unknown>(
      withQuery(`${INTERACTIONS_BASE}/connections`, { project_id: projectId }),
    );
    return asList<InteractionConnection>(payload, ["connections"]);
  },
  createConnection(payload) {
    return apiRequest<InteractionConnectionCreated>(`${INTERACTIONS_BASE}/connections`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_type: "telegram", ...payload }),
    });
  },
  updateConnection(connectionId, patch) {
    return apiRequest<InteractionConnection>(
      `${INTERACTIONS_BASE}/connections/${encodeURIComponent(connectionId)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      },
    );
  },
  verifyConnection(connectionId) {
    return apiRequest<InteractionConnection>(
      `${INTERACTIONS_BASE}/connections/${encodeURIComponent(connectionId)}/verify`,
      { method: "POST" },
    );
  },
  rotateWebhookSecret(connectionId) {
    return apiRequest<{ id: string; webhook_secret: string }>(
      `${INTERACTIONS_BASE}/connections/${encodeURIComponent(connectionId)}/rotate-webhook-secret`,
      { method: "POST" },
    );
  },
  async deleteConnection(connectionId) {
    await apiRequest<void>(`${INTERACTIONS_BASE}/connections/${encodeURIComponent(connectionId)}`, {
      method: "DELETE",
    });
  },
  setDeliveryMode(connectionId, mode) {
    return apiRequest<InteractionConnection>(
      `${INTERACTIONS_BASE}/connections/${encodeURIComponent(connectionId)}/delivery`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) },
    );
  },
  telegramSettings() {
    return apiRequest<TelegramSettings>(`${INTERACTIONS_BASE}/telegram/settings`);
  },
  linkTokenStatus(tokenId) {
    return apiRequest<LinkTokenStatus>(`${INTERACTIONS_BASE}/links/token/${encodeURIComponent(tokenId)}`);
  },
  createLinkToken(payload) {
    return apiRequest<InteractionLinkToken>(`${INTERACTIONS_BASE}/links/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  async listLinks(connectionId) {
    const payload = await apiRequest<unknown>(
      withQuery(`${INTERACTIONS_BASE}/links`, { connection_id: connectionId }),
    );
    return asList<InteractionLink>(payload, ["links"]);
  },
  revokeLink(linkId) {
    return apiRequest<InteractionLink>(`${INTERACTIONS_BASE}/links/${encodeURIComponent(linkId)}`, {
      method: "DELETE",
    });
  },
  async listNotificationRoutes(projectId) {
    const payload = await apiRequest<unknown>(
      withQuery(`${INTERACTIONS_BASE}/notification-routes`, { project_id: projectId }),
    );
    return asList<NotificationRoute>(payload, ["routes"]);
  },
  upsertNotificationRoute(payload) {
    return apiRequest<NotificationRoute>(`${INTERACTIONS_BASE}/notification-routes`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  async deleteNotificationRoute(eventClass, projectId) {
    await apiRequest<void>(
      withQuery(
        `${INTERACTIONS_BASE}/notification-routes/${encodeURIComponent(eventClass)}`,
        { project_id: projectId },
      ),
      { method: "DELETE" },
    );
  },
  sendTestNotification(payload) {
    return apiRequest<NotificationSendResult>(`${INTERACTIONS_BASE}/notifications/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
};
