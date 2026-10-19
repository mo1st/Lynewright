import type {
  AiModelOption, AiProviderProfile, AiProviderProfilePayload, AiProviderTestResponse,
  AiSettingsPayload, AiSettingsRead, AIProviderTaskSettings, AITaskReadiness,
  ApplicationLogList,
} from "../types";

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";
const SESSION_KEY = "lynewright.session-token";
const TENANT_KEY = "lynewright.active-tenant";
const PROJECT_KEY = "lynewright.active-project";

export type CurrentUserTenant = { id: string; slug: string; name: string; role: string; default_project_id?: string | null };
export type CurrentUser = { id: number; email: string; role: string; is_active: boolean; synthetic?: boolean; active_tenant_id?: string | null; tenants: CurrentUserTenant[] };
export type ManagedUser = { id: number; email: string; role: string; is_active: boolean; created_at: string; updated_at: string };
export type CredentialMetadata = { name: string; kind: string | null; configured: boolean; created_at: string | null; updated_at: string | null };
export type CredentialReference = { consumer: string; label: string };
export type ManagedMedia = { id: string; name: string; content_type: string; project_id: string; created_at: string; bytes: number; integration_id: string };
export type PlatformMcpClient = { id: string; name: string; type: string; permission_mode: string; granted_capabilities: string[]; enabled: boolean; created_at: string; updated_at: string; last_authenticated_at: string | null; tenant_id: string; project_ids: string[]; on_behalf_of: string };
export type MCPOAuthRequestInfo = { request_id: string; application_name: string; redirect_origin: string; requested_scopes: string[]; default_scopes: string[]; account_email: string; expires_at: number };
export type MCPOAuthDecision = { approved: boolean; scopes: string[]; confirm_writes: boolean };
export type MCPOAuthDecisionResult = { redirect_to: string };
export type AiModelsResult = { ok: boolean; models: AiModelOption[]; error?: string };
export type AIProviderSettingsResponse = { providers: AIProviderTaskSettings[] };
export type AIReadinessResponse = { tasks: AITaskReadiness[] };

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) { super(message); this.name = "ApiError"; this.status = status; this.detail = detail; }
}
export function describeApiError(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

let activeTenantId = "";
let activeProjectId = "";
export function getSessionToken(): string { try { return window.localStorage.getItem(SESSION_KEY) ?? ""; } catch { return ""; } }
export function setSessionToken(token: string): void { try { if (token) window.localStorage.setItem(SESSION_KEY, token); else window.localStorage.removeItem(SESSION_KEY); } catch { /* private browsing keeps the in-memory session */ } }
export function getActiveTenantId(): string { return activeTenantId || (() => { try { return window.localStorage.getItem(TENANT_KEY) ?? ""; } catch { return ""; } })(); }
export function setActiveTenantId(value: string): void { activeTenantId = value.trim(); try { if (activeTenantId) window.localStorage.setItem(TENANT_KEY, activeTenantId); else window.localStorage.removeItem(TENANT_KEY); } catch { /* in-memory fallback */ } }
export function getActiveProjectId(): string { return activeProjectId || (() => { try { return window.localStorage.getItem(PROJECT_KEY) ?? ""; } catch { return ""; } })(); }
export function setActiveProjectId(value: string): void { activeProjectId = value.trim(); try { if (activeProjectId) window.localStorage.setItem(PROJECT_KEY, activeProjectId); else window.localStorage.removeItem(PROJECT_KEY); } catch { /* in-memory fallback */ } }

function requestHeaders(input?: HeadersInit): Headers {
  const headers = new Headers(input);
  const token = getSessionToken();
  if (token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  const tenant = getActiveTenantId();
  const project = getActiveProjectId();
  if (tenant && !headers.has("X-Lynewright-Tenant-ID")) headers.set("X-Lynewright-Tenant-ID", tenant);
  if (project && !headers.has("X-Lynewright-Project-ID")) headers.set("X-Lynewright-Project-ID", project);
  return headers;
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers: requestHeaders(init.headers) });
  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("json") ? await response.json().catch(() => null) : await response.text().catch(() => "");
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? (payload as Record<string, unknown>).detail : payload;
    const message = typeof detail === "string" ? detail : response.statusText || `Request failed (${response.status})`;
    throw new ApiError(response.status, message, detail);
  }
  return payload as T;
}

function query(path: string, values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined && value !== "") params.set(key, String(value));
  return params.size ? `${path}?${params.toString()}` : path;
}

export const api = {
  async login(email: string, password: string): Promise<CurrentUser> {
    const result = await apiRequest<{ token: string }>("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) });
    setSessionToken(result.token);
    const current = await api.me();
    setActiveTenantId(current.active_tenant_id ?? current.tenants[0]?.id ?? "");
    return current;
  },
  async logout(): Promise<void> { try { await apiRequest("/api/auth/logout", { method: "POST" }); } finally { setSessionToken(""); } },
  me() { return apiRequest<CurrentUser>("/api/auth/me"); },
  listUsers() { return apiRequest<ManagedUser[]>("/api/auth/users"); },
  createUser(payload: { email: string; password: string; role: string }) { return apiRequest<ManagedUser>("/api/auth/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  updateUser(id: number, payload: { email?: string; role?: string; is_active?: boolean; password?: string }) { return apiRequest<ManagedUser>(`/api/auth/users/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  deleteUser(id: number) { return apiRequest<{ id: number; email: string; deleted: boolean }>(`/api/auth/users/${id}`, { method: "DELETE" }); },
  changeMyPassword(payload: { current_password: string; new_password: string }) { return apiRequest<{ changed: boolean }>("/api/auth/me/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  changeMyEmail(payload: { new_email: string; current_password: string }) { return apiRequest<{ email: string }>("/api/auth/me/email", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  logoutAll(keepCurrent = false) { return apiRequest<void>(`/api/auth/logout-all${keepCurrent ? "?keep_current=true" : ""}`, { method: "POST" }); },
  tenants() { return apiRequest<CurrentUserTenant[]>("/api/tenants"); },
  listTenantProjects(tenantId: string) { return apiRequest<Array<{ project_id: string; tenant_id: string; name?: string }>>(`/api/tenants/${encodeURIComponent(tenantId)}/projects`); },
  getCredentials() { return apiRequest<CredentialMetadata[]>("/api/settings/credentials"); },
  getCredentialReferences(name: string) { return apiRequest<CredentialReference[]>(`/api/settings/credentials/${encodeURIComponent(name)}/references`); },
  saveCredential(payload: { name: string; kind?: string; secret: string }) { return apiRequest<CredentialMetadata>("/api/settings/credentials", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  deleteCredential(name: string) { return apiRequest<{ name: string; deleted: boolean }>(`/api/settings/credentials/${encodeURIComponent(name)}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm_name: name }) }); },
  listManagedMedia() { return apiRequest<{ items: ManagedMedia[] }>("/api/media"); },
  listPlatformMcpClients() { return apiRequest<{ clients: PlatformMcpClient[]; mcp_url: string }>("/api/platform/mcp-clients"); },
  createPlatformMcpClient(payload: { name: string; client_type: "mcp"; permission_mode: "custom"; granted_capabilities: string[] }) { return apiRequest<PlatformMcpClient & { access_token: string }>("/api/platform/mcp-clients", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  deletePlatformMcpClient(id: string) { return apiRequest<void>(`/api/platform/mcp-clients/${encodeURIComponent(id)}`, { method: "DELETE" }); },
  deleteManagedMedia(id: string) { return apiRequest<{ id: string; deleted: boolean }>(`/api/media/${encodeURIComponent(id)}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm_media_id: id }) }); },
  getApplicationLogs(params: { level?: string; category?: string; search?: string } = {}) {
    return apiRequest<ApplicationLogList>(query("/api/logs", { ...params, project_id: getActiveProjectId(), limit: 100 }));
  },
  clearApplicationLogs() { return apiRequest<void>(query("/api/logs", { project_id: getActiveProjectId() }), { method: "DELETE" }); },
  reportFrontendLog(payload: { level: string; category: string; message: string; details?: Record<string, unknown>; request_path?: string; correlation_id?: string }) {
    if (!getActiveTenantId()) return Promise.resolve(undefined);
    return apiRequest<{ accepted: boolean }>("/api/logs/frontend", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  },
  getAiSettings() { return apiRequest<AiSettingsRead>("/api/settings/ai"); },
  saveAiSettings(payload: AiSettingsPayload) { return apiRequest<AiSettingsRead>("/api/settings/ai", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  testAiSettings(payload: AiSettingsPayload) { return apiRequest<AiProviderTestResponse>("/api/settings/ai/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  createAiProviderProfile(payload: AiProviderProfilePayload) { return apiRequest<AiProviderProfile>("/api/settings/ai/providers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  updateAiProviderProfile(id: number, payload: AiProviderProfilePayload) { return apiRequest<AiProviderProfile>(`/api/settings/ai/providers/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  deleteAiProviderProfile(id: number) { return apiRequest<void>(`/api/settings/ai/providers/${id}`, { method: "DELETE" }); },
  testAiProviderProfile(id: number) { return apiRequest<AiProviderTestResponse>(`/api/settings/ai/providers/${id}/test`, { method: "POST" }); },
  getAiModels(payload: AiSettingsPayload) { return apiRequest<AiModelsResult>("/api/settings/ai/models", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  getAiProviderProfileModels(id: number) { return apiRequest<AiModelsResult>(`/api/settings/ai/providers/${id}/models`, { method: "POST" }); },
  getAIProviderSettings() { return apiRequest<AIProviderSettingsResponse>("/api/ai/providers"); },
  updateAIProviderSettings(taskType: string, payload: Record<string, unknown>) { return apiRequest<AIProviderTaskSettings>(`/api/ai/providers/${encodeURIComponent(taskType)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); },
  getAIReadiness() { return apiRequest<AIReadinessResponse>("/api/ai/readiness"); },
  getMcpOAuthRequest(id: string) { return apiRequest<MCPOAuthRequestInfo>(`/api/mcp-oauth/requests/${encodeURIComponent(id)}`); },
  decideMcpOAuthRequest(id: string, decision: MCPOAuthDecision) { return apiRequest<MCPOAuthDecisionResult>(`/api/mcp-oauth/requests/${encodeURIComponent(id)}/decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(decision) }); },
};
