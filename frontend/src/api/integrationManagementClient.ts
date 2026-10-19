import { apiRequest } from "./client";
import type {
  IntegrationExternalResource,
  IntegrationAttentionItem,
  IntegrationInboundHandler,
  IntegrationManagementSummary,
  IntegrationPackageStatus,
  IntegrationManagementConnection,
  IntegrationMcpTool,
  IntegrationMcpContractMapping,
  IntegrationMcpContractTarget,
  IntegrationOpenApiContractMapping,
  IntegrationOAuthStatus,
  IntegrationOpenApiOperation,
  IntegrationOpenApiResourceMapping,
  IntegrationMcpResourceMapping,
  IntegrationMappingPreview,
  IntegrationMcpToolProbeResult,
  IntegrationMcpToolSchemaResult,
  IntegrationSettingsModel,
  IntegrationResourceActionResult,
  IntegrationInstallConfirmation,
  IntegrationInstallSummary,
} from "../types";


export type IntegrationDraft = {
  id: string; tenant_id: string; project_id: string; source_kind: "openapi_url" | "mcp_endpoint" | "github_repo" | "pasted_docs";
  source_ref: string; source_message: string; draft_kind: "integration_file" | "cli_command";
  draft_json: Record<string, unknown>; validation_errors: string[]; status: "draft" | "probed" | "enabled" | "discarded";
  probe_result: Record<string, unknown>; probe_operation_id: string; probe_hash_locked: boolean;
  created_by: string; created_at: string; updated_at: string; enabled_integration_id: string;
};

const BASE = "/api/integration-management";

function query(path: string, values: Record<string, string | number | undefined>) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  const suffix = params.toString();
  return suffix ? `${path}?${suffix}` : path;
}

type McpResourceMappingPayload = Omit<IntegrationMcpResourceMapping, "mapping_id"> & { mapping_id?: string };

export type IntegrationRemovalPreview = {
  integration_id: string; name: string; bundled: boolean; blocked: boolean; blocked_reasons: string[];
  disable_lines_blocked_reasons: string[];
  lines: Array<{ id: string; name: string; project_id: string; enabled: boolean; operations: string[] }>;
  connections: Array<{ id: string; name: string }>;
  mappings: Record<string, number>; runner_tokens: number;
  stored_wheel: { filename: string; directory: string } | null;
  staging_files: string[];
  media: Array<{ id: string; name: string; content_type: string; bytes: number }>;
  settings: boolean; settings_fields: string[]; credentials: { used: string[]; exclusive: string[]; shared: string[] };
  listed_package: boolean; distribution?: string; runner_instruction?: string;
};

export const integrationManagementApi = {
  createDraft(payload: { source_kind: IntegrationDraft["source_kind"]; project_id: string; url?: string; text?: string; internal_endpoint_ref?: string; credential_refs?: string[] }) {
    return apiRequest<IntegrationDraft>(`${BASE}/drafts`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  },
  drafts(projectId: string) {
    return apiRequest<{ items: IntegrationDraft[] }>(query(`${BASE}/drafts`, { project_id: projectId }));
  },
  getDraft(draftId: string, projectId: string) {
    return apiRequest<IntegrationDraft>(query(`${BASE}/drafts/${encodeURIComponent(draftId)}`, { project_id: projectId }));
  },
  updateDraft(draftId: string, projectId: string, draft: Record<string, unknown>) {
    return apiRequest<IntegrationDraft>(`${BASE}/drafts/${encodeURIComponent(draftId)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: projectId, draft_json: draft }) });
  },
  probeDraft(draftId: string, projectId: string, operationId: string, args: Record<string, unknown>) {
    return apiRequest<IntegrationDraft>(`${BASE}/drafts/${encodeURIComponent(draftId)}/probe`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: projectId, operation_id: operationId, args }) });
  },
  enableDraft(draftId: string, projectId: string, confirm?: IntegrationInstallConfirmation) {
    return apiRequest<IntegrationDraft>(`${BASE}/drafts/${encodeURIComponent(draftId)}/enable`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: projectId, ...(confirm ? { confirm } : {}) }) });
  },
  discardDraft(draftId: string, projectId: string) {
    return apiRequest<IntegrationDraft>(`${BASE}/drafts/${encodeURIComponent(draftId)}/discard`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: projectId }) });
  },
  downloadDraft(draftId: string, projectId: string) {
    return apiRequest<string>(query(`${BASE}/drafts/${encodeURIComponent(draftId)}/download`, { project_id: projectId }));
  },
  importFile(file: File, confirm?: IntegrationInstallConfirmation) {
    const body = new FormData();
    body.append("file", file);
    const headers = new Headers();
    if (confirm) headers.set("X-Lynewright-Install-Confirm", JSON.stringify(confirm));
    return apiRequest<{ integration: IntegrationManagementSummary; created: boolean; idempotent: boolean; approval_message: string | null }>(`${BASE}/import`, {
      method: "POST",
      headers,
      body,
    });
  },
  exportFile(integrationId: string) {
    return apiRequest<string>(`${BASE}/${encodeURIComponent(integrationId)}/export`);
  },
  list() {
    return apiRequest<{ items: IntegrationManagementSummary[]; package_statuses: IntegrationPackageStatus[]; install_policy?: "catalogue-only" | "confirm" | "open" }>(BASE);
  },
  get(integrationId: string) {
    return apiRequest<IntegrationManagementSummary>(`${BASE}/${encodeURIComponent(integrationId)}`);
  },
  resources(integrationId: string, options: { connectionId?: string; resourceType?: string; search?: string } = {}) {
    return apiRequest<{ items: IntegrationExternalResource[]; next_cursor: string | null }>(
      query(`${BASE}/${encodeURIComponent(integrationId)}/resources`, {
        connection_id: options.connectionId,
        resource_type: options.resourceType,
        search: options.search,
      }),
    );
  },
  attention(integrationId: string) {
    return apiRequest<{ items: IntegrationAttentionItem[] }>(`${BASE}/${encodeURIComponent(integrationId)}/attention`);
  },
  bindings(integrationId: string) {
    return apiRequest<{ items: Record<string, unknown>[] }>(`${BASE}/${encodeURIComponent(integrationId)}/bindings`);
  },
  setupRequirements(integrationId: string) {
    return apiRequest<{ items: Array<Record<string, unknown>> }>(`${BASE}/${encodeURIComponent(integrationId)}/setup-requirements`);
  },
  webhooks(integrationId: string) {
    return apiRequest<{ items: IntegrationInboundHandler[] }>(`${BASE}/${encodeURIComponent(integrationId)}/webhooks`);
  },
  oauthStatus(integrationId: string, connectionId: string) {
    return apiRequest<IntegrationOAuthStatus>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/oauth`);
  },
  oauthBegin(integrationId: string, connectionId: string, redirectUri: string) {
    return apiRequest<{ authorization_url: string; state: string; expires_at: number }>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/oauth/begin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ redirect_uri: redirectUri }),
    });
  },
  oauthCallback(integrationId: string, connectionId: string, payload: {
    state: string;
    code: string;
    access_credential_ref: string;
    refresh_credential_ref?: string;
  }) {
    return apiRequest<IntegrationOAuthStatus>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/oauth/callback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  oauthRefresh(integrationId: string, connectionId: string) {
    return apiRequest<IntegrationOAuthStatus>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/oauth/refresh`, { method: "POST" });
  },
  createConnection(integrationId: string, payload: {
    name: string;
    endpoint_url?: string;
    credential_refs?: string[];
    project_id?: string;
    enabled?: boolean;
    origin_kind?: "public" | "internal_endpoint";
    internal_endpoint_ref?: string;
  }) {
    return apiRequest(`${BASE}/${encodeURIComponent(integrationId)}/connections`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  configureConnection(integrationId: string, connectionId: string, payload: {
    endpoint_url?: string;
    credential_refs?: string[];
    project_id?: string;
  }) {
    return apiRequest(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  enableConnection(integrationId: string, connectionId: string) {
    return apiRequest(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/enable`, { method: "POST" });
  },
  disableConnection(integrationId: string, connectionId: string) {
    return apiRequest(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/disable`, { method: "POST" });
  },
  testConnection(integrationId: string, connectionId: string) {
    return apiRequest<Record<string, unknown>>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/test`, { method: "POST" });
  },
  removeConnection(integrationId: string, connectionId: string) {
    return apiRequest<void>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}`, { method: "DELETE" });
  },
  discover(integrationId: string, connectionId: string) {
    return apiRequest<{ resources: IntegrationExternalResource[]; tools?: IntegrationMcpTool[] }>(
      `${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/discover`,
      { method: "POST" },
    );
  },
  enable(integrationId: string) {
    return apiRequest<IntegrationManagementSummary>(`${BASE}/${encodeURIComponent(integrationId)}/enable`, { method: "POST" });
  },
  disable(integrationId: string) {
    return apiRequest<IntegrationManagementSummary>(`${BASE}/${encodeURIComponent(integrationId)}/disable`, { method: "POST" });
  },
  removalPreview(integrationId: string) {
    return apiRequest<IntegrationRemovalPreview>(`${BASE}/${encodeURIComponent(integrationId)}/removal-preview`);
  },
  remove(integrationId: string, payload: { confirm_name: string; disable_using_lines: boolean; delete_exclusive_credentials: boolean }) {
    return apiRequest<{ removed: boolean; runner_instruction: string; listed_package_note?: string | null }>(`${BASE}/${encodeURIComponent(integrationId)}`, {
      method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  reconcile(integrationId: string, maxItems = 5) {
    return apiRequest(`${BASE}/${encodeURIComponent(integrationId)}/reconcile?max_items=${maxItems}`, { method: "POST" });
  },
  resourceAction(integrationId: string, inventoryId: string, payload: {
    action: string;
    arguments?: Record<string, unknown>;
    confirm_destructive?: boolean;
    expected_revision?: number;
    idempotency_key?: string;
  }) {
    return apiRequest<IntegrationResourceActionResult>(`${BASE}/${encodeURIComponent(integrationId)}/resources/${encodeURIComponent(inventoryId)}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  createResource(integrationId: string, connectionId: string, resourceType: string, arguments_: Record<string, unknown>) {
    return apiRequest<IntegrationResourceActionResult>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/resources/${encodeURIComponent(resourceType)}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "create", arguments: arguments_ }),
    });
  },
  createManual(payload: {
    name: string;
    runtime_kind: "mcp" | "openapi" | "webhook";
    endpoint_url: string;
    document?: Record<string, unknown> | string;
    credential_refs?: string[];
    verification?: Record<string, unknown>;
    event_type?: string;
    payload_mapping?: Record<string, string>;
  }, confirm?: IntegrationInstallConfirmation) {
    const headers = new Headers({ "Content-Type": "application/json" });
    if (confirm) headers.set("X-Lynewright-Install-Confirm", JSON.stringify(confirm));
    return apiRequest<IntegrationManagementSummary>(`${BASE}/manual`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
  },
  confirmPackageInstall(integrationId: string, confirm?: IntegrationInstallConfirmation) {
    return apiRequest<IntegrationManagementSummary>(`${BASE}/${encodeURIComponent(integrationId)}/confirm-install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(confirm ? { confirm } : {}),
    });
  },
  manualCapabilities() {
    return apiRequest<{ items: string[] }>(`${BASE}/manual-capabilities`);
  },
  operations(projectId: string, search = "") {
    return apiRequest<{ items: Array<Record<string, unknown>> }>(query(`${BASE}/operations`, { project_id: projectId, query: search }));
  },
  createRunnerToken(integrationId: string) {
    return apiRequest<{ token: string; message: string; runner_id?: string }>(`${BASE}/${encodeURIComponent(integrationId)}/runner-token`, { method: "POST" });
  },
  revokeRunnerToken(integrationId: string) {
    return apiRequest<{ integration_id: string; revoked: boolean }>(`${BASE}/${encodeURIComponent(integrationId)}/runner-token`, { method: "DELETE" });
  },
  runtimeConfig(integrationId: string, format: "compose" | "quadlet" = "compose") {
    return apiRequest<{ format: string; content: string; network_file?: string; network_content?: string; warnings?: string[]; egress?: string; limits?: Record<string, unknown> }>(`${BASE}/${encodeURIComponent(integrationId)}/runtime-config?format=${format}`);
  },
  describeOperation(operationId: string, projectId: string) {
    return apiRequest<Record<string, unknown>>(query(`${BASE}/operations/${encodeURIComponent(operationId)}`, { project_id: projectId }));
  },
  prepareOperation(payload: { project_id: string; operation_id: string; arguments?: Record<string, unknown> }) {
    return apiRequest<Record<string, unknown>>(`${BASE}/operations/prepare`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  invokeOperation(payload: { project_id: string; operation_id: string; arguments?: Record<string, unknown>; prepared_invocation_id?: string; idempotency_key?: string }) {
    return apiRequest<Record<string, unknown>>(`${BASE}/operations/invoke`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  getExecution(executionId: string, projectId: string) {
    return apiRequest<Record<string, unknown>>(query(`${BASE}/operations/executions/${encodeURIComponent(executionId)}`, { project_id: projectId }));
  },
  cancelExecution(executionId: string, projectId: string) {
    return apiRequest<Record<string, unknown>>(`${BASE}/operations/executions/${encodeURIComponent(executionId)}/cancel`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: projectId }),
    });
  },
  openApiOperations(integrationId: string, connectionId?: string) {
    return apiRequest<{ items: IntegrationOpenApiOperation[] }>(query(`${BASE}/${encodeURIComponent(integrationId)}/openapi/operations`, { connection_id: connectionId }));
  },
  openApiResourceMappings(integrationId: string) {
    return apiRequest<{ items: IntegrationOpenApiResourceMapping[] }>(`${BASE}/${encodeURIComponent(integrationId)}/openapi/resource-mappings`);
  },
  previewOpenApiResourceMapping(integrationId: string, connectionId: string, payload: Omit<IntegrationOpenApiResourceMapping, "mapping_id"> & { mapping_id?: string }, invoke = false) {
    return apiRequest<IntegrationOpenApiResourceMapping | IntegrationMappingPreview>(query(`${BASE}/${encodeURIComponent(integrationId)}/openapi/resource-mappings/preview`, { connection_id: connectionId, invoke: invoke ? "true" : undefined }), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  createOpenApiResourceMapping(integrationId: string, payload: Omit<IntegrationOpenApiResourceMapping, "mapping_id"> & { mapping_id?: string }) {
    return apiRequest<IntegrationOpenApiResourceMapping>(`${BASE}/${encodeURIComponent(integrationId)}/openapi/resource-mappings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  updateOpenApiResourceMapping(integrationId: string, mappingId: string, payload: Omit<IntegrationOpenApiResourceMapping, "mapping_id"> & { mapping_id?: string }) {
    return apiRequest<IntegrationOpenApiResourceMapping>(`${BASE}/${encodeURIComponent(integrationId)}/openapi/resource-mappings/${encodeURIComponent(mappingId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  deleteOpenApiResourceMapping(integrationId: string, mappingId: string) {
    return apiRequest<void>(`${BASE}/${encodeURIComponent(integrationId)}/openapi/resource-mappings/${encodeURIComponent(mappingId)}`, { method: "DELETE" });
  },
  openApiContractMappings(integrationId: string, connectionId: string) {
    return apiRequest<{ items: IntegrationOpenApiContractMapping[] }>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/openapi/contract-mappings`);
  },
  createOpenApiContractMapping(integrationId: string, connectionId: string, payload: Omit<IntegrationOpenApiContractMapping, "mapping_id"> & { mapping_id?: string }) {
    return apiRequest<IntegrationOpenApiContractMapping>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/openapi/contract-mappings`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  updateOpenApiContractMapping(integrationId: string, connectionId: string, mappingId: string, payload: Omit<IntegrationOpenApiContractMapping, "mapping_id"> & { mapping_id?: string }) {
    return apiRequest<IntegrationOpenApiContractMapping>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/openapi/contract-mappings/${encodeURIComponent(mappingId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  deleteOpenApiContractMapping(integrationId: string, connectionId: string, mappingId: string) {
    return apiRequest<void>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/openapi/contract-mappings/${encodeURIComponent(mappingId)}`, { method: "DELETE" });
  },
  executeOpenApiContractMapping(integrationId: string, connectionId: string, mappingId: string, arguments_: Record<string, unknown> = {}, idempotencyKey?: string) {
    return apiRequest<Record<string, unknown>>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/openapi/contract-mappings/${encodeURIComponent(mappingId)}/execute`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ arguments: arguments_, ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}) }),
    });
  },
  approveMcpTools(integrationId: string, connectionId: string, toolNames: string[]) {
    return apiRequest(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/mcp/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool_names: toolNames }),
    });
  },
  probeMcpTool(integrationId: string, connectionId: string, toolName: string, arguments_: Record<string, unknown> = {}) {
    return apiRequest<IntegrationMcpToolProbeResult>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/mcp/tools/${encodeURIComponent(toolName)}/probe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ arguments: arguments_ }),
    });
  },
  mcpToolInputSchema(integrationId: string, connectionId: string, toolName: string) {
    return apiRequest<IntegrationMcpToolSchemaResult>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/mcp/tools/${encodeURIComponent(toolName)}/schema`);
  },
  mcpContractTargets(integrationId: string) {
    return apiRequest<{ items: IntegrationMcpContractTarget[] }>(`${BASE}/${encodeURIComponent(integrationId)}/mcp/contract-targets`);
  },
  mcpContractMappings(integrationId: string, connectionId: string) {
    return apiRequest<{ items: IntegrationMcpContractMapping[] }>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/mcp/contract-mappings`);
  },
  createMcpContractMapping(integrationId: string, connectionId: string, payload: Omit<IntegrationMcpContractMapping, "mapping_id"> & { mapping_id?: string }) {
    return apiRequest<IntegrationMcpContractMapping>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/mcp/contract-mappings`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  updateMcpContractMapping(integrationId: string, connectionId: string, mappingId: string, payload: Omit<IntegrationMcpContractMapping, "mapping_id">) {
    return apiRequest<IntegrationMcpContractMapping>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/mcp/contract-mappings/${encodeURIComponent(mappingId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  deleteMcpContractMapping(integrationId: string, connectionId: string, mappingId: string) {
    return apiRequest<void>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/mcp/contract-mappings/${encodeURIComponent(mappingId)}`, { method: "DELETE" });
  },
  executeMcpContractMapping(integrationId: string, connectionId: string, mappingId: string, arguments_: Record<string, unknown> = {}, idempotencyKey?: string, confirmDestructive = false) {
    return apiRequest<Record<string, unknown>>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/mcp/contract-mappings/${encodeURIComponent(mappingId)}/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ arguments: arguments_, ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}), confirm_destructive: confirmDestructive }),
    });
  },
  mcpResourceMappings(integrationId: string, connectionId: string) {
    return apiRequest<{ items: IntegrationMcpResourceMapping[] }>(query(`${BASE}/${encodeURIComponent(integrationId)}/mcp/resource-mappings`, { connection_id: connectionId }));
  },
  previewMcpResourceMapping(integrationId: string, connectionId: string, payload: McpResourceMappingPayload, invoke = false) {
    return apiRequest<IntegrationMcpResourceMapping | IntegrationMappingPreview>(query(`${BASE}/${encodeURIComponent(integrationId)}/mcp/resource-mappings/preview`, { connection_id: connectionId, invoke: invoke ? "true" : undefined }), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  createMcpResourceMapping(integrationId: string, connectionId: string, payload: McpResourceMappingPayload) {
    return apiRequest<IntegrationMcpResourceMapping>(query(`${BASE}/${encodeURIComponent(integrationId)}/mcp/resource-mappings`, { connection_id: connectionId }), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  updateMcpResourceMapping(integrationId: string, connectionId: string, mappingId: string, payload: McpResourceMappingPayload) {
    return apiRequest<IntegrationMcpResourceMapping>(query(`${BASE}/${encodeURIComponent(integrationId)}/mcp/resource-mappings/${encodeURIComponent(mappingId)}`, { connection_id: connectionId }), {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  },
  deleteMcpResourceMapping(integrationId: string, connectionId: string, mappingId: string) {
    return apiRequest<void>(query(`${BASE}/${encodeURIComponent(integrationId)}/mcp/resource-mappings/${encodeURIComponent(mappingId)}`, { connection_id: connectionId }), { method: "DELETE" });
  },
  approveOpenApiOperations(integrationId: string, connectionId: string, operationIds: string[]) {
    return apiRequest(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/openapi/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operation_ids: operationIds }),
    });
  },
  executeOpenApi(integrationId: string, connectionId: string, operationId: string, arguments_: Record<string, unknown> = {}, confirmDestructive = false) {
    return apiRequest<unknown>(`${BASE}/${encodeURIComponent(integrationId)}/connections/${encodeURIComponent(connectionId)}/openapi/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operation_id: operationId, arguments: arguments_, confirm_destructive: confirmDestructive }),
    });
  },
  bind(inventoryId: string, payload: { project_id?: string; resource_type: string; resource_id: string; role: string; ownership_mode: "managed" | "linked" | "observed" }) {
    return apiRequest(`${BASE}/resources/${encodeURIComponent(inventoryId)}/bind`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },
  unbind(bindingId: string, expectedRevision: number) {
    return apiRequest(`${BASE}/bindings/${encodeURIComponent(bindingId)}/unbind`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: expectedRevision }),
    });
  },
  repairBinding(integrationId: string, bindingId: string, reason: string) {
    return apiRequest(`${BASE}/${encodeURIComponent(integrationId)}/bindings/${encodeURIComponent(bindingId)}/repair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    });
  },
  verifySetup(integrationId: string, bindingId: string, requirementId: string, evidence: Record<string, unknown> = {}) {
    return apiRequest(`${BASE}/${encodeURIComponent(integrationId)}/bindings/${encodeURIComponent(bindingId)}/setup/${encodeURIComponent(requirementId)}/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ evidence }),
    });
  },
  connections(integrationId: string) {
    return apiRequest<{ items: IntegrationManagementConnection[] }>(`${BASE}/${encodeURIComponent(integrationId)}/connections`);
  },
  health(integrationId: string) {
    return apiRequest<Record<string, unknown>>(`${BASE}/${encodeURIComponent(integrationId)}/health`);
  },
  diagnostics(integrationId: string) {
    return apiRequest<Record<string, unknown>>(`${BASE}/${encodeURIComponent(integrationId)}/diagnostics`);
  },
  definitionUpgradePreview(integrationId: string) {
    return apiRequest<Record<string, unknown>>(
      `${BASE}/${encodeURIComponent(integrationId)}/definition-upgrade/preview`,
      { method: "POST" },
    );
  },
  definitionUpgradeApply(
    integrationId: string,
    payload: { expected_definition_version: string; expected_manifest_fingerprint: string },
  ) {
    return apiRequest<Record<string, unknown>>(
      `${BASE}/${encodeURIComponent(integrationId)}/definition-upgrade/apply`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) },
    );
  },
  settings(integrationId: string) {
    return apiRequest<IntegrationSettingsModel>(`${BASE}/${encodeURIComponent(integrationId)}/settings`);
  },
  updateSettings(integrationId: string, values: Record<string, unknown>) {
    return apiRequest<IntegrationSettingsModel>(`${BASE}/${encodeURIComponent(integrationId)}/settings`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values }),
    });
  },
  updateAuthorization(integrationId: string, payload: { permission_mode: "restricted" | "custom" | "full"; granted_capabilities: string[]; requested_capabilities?: string[] }) {
    return apiRequest<IntegrationManagementSummary>(`${BASE}/${encodeURIComponent(integrationId)}/authorization`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

};
